# Threadwise Python sidecar (legacy prototype)

> Superseded by the [Go Mattermost plugin](../threadwise/plugins/threadwise/README.md). Use that plugin if you want Threadwise to run inside Mattermost without a separate Python service or manually created bot. The setup below is retained only for the earlier standalone prototype.

This legacy Python sidecar does not modify Mattermost or connect to its database. It uses a bot account and Mattermost's REST/WebSocket APIs, Groq's API, and Mem0 Platform at app.mem0.ai.

## Architecture

```text
Mattermost message / slash command
   |
   +-- @AI via bot WebSocket event
   |      -> verify requester is a member of the channel
   |      -> fetch at most MAX_THREAD_POSTS in that thread with bot token
   |      -> search Mem0 for requester only; retain only exact thread/channel
   |      -> LangChain agent (Groq), with one tool bound to that verified thread
   |      -> reply as a Mattermost bot in the same thread
   |
   +-- /summarize or /threadwise-email via authenticated slash webhook
   |      -> validate Mattermost Authorization: Token header
   |      -> summarize with LangChain/Groq; cache against latest post id
   |      -> batch one summary into user-scoped Mem0
   |      -> post thread summary or return private email summary + attach prompt
   |
   +-- weekday scheduler (opt-in per user)
          -> tracked threads/commitments in local SQLite
          -> 3-day commitment / 14-day inactivity thresholds
          -> verify membership before any thread read; send only to user's DM
```

The Docker Compose service is bound to `127.0.0.1:8080`; only Mattermost slash commands and the local host can reach it by default. From Docker Desktop, it contacts Mattermost at `http://host.docker.internal:8065`.

## Setup

### 1. Create a least-privilege bot

In Mattermost, enable bot account creation and create a regular member bot named `threadwise` (display name `Threadwise AI`). Do not grant System Admin or organization-wide channel access. Add it only to the channels where it should answer and observe conversations. Copy the bot access token.

The sidecar accepts the literal `@AI` prefix and `@threadwise` in messages visible to the bot. `@AI` is a text alias; for native Mattermost mention notifications, mention `@threadwise`.

### 2. Create integrations

Create custom slash commands in **Product menu > Integrations > Slash Commands**, each using `POST`:

| Trigger | Request URL | Purpose |
|---|---|---|
| `summarize` | `http://host.docker.internal:8080/hooks/summarize` | Summarize and post an existing thread |
| `threadwise-email` | `http://host.docker.internal:8080/hooks/threadwise-email` | Summarize pasted email privately |
| `threadwise` | `http://host.docker.internal:8080/hooks/threadwise` | Digest preferences, private memory, attach email |

Mattermost generates a separate token for each command. Copy each command's token into its matching variable in `.env`: `MATTERMOST_SUMMARIZE_SLASH_TOKEN`, `MATTERMOST_EMAIL_SLASH_TOKEN`, and `MATTERMOST_THREADWISE_SLASH_TOKEN`. Mattermost sends the token in the `Authorization: Token ...` header. For older setups using one shared token, `MATTERMOST_SLASH_TOKEN` remains supported as a fallback.

Slash command requests do not consistently include the active thread ID. Therefore `/summarize` requires a post ID: `/summarize <post-id>`. `/threadwise attach <draft-id> <post-id>` attaches a prior email summary. Use the root post ID or any post ID in the target thread.

### 3. Configure provider keys and run

From this directory:

```powershell
Copy-Item .env.example .env
# Edit .env and set the Mattermost bot token, all three slash tokens, Mem0 API key, and Groq API key.
docker compose up --build -d
Invoke-WebRequest http://localhost:8080/health
```

The Mattermost instance must be reachable from Docker at the URL in `MATTERMOST_URL`. The sidecar checks its bot token during startup. The database is a Docker named volume; no Mattermost database is mounted.

In a channel where the bot is a member:

- `@AI what did we promise in this thread?`
- `@threadwise when did we last discuss pricing?`
- `/summarize <post-id>`
- `/threadwise-email <paste email text>`
- `/threadwise attach <draft-id> <post-id>`
- `/threadwise remember I prefer concise follow-ups`
- `/threadwise digest on` or `/threadwise digest off`

Email summaries are ephemeral until explicitly attached. The pasted raw email is sent to Groq for summarization and is not stored by the sidecar; only the summary draft is stored locally until attached.

## Memory schema and privacy

Each Mem0 record has the platform-level `user_id` plus this metadata:

```json
{
  "application": "threadwise",
  "user_id": "<Mattermost requester id>",
  "thread_id": "<Mattermost root post id>",
  "channel_id": "<Mattermost channel id>",
  "entity_name": "<person/client/project when clear>",
  "thread_type": "client | project | opportunity | internal",
  "memory_kind": "summary | preference | email_summary"
}
```

Thread summaries and attached email context are user-scoped. Search first filters by Mem0 `user_id`, then the sidecar independently rejects every result unless its `application` is `threadwise` and its `thread_id` and `channel_id` exactly match the request. The only cross-thread result allowed is a `preference` for the same Mem0 user. Entity and type are descriptive, never authorization. Bot visibility is not treated as user authorization: before reading a thread the sidecar also checks that the requesting Mattermost user is a channel member.

This strict per-user partition prevents memories from one person's private context from influencing another person's answer. It means a teammate does not automatically see another teammate's saved memories, even in a shared thread.

Memory writes occur once per thread summary, email attachment, or explicit `/threadwise remember` command—not per post. Summary cache keys are `(user_id, thread_id)` and store the newest Mattermost post ID, so an unchanged thread avoids another Groq summary call.

## Groq limits and fallback

Q&A defaults to `llama-3.1-8b-instant`; summaries try `llama-3.3-70b-versatile` first, then fall back once to the fast model on HTTP 429/token quota responses. Model IDs can be changed with `GROQ_FAST_MODEL` and `GROQ_SUMMARY_MODEL`; confirm current model availability and free-tier limits in your Groq console.

If quota is exhausted, Threadwise returns the latest user-scoped cached summary when available; otherwise it clearly asks the user to retry later. It does not fabricate an answer or repeatedly retry. Other provider errors are logged without message content or keys and return a generic user-facing failure.

## Follow-up digests

Digests default to weekdays at 09:00 in `TIMEZONE`, use the confirmed 3-day commitment and 14-day inactivity thresholds, and are opt-in per user (`/threadwise digest on`). The bot privately DMs a digest only when there are items. It reports an open action as stale only when no newer thread activity is observed after the summary that detected it. This is intentionally a lightweight heuristic; a new message can be unrelated to the action.

Activity is learned from Mattermost WebSocket `posted` events while the sidecar is running. Consequently, inactivity detection is strongest for threads the bot observed and that have been summarized at least once. The service does not crawl the full Mattermost history.

## Core question mapping

| Feature | Questions answered |
|---|---|
| `@AI` thread Q&A | What was discussed/promised? What happens next? |
| `/summarize` + persistent summary | What was discussed/promised? What happens next? |
| `/threadwise-email` + explicit attach | What was discussed/promised? What happens next? |
| Weekday digest | Who needs a follow-up? Which relationships are going inactive? |
| `/threadwise remember` | What context/preferences should guide future follow-ups? |

## Security notes

- Keep `.env` out of source control; rotate any key that was accidentally exposed.
- Prefer a dedicated bot with channel membership limited to the intended scope. The server requires the bot to be able to read a channel and the requesting user to be a channel member.
- The integration token authenticates slash-command callbacks. Do not expose port 8080 to untrusted networks.
- Email/thread text is sent to Groq, and saved summaries/preferences are sent to Mem0 Platform. Use only data approved for those providers.
- SQLite stores summaries, email-summary drafts, thread activity, and owner/action metadata. Protect the Docker volume and backups.
