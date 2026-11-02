package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/mattermost/mattermost/server/public/model"
	"github.com/mattermost/mattermost/server/public/pluginapi"
)

const (
	groqEndpoint        = "https://api.groq.com/openai/v1/chat/completions"
	mem0Endpoint        = "https://api.mem0.ai/v3/memories"
	maxProviderResponse = 1 << 20
)

type SettingsProvider func() Settings

type Service struct {
	client   *pluginapi.Client
	settings SettingsProvider
	http     *http.Client
}

type Summary struct {
	TLDR          string       `json:"tldr"`
	Commitments   []Commitment `json:"commitments"`
	OpenFollowups []Commitment `json:"open_followups"`
	EntityName    string       `json:"entity_name"`
	ThreadType    string       `json:"thread_type"`
}

type Commitment struct {
	Action string `json:"action"`
	Owner  string `json:"owner"`
	Due    string `json:"due,omitempty"`
}

type emailSummary struct {
	TLDR              string       `json:"tldr"`
	KeyPoints         []string     `json:"key_points"`
	ActionItems       []Commitment `json:"action_items"`
	SuggestedFollowup string       `json:"suggested_followup_date"`
}

type providerMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type groqRequest struct {
	Model          string            `json:"model"`
	Messages       []providerMessage `json:"messages"`
	MaxTokens      int               `json:"max_tokens"`
	Temperature    float64           `json:"temperature"`
	ResponseFormat *struct {
		Type string `json:"type"`
	} `json:"response_format,omitempty"`
}

type groqResponse struct {
	Choices []struct {
		Message providerMessage `json:"message"`
	} `json:"choices"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

type memoryResult struct {
	Memory   string         `json:"memory"`
	UserID   string         `json:"user_id"`
	Metadata map[string]any `json:"metadata"`
}

type memorySearchResponse struct {
	Results []memoryResult `json:"results"`
}

type cachedSummary struct {
	LatestPostID string  `json:"latest_post_id"`
	Summary      Summary `json:"summary"`
}

type emailDraft struct {
	Text      string `json:"text"`
	CreatedAt int64  `json:"created_at"`
}

func NewService(client *pluginapi.Client, settings SettingsProvider) *Service {
	return &Service{
		client:   client,
		settings: settings,
		http:     &http.Client{Timeout: 45 * time.Second},
	}
}

func (s *Service) ExecuteCommand(ctx context.Context, trigger string, args *model.CommandArgs, text string) string {
	switch trigger {
	case "summarize":
		return s.summarizeCommand(ctx, args)
	case "threadwise-email":
		return s.emailCommand(ctx, args, text)
	case "threadwise":
		return s.threadwiseCommand(ctx, args, text)
	default:
		return "Unknown Threadwise command."
	}
}

func (s *Service) summarizeCommand(ctx context.Context, args *model.CommandArgs) string {
	if _, err := s.client.Channel.GetMember(args.ChannelId, args.UserId); err != nil {
		return "You must be a member of this channel to summarize it."
	}
	channel, err := s.client.Channel.Get(args.ChannelId)
	if err != nil {
		return "I couldn't access this channel."
	}

	scope := args.RootId
	posts, err := s.loadPosts(args.ChannelId, scope)
	if err != nil {
		s.client.Log.Error("Failed to load Mattermost posts for summary", "error", err.Error())
		return "I couldn't load the conversation to summarize."
	}
	if len(posts) == 0 {
		return "There are no messages to summarize."
	}

	summary, err := s.summarize(ctx, args.UserId, channel.Id, scope, posts)
	if err != nil {
		s.client.Log.Error("Threadwise summary failed", "error", err.Error())
		return providerFailure(err)
	}
	return formatSummary(summary, scope == "")
}

func (s *Service) emailCommand(ctx context.Context, args *model.CommandArgs, text string) string {
	if strings.TrimSpace(text) == "" {
		return "Usage: `/threadwise-email <paste email text>`"
	}
	if !utf8.ValidString(text) || len(text) > 40_000 {
		return "Email text must be valid UTF-8 and no larger than 40,000 bytes."
	}
	settings := s.settings()
	if settings.GroqAPIKey == "" {
		return "Threadwise needs a Groq API key. Ask your Mattermost administrator to configure the plugin."
	}
	content, err := s.complete(ctx, settings.SummaryModel, "Summarize the email as JSON with fields tldr, key_points (array), action_items (array of {action, owner, due}), and suggested_followup_date. Use an empty string when no date is stated. Do not invent owners or dates.", text, true)
	if err != nil {
		return providerFailure(err)
	}
	var result emailSummary
	if err := json.Unmarshal([]byte(content), &result); err != nil {
		return "The AI returned an unreadable email summary. Please try again."
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return "I couldn't save the email summary draft."
	}
	_, err = s.client.KV.Set(draftKey(args.UserId), emailDraft{Text: string(encoded), CreatedAt: time.Now().Unix()}, pluginapi.SetExpiry(24*time.Hour))
	if err != nil {
		s.client.Log.Error("Failed to store Threadwise email draft", "error", err.Error())
		return "I summarized the email but couldn't save it for attachment."
	}
	return formatEmailSummary(result) + "\n\nWant me to attach this to a thread? Run `/threadwise attach <post-id>` in that conversation."
}

func (s *Service) threadwiseCommand(ctx context.Context, args *model.CommandArgs, text string) string {
	parts := strings.Fields(text)
	if len(parts) == 0 {
		return "Try `/threadwise digest on|off`, `/threadwise remember <preference>`, or `/threadwise attach <post-id>`."
	}
	switch strings.ToLower(parts[0]) {
	case "digest":
		if len(parts) != 2 || (strings.ToLower(parts[1]) != "on" && strings.ToLower(parts[1]) != "off") {
			return "Use `/threadwise digest on` or `/threadwise digest off`."
		}
		enabled := strings.ToLower(parts[1]) == "on"
		if _, err := s.client.KV.Set(digestKey(args.UserId), enabled); err != nil {
			s.client.Log.Error("Failed to update Threadwise digest preference", "error", err.Error())
			return "I couldn't update your digest preference."
		}
		if enabled {
			return "Weekday private follow-up digests enabled."
		}
		return "Weekday follow-up digests disabled."
	case "remember":
		preference := strings.TrimSpace(strings.TrimPrefix(text, parts[0]))
		if preference == "" {
			return "Tell me what preference to remember."
		}
		if err := s.addMemory(ctx, args.UserId, "", "", "preference", preference, nil); err != nil {
			return providerFailure(err)
		}
		return "I'll remember that preference for your future Threadwise answers."
	case "attach":
		postID := ""
		if len(parts) > 1 {
			postID = parts[1]
		}
		if args.RootId != "" {
			postID = args.RootId
		}
		if postID == "" {
			return "Use `/threadwise attach <post-id>` in the target conversation."
		}
		return s.attachDraft(ctx, args, postID)
	default:
		return "Try `/threadwise digest on|off`, `/threadwise remember <preference>`, or `/threadwise attach <post-id>`."
	}
}

func (s *Service) attachDraft(ctx context.Context, args *model.CommandArgs, postID string) string {
	draft := emailDraft{}
	if err := s.client.KV.Get(draftKey(args.UserId), &draft); err != nil {
		s.client.Log.Error("Failed to load Threadwise email draft", "error", err.Error())
		return "I couldn't retrieve the email draft. Please try again."
	}
	if draft.Text == "" {
		return "I couldn't find an email summary draft. Drafts expire after 24 hours."
	}
	post, err := s.client.Post.GetPost(postID)
	if err != nil {
		s.client.Log.Error("Failed to load post for email attachment", "error", err.Error())
		return "That post isn't available in this channel."
	}
	if post.ChannelId != args.ChannelId {
		return "That post isn't available in this channel."
	}
	if _, err := s.client.Channel.GetMember(post.ChannelId, args.UserId); err != nil {
		return "You must be a member of the target channel to attach this email."
	}
	threadID := post.RootId
	if threadID == "" {
		threadID = post.Id
	}
	if err := s.addMemory(ctx, args.UserId, threadID, post.ChannelId, "email_summary", draft.Text, map[string]any{
		"thread_type": "client",
	}); err != nil {
		return providerFailure(err)
	}
	if err := s.client.KV.Delete(draftKey(args.UserId)); err != nil {
		s.client.Log.Error("Attached email but failed to clear its draft", "error", err.Error())
	}
	return "Email summary attached to this conversation's memory."
}

func (s *Service) Answer(ctx context.Context, post *model.Post, question string) (string, error) {
	if _, err := s.client.Channel.GetMember(post.ChannelId, post.UserId); err != nil {
		return "", fmt.Errorf("requester is not a member of the channel")
	}
	threadID := post.RootId
	if threadID == "" {
		threadID = post.Id
	}
	posts, err := s.loadPosts(post.ChannelId, threadID)
	if err != nil {
		return "", err
	}
	if len(posts) == 0 {
		return "", fmt.Errorf("thread has no readable messages")
	}
	memories, err := s.searchMemories(ctx, post.UserId, question, threadID, post.ChannelId)
	if err != nil {
		return "", err
	}
	contextText := s.formatPostsWithUsers(posts)
	if len(memories) > 0 {
		contextText = "Relevant persistent memories:\n" + strings.Join(memories, "\n") + "\n\nConversation:\n" + contextText
	}
	settings := s.settings()
	if settings.GroqAPIKey == "" {
		return "", fmt.Errorf("Groq API key is not configured")
	}
	answer, err := s.complete(ctx, settings.FastModel, "Answer only from the provided conversation and relevant memories. State when the answer is not present. Keep the answer concise.", "Question: "+question+"\n\nContext:\n"+contextText, false)
	if isQuotaError(err) {
		var cached cachedSummary
		if cacheErr := s.client.KV.Get(cacheKey(post.UserId, post.ChannelId, threadID), &cached); cacheErr == nil && cached.LatestPostID == latestPostID(posts) {
			return "Groq is rate-limited. Here is the cached summary instead:\n\n" + formatSummary(cached.Summary, false), nil
		}
	}
	return answer, err
}

func (s *Service) summarize(ctx context.Context, userID, channelID, scope string, posts []*model.Post) (Summary, error) {
	latestID := latestPostID(posts)
	key := cacheKey(userID, channelID, scope)
	var cached cachedSummary
	if err := s.client.KV.Get(key, &cached); err != nil {
		return Summary{}, fmt.Errorf("read summary cache: %w", err)
	}
	if latestID != "" && cached.LatestPostID == latestID {
		return cached.Summary, nil
	}

	settings := s.settings()
	if settings.GroqAPIKey == "" {
		if cached.LatestPostID == latestID {
			return cached.Summary, nil
		}
		return Summary{}, fmt.Errorf("Groq API key is not configured")
	}
	content, err := s.complete(ctx, settings.SummaryModel, "Return JSON with fields tldr (2-3 concise sentences), commitments (array of {action, owner, due}), open_followups (array of {action, owner, due}), entity_name, and thread_type (client, project, opportunity, or internal). Only extract facts stated in the messages; do not infer owners or dates.", s.formatPostsWithUsers(posts), true)
	if err != nil {
		if isQuotaError(err) && cached.LatestPostID == latestID {
			return cached.Summary, nil
		}
		return Summary{}, err
	}
	var summary Summary
	if err := json.Unmarshal([]byte(content), &summary); err != nil {
		return Summary{}, fmt.Errorf("parse summary response: %w", err)
	}
	if summary.ThreadType == "" {
		summary.ThreadType = "internal"
	}
	if err := s.addMemory(ctx, userID, scope, channelID, "summary", formatSummary(summary, scope == ""), map[string]any{
		"entity_name":    summary.EntityName,
		"thread_type":    summary.ThreadType,
		"commitments":    summary.Commitments,
		"open_followups": summary.OpenFollowups,
		"latest_post_id": latestID,
		"summary_at":     time.Now().Unix(),
	}); err != nil {
		return Summary{}, fmt.Errorf("save summary to Mem0: %w", err)
	}
	_, err = s.client.KV.Set(key, cachedSummary{LatestPostID: latestID, Summary: summary})
	if err != nil {
		s.client.Log.Error("Could not cache Threadwise summary", "error", err.Error())
	}
	return summary, nil
}

func (s *Service) loadPosts(channelID, scope string) ([]*model.Post, error) {
	var list *model.PostList
	var err error
	if scope != "" {
		list, err = s.client.Post.GetPostThread(scope)
	} else {
		var posts []*model.Post
		for page := 0; len(posts) < s.settings().MaxPosts; page++ {
			var list *model.PostList
			list, err = s.client.Post.GetPostsForChannel(channelID, page, 100)
			if err != nil {
				return nil, err
			}
			pagePosts := list.ToSlice()
			if len(pagePosts) == 0 {
				break
			}
			posts = append(posts, pagePosts...)
			if len(pagePosts) < 100 {
				break
			}
		}
		sort.Slice(posts, func(i, j int) bool { return posts[i].CreateAt < posts[j].CreateAt })
		if max := s.settings().MaxPosts; len(posts) > max {
			posts = posts[len(posts)-max:]
		}
		return posts, nil
	}
	if err != nil {
		return nil, err
	}
	posts := list.ToSlice()
	sort.Slice(posts, func(i, j int) bool { return posts[i].CreateAt < posts[j].CreateAt })
	if max := s.settings().MaxPosts; len(posts) > max {
		posts = posts[len(posts)-max:]
	}
	return posts, nil
}

func (s *Service) formatPostsWithUsers(posts []*model.Post) string {
	users := make(map[string]string)
	for _, post := range posts {
		if _, ok := users[post.UserId]; ok {
			continue
		}
		user, err := s.client.User.Get(post.UserId)
		if err == nil && user != nil {
			users[post.UserId] = user.Username
		} else {
			users[post.UserId] = "unknown"
		}
	}
	var b strings.Builder
	for _, post := range posts {
		fmt.Fprintf(&b, "[%s] %s: %s\n", time.UnixMilli(post.CreateAt).UTC().Format(time.RFC3339), users[post.UserId], post.Message)
	}
	return truncateContext(b.String(), 40_000)
}

func truncateContext(value string, maxRunes int) string {
	runes := []rune(value)
	if len(runes) <= maxRunes {
		return value
	}
	return "[Earlier messages omitted to fit the AI context window.]\n" + string(runes[len(runes)-maxRunes:])
}

func (s *Service) complete(ctx context.Context, modelName, system, user string, jsonMode bool) (string, error) {
	settings := s.settings()
	if settings.GroqAPIKey == "" {
		return "", fmt.Errorf("Groq API key is not configured")
	}
	response, err := s.callGroq(ctx, settings.GroqAPIKey, modelName, system, user, jsonMode)
	if err == nil {
		return response, nil
	}
	if !jsonMode || !isQuotaError(err) || modelName == settings.FastModel {
		return "", err
	}
	return s.callGroq(ctx, settings.GroqAPIKey, settings.FastModel, system, user, true)
}

func (s *Service) callGroq(ctx context.Context, apiKey, modelName, system, user string, jsonMode bool) (string, error) {
	request := groqRequest{
		Model:       modelName,
		Messages:    []providerMessage{{Role: "system", Content: system}, {Role: "user", Content: user}},
		MaxTokens:   900,
		Temperature: 0.1,
	}
	if jsonMode {
		request.ResponseFormat = &struct {
			Type string `json:"type"`
		}{Type: "json_object"}
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, groqEndpoint, bytes.NewReader(payload))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("Groq request failed: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxProviderResponse))
	if err != nil {
		return "", fmt.Errorf("read Groq response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", providerError("Groq", resp.StatusCode, body)
	}
	var result groqResponse
	if err := json.Unmarshal(body, &result); err != nil || len(result.Choices) == 0 {
		return "", fmt.Errorf("Groq returned an invalid response")
	}
	return strings.TrimSpace(result.Choices[0].Message.Content), nil
}

func (s *Service) searchMemories(ctx context.Context, userID, query, threadID, channelID string) ([]string, error) {
	settings := s.settings()
	if settings.Mem0APIKey == "" {
		return nil, fmt.Errorf("Mem0 API key is not configured")
	}
	payload, err := json.Marshal(map[string]any{
		"query":   query,
		"filters": map[string]string{"user_id": userID},
	})
	if err != nil {
		return nil, err
	}
	body, err := s.mem0Request(ctx, http.MethodPost, "/search/", payload)
	if err != nil {
		return nil, err
	}
	var result memorySearchResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("parse Mem0 search response: %w", err)
	}
	return scopedMemories(result.Results, userID, threadID, channelID), nil
}

func scopedMemories(results []memoryResult, userID, threadID, channelID string) []string {
	memories := make([]string, 0, min(len(results), 5))
	for _, item := range results {
		if item.UserID != "" && item.UserID != userID {
			continue
		}
		meta := item.Metadata
		if meta["application"] != "threadwise" || meta["user_id"] != userID {
			continue
		}
		kind, _ := meta["memory_kind"].(string)
		sameThread := meta["thread_id"] == threadID && meta["channel_id"] == channelID
		sameChannelSummary := meta["thread_id"] == "" && meta["channel_id"] == channelID
		if kind != "preference" && !sameThread && !sameChannelSummary {
			continue
		}
		if strings.TrimSpace(item.Memory) != "" {
			memories = append(memories, item.Memory)
		}
		if len(memories) == 5 {
			break
		}
	}
	return memories
}

func (s *Service) addMemory(ctx context.Context, userID, threadID, channelID, kind, text string, extra map[string]any) error {
	settings := s.settings()
	if settings.Mem0APIKey == "" {
		return fmt.Errorf("Mem0 API key is not configured")
	}
	metadata := map[string]any{
		"application": "threadwise",
		"user_id":     userID,
		"thread_id":   threadID,
		"channel_id":  channelID,
		"memory_kind": kind,
	}
	for key, value := range extra {
		metadata[key] = value
	}
	payload, err := json.Marshal(map[string]any{
		"messages": []providerMessage{{Role: "user", Content: text}},
		"user_id":  userID,
		"metadata": metadata,
	})
	if err != nil {
		return err
	}
	_, err = s.mem0Request(ctx, http.MethodPost, "/add/", payload)
	return err
}

func (s *Service) mem0Request(ctx context.Context, method, path string, body []byte) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, mem0Endpoint+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Token "+s.settings().Mem0APIKey)
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Mem0 request failed: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxProviderResponse))
	if err != nil {
		return nil, fmt.Errorf("read Mem0 response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, providerError("Mem0", resp.StatusCode, data)
	}
	return data, nil
}

func (s *Service) RunDigests(ctx context.Context, now time.Time) error {
	keys, err := s.client.KV.ListKeys(0, 1000)
	if err != nil {
		return fmt.Errorf("list digest preferences: %w", err)
	}
	for _, key := range keys {
		if !strings.HasPrefix(key, "digest:") {
			continue
		}
		var enabled bool
		if err := s.client.KV.Get(key, &enabled); err != nil {
			return fmt.Errorf("read digest preference: %w", err)
		}
		if !enabled {
			continue
		}
		userID := strings.TrimPrefix(key, "digest:")
		if err := s.sendUserDigest(ctx, userID, now); err != nil {
			s.client.Log.Error("Failed to send Threadwise digest", "user_id", userID, "error", err.Error())
		}
	}
	return nil
}

func (s *Service) sendUserDigest(ctx context.Context, userID string, now time.Time) error {
	if s.settings().Mem0APIKey == "" {
		return fmt.Errorf("Mem0 API key is not configured")
	}
	payload, _ := json.Marshal(map[string]any{
		"query":   "open commitments and inactive conversations that need follow-up",
		"filters": map[string]string{"user_id": userID},
	})
	body, err := s.mem0Request(ctx, http.MethodPost, "/search/", payload)
	if err != nil {
		return err
	}
	var result memorySearchResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return err
	}
	for _, memory := range result.Results {
		if memory.UserID != "" && memory.UserID != userID {
			continue
		}
		meta := memory.Metadata
		if meta["application"] != "threadwise" || meta["user_id"] != userID || meta["memory_kind"] != "summary" {
			continue
		}
		channelID, _ := meta["channel_id"].(string)
		threadID, _ := meta["thread_id"].(string)
		if channelID == "" || threadID == "" {
			continue
		}
		if _, err := s.client.Channel.GetMember(channelID, userID); err != nil {
			continue
		}
		lastActivity, err := s.activityAt(threadID)
		if err != nil {
			return err
		}
		items := staleFollowups(meta, now, lastActivity, s.settings().CommitmentDays)
		if lastActivity == 0 {
			lastActivity = memoryTimestamp(meta)
		}
		if now.Sub(time.Unix(lastActivity, 0)) >= time.Duration(s.settings().InactivityDays)*24*time.Hour {
			items = append(items, "This conversation may be inactive (no recent message observed).")
		}
		if len(items) == 0 {
			continue
		}
		s.client.Post.SendEphemeralPost(userID, &model.Post{
			UserId:    userID,
			ChannelId: channelID,
			RootId:    threadID,
			Message:   "Threadwise follow-up digest\n" + strings.Join(items, "\n"),
		})
	}
	return nil
}

func (s *Service) activityAt(threadID string) (int64, error) {
	var activity int64
	if err := s.client.KV.Get(activityKey(threadID), &activity); err != nil {
		return 0, fmt.Errorf("read thread activity: %w", err)
	}
	return activity, nil
}

func (s *Service) recordActivity(post *model.Post) {
	threadID := post.RootId
	if threadID == "" {
		threadID = post.Id
	}
	_, err := s.client.KV.Set(activityKey(threadID), post.CreateAt/1000)
	if err != nil {
		s.client.Log.Error("Failed to track Threadwise thread activity", "error", err.Error())
	}
}

func formatSummary(summary Summary, channel bool) string {
	label := "Thread summary"
	if channel {
		label = "Channel summary"
	}
	var b strings.Builder
	fmt.Fprintf(&b, "**%s**\n%s", label, summary.TLDR)
	if len(summary.Commitments) > 0 {
		b.WriteString("\n\n**Commitments made**")
		for _, item := range summary.Commitments {
			fmt.Fprintf(&b, "\n- %s — %s", item.Action, ownerOrUnknown(item.Owner))
			if item.Due != "" {
				fmt.Fprintf(&b, " (due %s)", item.Due)
			}
		}
	}
	if len(summary.OpenFollowups) > 0 {
		b.WriteString("\n\n**Open follow-ups**")
		for _, item := range summary.OpenFollowups {
			fmt.Fprintf(&b, "\n- %s — %s", item.Action, ownerOrUnknown(item.Owner))
			if item.Due != "" {
				fmt.Fprintf(&b, " (due %s)", item.Due)
			}
		}
	}
	return b.String()
}

func formatEmailSummary(summary emailSummary) string {
	var b strings.Builder
	fmt.Fprintf(&b, "**Email summary**\n%s", summary.TLDR)
	if len(summary.KeyPoints) > 0 {
		b.WriteString("\n\n**Key points**")
		for _, point := range summary.KeyPoints {
			fmt.Fprintf(&b, "\n- %s", point)
		}
	}
	if len(summary.ActionItems) > 0 {
		b.WriteString("\n\n**Action items**")
		for _, item := range summary.ActionItems {
			fmt.Fprintf(&b, "\n- %s — %s", item.Action, ownerOrUnknown(item.Owner))
		}
	}
	if summary.SuggestedFollowup != "" {
		fmt.Fprintf(&b, "\n\n**Suggested follow-up:** %s", summary.SuggestedFollowup)
	}
	return b.String()
}

func ownerOrUnknown(owner string) string {
	if strings.TrimSpace(owner) == "" {
		return "owner not specified"
	}
	return owner
}

func aiQuestion(message string) (string, bool) {
	message = strings.TrimSpace(message)
	for _, mention := range []string{"@AI", "@threadwise"} {
		if len(message) >= len(mention) && strings.EqualFold(message[:len(mention)], mention) {
			return strings.TrimSpace(message[len(mention):]), true
		}
	}
	return "", false
}

func latestPostID(posts []*model.Post) string {
	if len(posts) == 0 {
		return ""
	}
	var latest *model.Post
	for _, post := range posts {
		if latest == nil || post.CreateAt > latest.CreateAt {
			latest = post
		}
	}
	return latest.Id
}

func cacheKey(userID, channelID, scope string) string {
	sum := sha256.Sum256([]byte(userID + ":" + channelID + ":" + scope))
	return "summary:" + hex.EncodeToString(sum[:])
}

func draftKey(userID string) string      { return "draft:" + userID }
func digestKey(userID string) string     { return "digest:" + userID }
func activityKey(threadID string) string { return "activity:" + threadID }

func providerError(provider string, status int, body []byte) error {
	var detail struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
		Message string `json:"message"`
	}
	_ = json.Unmarshal(body, &detail)
	message := detail.Error.Message
	if message == "" {
		message = detail.Message
	}
	if message == "" {
		message = http.StatusText(status)
	}
	return fmt.Errorf("%s returned HTTP %d: %s", provider, status, message)
}

func isQuotaError(err error) bool {
	if err == nil {
		return false
	}
	text := strings.ToLower(err.Error())
	return strings.Contains(text, "429") || strings.Contains(text, "rate limit") || strings.Contains(text, "quota")
}

func providerFailure(err error) string {
	if isQuotaError(err) {
		return "Groq is rate-limited or out of quota. Please try again later."
	}
	return "Threadwise couldn't complete that request. Check the plugin configuration and server logs."
}

func staleFollowups(metadata map[string]any, now time.Time, lastActivity int64, thresholdDays int) []string {
	items, ok := metadata["open_followups"].([]any)
	if !ok {
		return nil
	}
	summaryAt := time.Unix(memoryTimestamp(metadata), 0)
	if lastActivity > summaryAt.Unix() {
		return nil
	}
	if now.Sub(summaryAt) < time.Duration(thresholdDays)*24*time.Hour {
		return nil
	}
	out := make([]string, 0, len(items))
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		action, _ := item["action"].(string)
		owner, _ := item["owner"].(string)
		if action == "" {
			continue
		}
		out = append(out, fmt.Sprintf("- Follow-up overdue: %s — %s", action, ownerOrUnknown(owner)))
	}
	return out
}

func memoryTimestamp(metadata map[string]any) int64 {
	if value, ok := metadata["summary_at"].(float64); ok {
		return int64(value)
	}
	return 0
}
