package main

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/plugin"
	"github.com/mattermost/mattermost/server/public/pluginapi"
	"github.com/mattermost/mattermost/server/public/pluginapi/cluster"
)

func main() {
	plugin.ClientMain(&ThreadwisePlugin{})
}

type ThreadwisePlugin struct {
	plugin.MattermostPlugin

	mu          sync.RWMutex
	client      *pluginapi.Client
	config      Settings
	service     *Service
	botUserID   string
	digestMutex *cluster.Mutex
	ctx         context.Context
	cancel      context.CancelFunc
}

type Settings struct {
	GroqAPIKey     string `json:"groqapikey"`
	Mem0APIKey     string `json:"mem0apikey"`
	FastModel      string `json:"fastmodel"`
	SummaryModel   string `json:"summarymodel"`
	MaxPosts       int    `json:"maxposts"`
	DigestTime     string `json:"digesttime"`
	Timezone       string `json:"timezone"`
	CommitmentDays int    `json:"commitmentdays"`
	InactivityDays int    `json:"inactivitydays"`
}

func (p *ThreadwisePlugin) OnConfigurationChange() error {
	var config Settings
	if err := p.API.LoadPluginConfiguration(&config); err != nil {
		return fmt.Errorf("load Threadwise configuration: %w", err)
	}
	if config.FastModel == "" {
		config.FastModel = "llama-3.1-8b-instant"
	}
	if config.SummaryModel == "" {
		config.SummaryModel = "llama-3.3-70b-versatile"
	}
	if config.MaxPosts == 0 {
		config.MaxPosts = 100
	}
	if config.DigestTime == "" {
		config.DigestTime = "09:00"
	}
	if config.Timezone == "" {
		config.Timezone = "UTC"
	}
	if config.CommitmentDays < 1 {
		config.CommitmentDays = 3
	}
	if config.InactivityDays < 1 {
		config.InactivityDays = 14
	}
	if config.MaxPosts < 1 || config.MaxPosts > 500 {
		return fmt.Errorf("maximum messages per summary must be between 1 and 500")
	}
	if _, err := time.Parse("15:04", config.DigestTime); err != nil {
		return fmt.Errorf("invalid Threadwise digest time %q: expected HH:MM", config.DigestTime)
	}
	if _, err := time.LoadLocation(config.Timezone); err != nil {
		return fmt.Errorf("invalid Threadwise timezone %q: %w", config.Timezone, err)
	}
	if config.CommitmentDays > 3650 || config.InactivityDays > 3650 {
		return fmt.Errorf("Threadwise day thresholds must not exceed 3650")
	}

	p.mu.Lock()
	p.config = config
	p.mu.Unlock()
	return nil
}

func (p *ThreadwisePlugin) OnActivate() error {
	p.client = pluginapi.NewClient(p.API, p.Driver)
	botUserID, err := p.client.Bot.EnsureBot(&model.Bot{
		Username:    "threadwise-assistant",
		DisplayName: "Threadwise",
		Description: "Posts Threadwise AI answers in conversation threads.",
	})
	if err != nil {
		return fmt.Errorf("ensure Threadwise bot: %w", err)
	}
	p.botUserID = botUserID
	p.ctx, p.cancel = context.WithCancel(context.Background())
	p.service = NewService(p.client, p.currentSettings)
	p.digestMutex, err = cluster.NewMutex(p.API, "threadwise_digest_scheduler")
	if err != nil {
		return fmt.Errorf("create Threadwise digest scheduler mutex: %w", err)
	}
	if err := p.registerCommands(); err != nil {
		return err
	}

	go p.runDigestScheduler()
	return nil
}

func (p *ThreadwisePlugin) registerCommands() error {
	for _, command := range []string{"summarize", "threadwise", "threadwise-email"} {
		if err := p.API.RegisterCommand(&model.Command{
			TeamId:           "",
			Trigger:          command,
			DisplayName:      "Threadwise",
			Method:           model.CommandMethodPost,
			AutoComplete:     true,
			AutoCompleteDesc: "Thread and relationship intelligence",
			AutoCompleteHint: "[question, email, or digest settings]",
		}); err != nil {
			return fmt.Errorf("register /%s: %w", command, err)
		}
	}
	return nil
}

func (p *ThreadwisePlugin) OnDeactivate() error {
	if p.cancel != nil {
		p.cancel()
	}
	return nil
}

func (p *ThreadwisePlugin) currentSettings() Settings {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.config
}

func (p *ThreadwisePlugin) ExecuteCommand(_ *plugin.Context, args *model.CommandArgs) (*model.CommandResponse, *model.AppError) {
	if args == nil || args.UserId == "" || args.ChannelId == "" {
		return ephemeral("Threadwise needs a user and channel context."), nil
	}
	fields := strings.Fields(args.Command)
	if len(fields) == 0 {
		return ephemeral("Try `/summarize`, `/threadwise-email <email>`, or `/threadwise digest on|off`."), nil
	}
	trigger := strings.TrimPrefix(fields[0], "/")
	text := strings.TrimSpace(strings.TrimPrefix(args.Command, fields[0]))
	response := p.service.ExecuteCommand(p.ctx, trigger, args, text)
	return ephemeral(response), nil
}

func (p *ThreadwisePlugin) MessageHasBeenPosted(_ *plugin.Context, post *model.Post) {
	p.API.LogInfo("MessageHasBeenPosted invoked")
	if post == nil || post.UserId == "" || post.ChannelId == "" || post.Type != "" {
		p.API.LogInfo("MessageHasBeenPosted ignored: post is nil, missing identifiers, or is a system message")
		return
	}
	p.service.recordActivity(post)
	question, ok := aiQuestion(post.Message)
	p.API.LogInfo("Checked AI mention prefix", "matched", ok)
	if !ok {
		p.API.LogInfo("MessageHasBeenPosted ignored: no AI prefix matched")
		lowerMessage := strings.ToLower(post.Message)
		if strings.Contains(lowerMessage, "@ai") || strings.Contains(lowerMessage, "@threadwise") {
			p.API.LogDebug("Threadwise ignored a mention that was not at the start of the post", "post_id", post.Id, "channel_id", post.ChannelId)
		}
		return
	}
	p.API.LogInfo("Threadwise matched a question mention", "post_id", post.Id, "channel_id", post.ChannelId, "thread_id", post.RootId, "has_question", question != "")
	if question == "" {
		p.API.LogInfo("MessageHasBeenPosted ignored: question text is empty")
		p.API.LogInfo("Sending ephemeral response: prompt for question")
		p.sendEphemeral(post.UserId, post.ChannelId, rootID(post), "Add a question after `@AI`.")
		return
	}
	go func(post *model.Post, question string) {
		p.API.LogInfo("Starting AI answer processing")
		answer, err := p.service.Answer(p.ctx, post, question)
		if err != nil {
			p.API.LogError("Threadwise could not answer message", "error", err.Error())
			p.API.LogInfo("Sending ephemeral response: error notification")
			p.sendEphemeral(post.UserId, post.ChannelId, rootID(post), "Threadwise could not answer right now. Please try again later.")
			return
		}
		p.API.LogInfo("AI answer processing completed successfully")
		reply := &model.Post{
			UserId:    p.botUserID,
			ChannelId: post.ChannelId,
			RootId:    rootID(post),
			Message:   answer,
		}
		if err := p.ensureBotMembership(post.ChannelId); err != nil {
			p.API.LogError("Threadwise could not join conversation to post public reply", "error", err.Error(), "post_id", post.Id, "channel_id", post.ChannelId)
			p.sendEphemeral(post.UserId, post.ChannelId, rootID(post), "Threadwise could not post a public reply right now. Please try again later.")
			return
		}
		if err := p.client.Post.CreatePost(reply); err != nil {
			p.API.LogError("Threadwise could not post public reply", "error", err.Error(), "post_id", post.Id, "channel_id", post.ChannelId)
			p.sendEphemeral(post.UserId, post.ChannelId, rootID(post), "Threadwise could not post a public reply right now. Please try again later.")
			return
		}
		p.API.LogInfo("Threadwise posted a public thread reply", "post_id", post.Id, "reply_id", reply.Id, "channel_id", post.ChannelId, "thread_id", rootID(post))
	}(post.Clone(), question)
}

func (p *ThreadwisePlugin) ensureBotMembership(channelID string) error {
	channel, err := p.client.Channel.Get(channelID)
	if err != nil {
		return fmt.Errorf("get channel: %w", err)
	}
	if channel.TeamId != "" {
		if _, err := p.client.Team.GetMember(channel.TeamId, p.botUserID); err != nil {
			if _, addErr := p.client.Team.CreateMember(channel.TeamId, p.botUserID); addErr != nil {
				if _, memberErr := p.client.Team.GetMember(channel.TeamId, p.botUserID); memberErr != nil {
					return fmt.Errorf("join team: %w", addErr)
				}
			}
		}
	}
	if _, err := p.client.Channel.GetMember(channelID, p.botUserID); err != nil {
		if _, addErr := p.client.Channel.AddMember(channelID, p.botUserID); addErr != nil {
			if _, memberErr := p.client.Channel.GetMember(channelID, p.botUserID); memberErr != nil {
				return fmt.Errorf("join channel: %w", addErr)
			}
		}
	}
	return nil
}

func rootID(post *model.Post) string {
	if post.RootId != "" {
		return post.RootId
	}
	return post.Id
}

func (p *ThreadwisePlugin) sendEphemeral(userID, channelID, rootID, text string) {
	p.client.Post.SendEphemeralPost(userID, &model.Post{
		UserId:    userID,
		ChannelId: channelID,
		RootId:    rootID,
		Message:   text,
	})
}

func (p *ThreadwisePlugin) runDigestScheduler() {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	lastRun := ""
	for {
		select {
		case <-p.ctx.Done():
			return
		case now := <-ticker.C:
			settings := p.currentSettings()
			if settings.Mem0APIKey == "" {
				continue
			}
			zone, err := time.LoadLocation(settings.Timezone)
			if err != nil {
				p.API.LogError("Invalid Threadwise digest timezone", "timezone", settings.Timezone)
				continue
			}
			localNow := now.In(zone)
			digestTime, err := time.Parse("15:04", settings.DigestTime)
			if err != nil {
				p.API.LogError("Invalid Threadwise digest time", "time", settings.DigestTime, "error", err.Error())
				continue
			}
			if localNow.Weekday() == time.Saturday || localNow.Weekday() == time.Sunday || localNow.Hour() != digestTime.Hour() || localNow.Minute() != digestTime.Minute() {
				continue
			}
			date := localNow.Format("2006-01-02")
			if date == lastRun {
				continue
			}
			if err := p.digestMutex.LockWithContext(p.ctx); err != nil {
				if p.ctx.Err() != nil {
					return
				}
				p.API.LogError("Could not acquire Threadwise digest scheduler lock", "error", err.Error())
				continue
			}
			var completedDate string
			if err := p.client.KV.Get("digest-run:"+date, &completedDate); err != nil {
				p.digestMutex.Unlock()
				p.API.LogError("Could not check Threadwise digest run marker", "error", err.Error())
				continue
			}
			if completedDate == date {
				p.digestMutex.Unlock()
				lastRun = date
				continue
			}
			if err := p.service.RunDigests(p.ctx, localNow); err != nil {
				p.digestMutex.Unlock()
				p.API.LogError("Threadwise digest run failed", "error", err.Error())
				continue
			}
			if _, err := p.client.KV.Set("digest-run:"+date, date, pluginapi.SetExpiry(48*time.Hour)); err != nil {
				p.digestMutex.Unlock()
				p.API.LogError("Could not persist Threadwise digest run marker", "error", err.Error())
				continue
			}
			p.digestMutex.Unlock()
			lastRun = date
		}
	}
}

func ephemeral(text string) *model.CommandResponse {
	return &model.CommandResponse{
		ResponseType: model.CommandResponseTypeEphemeral,
		Text:         text,
	}
}
