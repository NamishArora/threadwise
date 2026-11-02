# ThreadWise Plugin

ThreadWise runs as a Go plugin managed by the platform. It does not need a separate Python service, slash-command callback URLs, or a manually created bot account. It uses plugin APIs and calls Groq and Mem0 directly.

```text
Mattermost channel/thread event
    -> plugin checks the requesting user's channel membership
    -> retrieve that user's same-thread/channel memories from Mem0
    -> Groq answers or summarizes bounded recent context
    -> reply privately with a Mattermost ephemeral post
    -> persist summaries/preferences to Mem0 and cache summary by latest post ID

Mattermost slash command
    -> plugin handles /summarize, /threadwise-email, /threadwise in-process
    -> ephemeral result; no callback URL or separate service
```

## Features

- `@AI <question>` or `@threadwise <question>` in a conversation returns a private, ephemeral answer in that thread.
- `/summarize` summarizes up to the configured number of recent channel messages. When used in a thread, it summarizes that thread instead. The response includes a TL;DR, commitments, and open follow-ups.
- `/threadwise-email <email text>` returns a private summary and temporarily stores its draft for 24 hours.
- `/threadwise attach <post-id>` attaches the current user's email draft to that conversation's Mem0 memory.
- `/threadwise remember <preference>` stores a user-scoped preference.
- `/threadwise digest on|off` enables/disables weekday private follow-up digests.

Summaries are cached in Mattermost's plugin key-value store by requester, channel/thread, and latest post ID. Mem0 receives summaries, attached email summaries, and explicit preferences—not every message. Email text is sent to Groq but is not saved by the plugin.

## Configuration and installation

1. Build on Windows with Docker Desktop:

   ```powershell
   cd C:\OpsTree\Git\threadwise
   .\plugins\threadwise\build.ps1
   ```

   This creates `plugins\threadwise\threadwise.tar.gz` for a Linux/amd64 Mattermost server.

2. In Mattermost, enable plugin uploads if needed, then go to **System Console → Plugins → Plugin Management → Upload Plugin** and upload `threadwise.tar.gz`. Enable Threadwise.

3. Open Threadwise's plugin settings and set the Groq API key and Mem0 API key. Model, summary size, and digest timing defaults are listed below. No slash command setup is needed; the plugin registers commands globally for all teams.

   If you previously created custom slash commands for the Python sidecar, remove them after enabling the plugin. Plugin commands take precedence, but deleting the old callbacks avoids confusion.

4. Try `/summarize` in a channel, `@AI what are the open follow-ups?` in a thread, or `/threadwise-email <paste email>`.

The keys are stored in Mattermost's plugin configuration and should be entered only in the System Console. Never put API keys in the plugin archive or commit them.

## Defaults

| Setting | Default |
|---|---|
| Fast Groq model | `llama-3.1-8b-instant` |
| Summary Groq model | `llama-3.3-70b-versatile` |
| Recent messages per summary | 100 |
| Commitment follow-up threshold | 3 days |
| Inactivity threshold | 14 days |
| Weekday digest time | 09:00 UTC |

Summary requests fall back once to the fast model when the summary model is rate-limited. If Groq is out of quota, the plugin returns the cached summary when available; otherwise it asks the user to retry. It does not invent an answer.

## Memory scope

Mem0 records include platform-level `user_id` and metadata:

```json
{
  "application": "threadwise",
  "user_id": "<Mattermost user id>",
  "thread_id": "<root post id or empty for channel summary>",
  "channel_id": "<Mattermost channel id or empty for preference>",
  "entity_name": "<person, client, project, or opportunity>",
  "thread_type": "client | project | opportunity | internal",
  "memory_kind": "summary | preference | email_summary"
}
```

Retrieval filters by the requesting Mattermost user's ID, then independently accepts only Threadwise records belonging to the exact thread and channel; only that same user's preferences can cross threads. Before reading a thread, the plugin verifies the requester is a channel member. AI answers and slash-command outputs are ephemeral and private to the requester, avoiding a plugin-created bot account.

## Product questions

| Feature | Questions answered |
|---|---|
| `@AI` Q&A | What was discussed/promised? What happens next? |
| `/summarize` | What was discussed/promised? What happens next? |
| Email summary and attach | What was discussed/promised? What happens next? |
| Follow-up digests | Who needs a follow-up? Which relationships may be inactive? |
| `/threadwise remember` | What preferences should guide future follow-ups? |
