from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from websockets.asyncio.client import connect

from .ai import GroqAgents, GroqQuotaExceeded
from .config import Settings
from .database import Database
from .mattermost import AccessDenied, Mattermost
from .memory import ScopedMemory
from .service import ThreadwiseService

logger = logging.getLogger(__name__)
MAX_SLASH_BYTES = 64_000


def _command_response(text: str, response_type: str = "ephemeral") -> JSONResponse:
    return JSONResponse({"response_type": response_type, "text": text})


def _parse_form(body: bytes) -> dict[str, str]:
    values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: items[-1] for key, items in values.items() if items}


def _request_value(payload: dict[str, str], *names: str) -> str:
    for name in names:
        value = payload.get(name, "").strip()
        if value:
            return value
    return ""


def _find_post_id(payload: dict[str, str], text: str) -> tuple[str, str]:
    root_id = _request_value(payload, "root_id", "rootId", "thread_id", "threadId")
    post_id = _request_value(payload, "post_id", "postId") or root_id
    if not post_id:
        candidate = text.split(maxsplit=1)[0] if text else ""
        if re.fullmatch(r"[a-z0-9]{26}", candidate):
            post_id = candidate
    return post_id, root_id


def create_app(
    settings: Settings | None = None,
    service: ThreadwiseService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    if service is None:
        mattermost = Mattermost(settings.mattermost_url, settings.mattermost_token)
        service = ThreadwiseService(
            settings=settings,
            mattermost=mattermost,
            memory=ScopedMemory(settings.mem0_api_key),
            agents=GroqAgents(settings),
            database=database,
        )

    async def websocket_listener() -> None:
        base = settings.mattermost_url
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        delay = 1
        while True:
            try:
                async with connect(
                    f"{ws_base}/api/v4/websocket",
                    additional_headers={"Authorization": f"Bearer {settings.mattermost_token}"},
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "seq": 1,
                                "action": "authentication_challenge",
                                "data": {"token": settings.mattermost_token},
                            }
                        )
                    )
                    delay = 1
                    async for raw in websocket:
                        event = json.loads(raw)
                        if event.get("event") != "posted":
                            continue
                        data = event.get("data") or {}
                        post = data.get("post")
                        if isinstance(post, str):
                            post = json.loads(post)
                        if isinstance(post, dict):
                            await run_in_threadpool(service.process_websocket_post, post)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Mattermost WebSocket disconnected; reconnecting.")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def digest_scheduler() -> None:
        try:
            zone = ZoneInfo(settings.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown TIMEZONE: {settings.timezone}") from error
        last_run_date = ""
        while True:
            now = datetime.now(zone)
            if (
                settings.enable_digests
                and now.weekday() < 5
                and now.strftime("%H:%M") >= settings.digest_time
                and last_run_date != now.date().isoformat()
            ):
                completed = await run_in_threadpool(service.run_digests, now.date().isoformat())
                if completed:
                    last_run_date = now.date().isoformat()
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate()
        await run_in_threadpool(service.initialize)
        tasks = []
        if settings.enable_websocket:
            tasks.append(asyncio.create_task(websocket_listener(), name="mattermost-websocket"))
        if settings.enable_digests:
            tasks.append(asyncio.create_task(digest_scheduler(), name="threadwise-digests"))
        yield
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        service.mm.close()

    app = FastAPI(title="Threadwise sidecar", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.database = database

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def command_request(
        request: Request, expected_command: str
    ) -> tuple[dict[str, str], str]:
        body = await request.body()
        if len(body) > MAX_SLASH_BYTES:
            raise ValueError("Slash command payload is too large.")
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.casefold() != "token" or not hmac.compare_digest(
            supplied.strip(), settings.slash_token_for(expected_command)
        ):
            raise PermissionError("Invalid slash command token.")
        payload = _parse_form(body)
        command = payload.get("command", "")
        if command and command != expected_command:
            raise PermissionError("Unexpected slash command.")
        return payload, payload.get("text", "").strip()

    @app.post("/hooks/summarize")
    async def summarize(request: Request) -> JSONResponse:
        try:
            payload, text = await command_request(request, "/summarize")
            post_id, root_id = _find_post_id(payload, text)
            channel_id = payload.get("channel_id", "")
            user_id = payload.get("user_id", "")
            if not all((post_id, channel_id, user_id)):
                return _command_response(
                    "Use `/summarize <post-id>` from the thread. Mattermost slash-command requests "
                    "do not consistently include the current thread ID."
                )
            summary, snapshot = await run_in_threadpool(
                service.summarize, post_id, channel_id, user_id
            )
            if not root_id:
                root_id = snapshot.root_id
            await run_in_threadpool(
                service.mm.post_reply, channel_id, root_id, summary
            )
            return _command_response("Thread summary posted in the conversation.")
        except PermissionError:
            logger.warning("Rejected unauthenticated Mattermost slash command.")
            return _command_response("This Mattermost command is not authorized.")
        except AccessDenied:
            return _command_response("I can't access that channel or thread.")
        except GroqQuotaExceeded:
            return _command_response("Groq is rate-limited or out of quota. Please try again later.")
        except ValueError as error:
            return _command_response(str(error))
        except Exception:
            logger.exception("Could not summarize the requested thread.")
            return _command_response("I couldn't summarize that thread. Please try again later.")

    @app.post("/hooks/threadwise-email")
    async def summarize_email(request: Request) -> JSONResponse:
        try:
            payload, text = await command_request(request, "/threadwise-email")
            user_id = payload.get("user_id", "")
            channel_id = payload.get("channel_id", "")
            if not user_id or not channel_id:
                return _command_response("Mattermost did not provide the user or channel context.")
            summary, draft_id, _ = await run_in_threadpool(
                service.summarize_email, user_id, channel_id, text
            )
            return _command_response(
                f"{summary}\n\nWant me to attach this to a thread? "
                f"Run `/threadwise attach {draft_id} <post-id>` in that thread."
            )
        except PermissionError:
            logger.warning("Rejected unauthenticated Mattermost slash command.")
            return _command_response("This Mattermost command is not authorized.")
        except GroqQuotaExceeded:
            return _command_response("Groq is rate-limited or out of quota. Please try again later.")
        except ValueError as error:
            return _command_response(str(error))
        except Exception:
            logger.exception("Could not summarize pasted email.")
            return _command_response("I couldn't summarize that email. Please try again later.")

    @app.post("/hooks/threadwise")
    async def threadwise(request: Request) -> JSONResponse:
        try:
            payload, text = await command_request(request, "/threadwise")
            user_id = payload.get("user_id", "")
            channel_id = payload.get("channel_id", "")
            parts = text.split(maxsplit=2)
            if not user_id or not parts:
                return _command_response(
                    "Try `/threadwise digest on|off`, `/threadwise remember <preference>`, "
                    "or `/threadwise attach <draft-id> <post-id>`."
                )
            operation = parts[0].casefold()
            if operation == "digest" and len(parts) >= 2 and parts[1].casefold() in {"on", "off"}:
                enabled = parts[1].casefold() == "on"
                await run_in_threadpool(service.db.set_digest_enabled, user_id, enabled)
                return _command_response(
                    "Weekday private digests enabled." if enabled else "Weekday private digests disabled."
                )
            if operation == "remember" and len(parts) == 3:
                result = await run_in_threadpool(service.remember_preference, user_id, parts[2])
                return _command_response(result)
            if operation == "attach" and len(parts) == 3:
                draft_id = parts[1]
                post_id, _ = _find_post_id(payload, parts[2])
                if not post_id or not channel_id:
                    return _command_response("Use `/threadwise attach <draft-id> <post-id>` in the target thread.")
                result = await run_in_threadpool(
                    service.attach_email, draft_id, post_id, channel_id, user_id
                )
                return _command_response(result)
            return _command_response(
                "Try `/threadwise digest on|off`, `/threadwise remember <preference>`, "
                "or `/threadwise attach <draft-id> <post-id>`."
            )
        except PermissionError:
            logger.warning("Rejected unauthenticated Mattermost slash command.")
            return _command_response("This Mattermost command is not authorized.")
        except AccessDenied:
            return _command_response("I can't access that channel or thread.")
        except ValueError as error:
            return _command_response(str(error))
        except Exception:
            logger.exception("Could not process Threadwise command.")
            return _command_response("I couldn't process that command. Please try again later.")

    return app


app = create_app()
