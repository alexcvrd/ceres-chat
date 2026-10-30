"""ceres-chat server: serves the page and forwards questions to Ceres with the API key added here, never in the browser.

Routes: GET / (the page) · GET /api/status · POST /api/ask · POST /api/feedback. Every exchange and every feedback is appended
to JSONL under the runtime dir. Spec: SPEC.md.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config, load

STATIC = Path(__file__).resolve().parent / "static"
MAX_QUESTION_CHARS = 2000   # Ceres api.max_question_chars; checked here so a bad request never reaches the GPU


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    lang: Literal["en", "ro"]
    # Ceres memory v0: the page keeps one conversation id per chat and an optional persona (validated here, before any GPU call)
    conversation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    profile_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")


class FeedbackIn(BaseModel):
    exchange_id: str = Field(pattern=r"^[0-9a-f]{32}$")   # the format ask() generates (uuid4 hex)
    rating: Literal["up", "down"]
    comment: str = Field(default="", max_length=4000)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Journal:
    """Append-only JSONL files; one lock, so concurrent requests never interleave a line."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.Lock()

    def append(self, name: str, row: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self.lock, open(self.root / name, "a", encoding="utf-8") as f:
            f.write(line)

    def has_exchange(self, exchange_id: str) -> bool:
        path = self.root / "exchanges.jsonl"
        if not path.is_file():
            return False
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("exchange_id") == exchange_id:
                    return True
        return False


def _error(kind: str, status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"kind": kind, "status": status, "detail": detail}})


def create_app(config: Config | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    """`transport` lets tests put a fake Ceres behind the real client."""
    config = config or load()
    journal = Journal(config.runtime_dir)
    headers = {"X-API-Key": config.api_key} if config.api_key else {}
    client = httpx.Client(base_url=config.ceres_url, headers=headers, timeout=config.timeout_s, transport=transport)
    app = FastAPI(title="ceres-chat", docs_url=None, redoc_url=None)

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        try:
            health = client.get("/health", timeout=5).json()
        except (httpx.HTTPError, ValueError):
            return {"ceres": "down", "url": config.ceres_url}
        out: dict[str, Any] = {"ceres": "ready" if health.get("ready") else "not_ready", "health": health, "url": config.ceres_url}
        try:
            info = client.get("/v1/info", timeout=5)
            out["info"] = info.json() if info.status_code == 200 else None
            if info.status_code == 401:
                out["ceres"] = "key_rejected"
        except (httpx.HTTPError, ValueError):
            out["info"] = None
        return out

    @app.post("/api/ask")
    def ask(body: AskIn) -> Any:
        exchange_id = uuid.uuid4().hex
        started = time.monotonic()
        row: dict[str, Any] = {"exchange_id": exchange_id, "at": _now(), "question": body.question, "lang": body.lang,
                               **({"conversation_id": body.conversation_id} if body.conversation_id else {}),
                               **({"profile_id": body.profile_id} if body.profile_id else {})}
        payload = {"question": body.question, "lang": body.lang, "request_id": f"ceres-chat-{exchange_id}",
                   **({"conversation_id": body.conversation_id} if body.conversation_id else {}),
                   **({"profile_id": body.profile_id} if body.profile_id else {})}
        try:
            resp = client.post("/v1/ask", json=payload)
        except httpx.TimeoutException:
            result = _error("timeout", 504, f"Ceres did not answer within {config.timeout_s:.0f} s")
        except httpx.HTTPError as exc:
            result = _error("ceres_down", 502, f"Ceres is not reachable at {config.ceres_url} ({type(exc).__name__})")
        else:
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    result = JSONResponse(content={"exchange_id": exchange_id, "ceres": payload})
                    row["ceres"] = payload
                else:
                    result = _error("bad_reply", 502, "Ceres sent a reply that is not a JSON object")
            elif resp.status_code == 401:
                result = _error("key_rejected", 502, "Ceres rejected the chat server's API key (check ceres.api_key_env / api_key_file)")
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                detail = body.get("detail", resp.text) if isinstance(body, dict) else resp.text
                result = _error("ceres_error", resp.status_code, str(detail)[:2000])
        if "ceres" not in row:
            row["error"] = json.loads(result.body)["error"]
        row["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        journal.append("exchanges.jsonl", row)
        return result

    @app.get("/api/profiles")
    def profiles() -> dict[str, Any]:
        """Ceres's personas (memory v0); an empty list when Ceres is down or memory is off."""
        try:
            r = client.get("/v1/profiles", timeout=5)
            return r.json() if r.status_code == 200 else {"memory_enabled": False, "profiles": []}
        except (httpx.HTTPError, ValueError):
            return {"memory_enabled": False, "profiles": []}

    @app.post("/api/feedback")
    def feedback(body: FeedbackIn) -> dict[str, str]:
        if not journal.has_exchange(body.exchange_id):
            raise HTTPException(status_code=404, detail="unknown exchange_id")
        journal.append("feedback.jsonl", {"at": _now(), **body.model_dump()})
        return {"status": "saved"}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.client = client
    return app


def main() -> None:
    import uvicorn
    config = load()
    if not config.api_key:
        print("warning: no Ceres API key found (ceres.api_key_env / api_key_file); /v1/ask will be rejected")
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
