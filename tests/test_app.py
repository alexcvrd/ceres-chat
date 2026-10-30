"""ceres-chat server tests: a fake Ceres (httpx.MockTransport) answering with recorded Ceres responses (tests/fixtures:
ask-answer.json = ANSWER, ask-unsupported.json = UNSUPPORTED). No GPU, no network.

How the server can fail, and the test that catches each:
  * the API key reaches the browser (page, status, ask, error bodies)                   → test_key_never_in_browser_responses
  * the key is not sent to Ceres (every ask 401s)                                        → test_key_sent_to_ceres
  * the key lands in the exchange log                                                     → test_key_never_logged
  * a bad request (empty, too long, wrong lang) still costs a GPU call                    → test_bad_request_never_reaches_ceres
  * a decline (UNSUPPORTED) is altered or treated as an error                             → test_decline_passes_through_unchanged
  * Ceres down / timing out / rejecting the key shows a generic 500 or hangs              → test_ceres_down, test_timeout, test_key_rejected
  * a Ceres 4xx (429 busy, 413) loses its detail                                          → test_ceres_error_detail_kept
  * an exchange is not logged (answers and errors alike), so a test session is lost       → test_every_exchange_logged
  * feedback for an exchange that never happened is accepted, or bad ratings pass         → test_feedback_rules
  * status claims "ready" when Ceres is not                                               → test_status_states
Added for Ceres memory v0:
  * conversation/persona never reach Ceres, or a stateless ask grows memory fields        → test_memory_fields_forwarded
  * a malformed conversation id still costs a GPU call                                    → test_bad_request_never_reaches_ceres
  * the persona list breaks the page when Ceres is down (backend JSON only; the page is not tested) → test_profiles
Added after the outside review:
  * a non-JSON 200 or a non-object error body from Ceres crashes the route / skips the log → test_malformed_ok_reply_is_502_and_logged,
                                                                                            test_non_object_error_body_is_logged
  * feedback accepted for an id that only appears inside another field of a logged exchange → test_feedback_matches_exchange_id_exactly
  * a recorded follow-up (memory) reply is altered on the way to the page                  → test_memory_followup_passes_through
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ceres_chat.app import Journal, create_app
from ceres_chat.config import Config

FIX = Path(__file__).resolve().parent / "fixtures"
ANSWER = json.loads((FIX / "ask-answer.json").read_text())
UNSUPPORTED = json.loads((FIX / "ask-unsupported.json").read_text())
KEY = "test-key-0123456789abcdef-should-never-leak"


class FakeCeres:
    def __init__(self, ask_reply=None, ask_status=200, health=None, down=False, timeout=False):
        self.calls: list[httpx.Request] = []
        self.ask_reply, self.ask_status, self.down, self.timeout = ask_reply or ANSWER, ask_status, down, timeout
        self.health = health or {"api": "ok", "ready": True, "store": "ready", "model": "ready", "embedder": "ready"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        if self.timeout and request.url.path == "/v1/ask":
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.path == "/health":
            return httpx.Response(200, json=self.health)
        if request.url.path == "/v1/info":
            if request.headers.get("X-API-Key") != KEY:
                return httpx.Response(401, json={"detail": "missing or invalid X-API-Key"})
            return httpx.Response(200, json={"release_attempt_id": "all/release-attempt-0001", "route_id": "a6f94ada3ea20a0f2d11bd4a737dbd95cbc6a8ae3ff312ca93af9552261972bf"})
        if request.url.path == "/v1/profiles":
            return httpx.Response(200, json={"memory_enabled": True, "profiles": [{"id": "dolj-maize-dry", "role": "farmer",
                                                                                   "language": "ro", "summary": "Farmer in Dolj"}]})
        if request.url.path == "/v1/ask":
            if self.ask_status != 200:
                return httpx.Response(self.ask_status, json={"detail": f"ceres said {self.ask_status}"})
            return httpx.Response(200, json=self.ask_reply)
        return httpx.Response(404)


def make(tmp_path: Path, fake: FakeCeres, key: str = KEY) -> TestClient:
    cfg = Config(ceres_url="http://ceres.test", api_key=key, timeout_s=5, runtime_dir=tmp_path / "rt")
    return TestClient(create_app(cfg, transport=httpx.MockTransport(fake)))


def log_rows(tmp_path: Path, name: str = "exchanges.jsonl") -> list[dict]:
    p = tmp_path / "rt" / name
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_key_never_in_browser_responses(tmp_path):
    """Bug: the Ceres key appears in anything the browser can read."""
    fake = FakeCeres()
    c = make(tmp_path, fake)
    bodies = [c.get("/").text, c.get("/static/app.js").text, c.get("/api/status").text,
              c.post("/api/ask", json={"question": "q", "lang": "en"}).text]
    c2 = make(tmp_path, FakeCeres(ask_status=401))
    bodies.append(c2.post("/api/ask", json={"question": "q", "lang": "en"}).text)
    assert all(KEY not in b for b in bodies)


def test_key_sent_to_ceres(tmp_path):
    """Bug: the forwarded request lacks X-API-Key, so Ceres rejects every question."""
    fake = FakeCeres()
    make(tmp_path, fake).post("/api/ask", json={"question": "q", "lang": "ro"})
    ask = [r for r in fake.calls if r.url.path == "/v1/ask"][0]
    assert ask.headers["X-API-Key"] == KEY
    body = json.loads(ask.content)
    assert body["question"] == "q" and body["lang"] == "ro" and body["request_id"].startswith("ceres-chat-")
    assert set(body) == {"question", "lang", "request_id"}        # Ceres AskRequest forbids extra fields


def test_key_never_logged(tmp_path):
    """Bug: the exchange or feedback log carries the key."""
    c = make(tmp_path, FakeCeres())
    ex = c.post("/api/ask", json={"question": "q", "lang": "en"}).json()["exchange_id"]
    c.post("/api/feedback", json={"exchange_id": ex, "rating": "up"})
    make(tmp_path, FakeCeres(down=True)).post("/api/ask", json={"question": "q", "lang": "en"})
    for f in (tmp_path / "rt").iterdir():
        assert KEY not in f.read_text()


@pytest.mark.parametrize("body", [{"question": "", "lang": "en"}, {"question": "x" * 2001, "lang": "en"},
                                  {"question": "q", "lang": "de"}, {"question": "q"},
                                  {"question": "q", "lang": "en", "conversation_id": "short"},
                                  {"question": "q", "lang": "en", "profile_id": "Bad Persona"}])
def test_bad_request_never_reaches_ceres(tmp_path, body):
    """Bug: an invalid question is forwarded, wasting a ~15 s GPU answer on a 422."""
    fake = FakeCeres()
    r = make(tmp_path, fake).post("/api/ask", json=body)
    assert r.status_code == 422
    assert not [x for x in fake.calls if x.url.path == "/v1/ask"]


def test_decline_passes_through_unchanged(tmp_path):
    """Bug: an UNSUPPORTED answer is rewritten or reported as an error (declines are a normal Ceres outcome)."""
    r = make(tmp_path, FakeCeres(ask_reply=UNSUPPORTED)).post("/api/ask", json={"question": "q", "lang": "en"})
    assert r.status_code == 200
    assert r.json()["ceres"] == UNSUPPORTED


def test_ceres_down(tmp_path):
    """Bug: Ceres not running gives a 500 or a hang instead of a clear 'down'."""
    r = make(tmp_path, FakeCeres(down=True)).post("/api/ask", json={"question": "q", "lang": "en"})
    assert r.status_code == 502 and r.json()["error"]["kind"] == "ceres_down"


def test_timeout(tmp_path):
    """Bug: a Ceres that never answers hangs the page or shows a generic error."""
    r = make(tmp_path, FakeCeres(timeout=True)).post("/api/ask", json={"question": "q", "lang": "en"})
    assert r.status_code == 504 and r.json()["error"]["kind"] == "timeout"


def test_key_rejected(tmp_path):
    """Bug: a wrong key looks like a Ceres failure instead of a config problem."""
    r = make(tmp_path, FakeCeres(ask_status=401)).post("/api/ask", json={"question": "q", "lang": "en"})
    assert r.json()["error"]["kind"] == "key_rejected"


@pytest.mark.parametrize("status", [413, 429, 503])
def test_ceres_error_detail_kept(tmp_path, status):
    """Bug: Ceres's own reason (busy 429, too long 413, not ready 503) is lost."""
    r = make(tmp_path, FakeCeres(ask_status=status)).post("/api/ask", json={"question": "q", "lang": "en"})
    assert r.status_code == status
    assert r.json()["error"] == {"kind": "ceres_error", "status": status, "detail": f"ceres said {status}"}


def test_every_exchange_logged(tmp_path):
    """Bug: an answer or an error is not written to exchanges.jsonl, so a test session cannot be reviewed."""
    make(tmp_path, FakeCeres()).post("/api/ask", json={"question": "a?", "lang": "en"})
    make(tmp_path, FakeCeres(down=True)).post("/api/ask", json={"question": "b?", "lang": "ro"})
    rows = log_rows(tmp_path)
    assert [r["question"] for r in rows] == ["a?", "b?"]
    assert rows[0]["ceres"] == ANSWER and "error" not in rows[0]
    assert rows[1]["error"]["kind"] == "ceres_down" and "ceres" not in rows[1]
    assert all(r["exchange_id"] and r["latency_ms"] >= 0 for r in rows)


def test_feedback_rules(tmp_path):
    """Bug: feedback for an exchange that never happened is stored, or an invalid rating is accepted."""
    c = make(tmp_path, FakeCeres())
    ex = c.post("/api/ask", json={"question": "q", "lang": "en"}).json()["exchange_id"]
    assert c.post("/api/feedback", json={"exchange_id": "0" * 32, "rating": "up"}).status_code == 404
    assert c.post("/api/feedback", json={"exchange_id": ex, "rating": "meh"}).status_code == 422
    assert c.post("/api/feedback", json={"exchange_id": ex, "rating": "down", "comment": "wrong book"}).status_code == 200
    rows = log_rows(tmp_path, "feedback.jsonl")
    assert len(rows) == 1 and rows[0]["exchange_id"] == ex and rows[0]["rating"] == "down" and rows[0]["comment"] == "wrong book"


def test_status_states(tmp_path):
    """Bug: the status badge says ready while Ceres is down, not ready, or rejecting the key."""
    assert make(tmp_path, FakeCeres()).get("/api/status").json()["ceres"] == "ready"
    assert make(tmp_path, FakeCeres(down=True)).get("/api/status").json()["ceres"] == "down"
    nr = FakeCeres(health={"api": "ok", "ready": False, "store": "route_store_mismatch", "model": "down"})
    assert make(tmp_path, nr).get("/api/status").json()["ceres"] == "not_ready"
    assert make(tmp_path, FakeCeres(), key="wrong").get("/api/status").json()["ceres"] == "key_rejected"


def test_memory_fields_forwarded(tmp_path):
    """Bug: the page's conversation id / persona never reach Ceres, or a stateless ask suddenly carries them."""
    fake = FakeCeres()
    c = make(tmp_path, fake)
    c.post("/api/ask", json={"question": "And for maize?", "lang": "en", "conversation_id": "c-0123456789", "profile_id": "dolj-maize-dry"})
    c.post("/api/ask", json={"question": "q", "lang": "en"})
    asks = [json.loads(r.content) for r in fake.calls if r.url.path == "/v1/ask"]
    assert asks[0]["conversation_id"] == "c-0123456789" and asks[0]["profile_id"] == "dolj-maize-dry"
    assert "conversation_id" not in asks[1] and "profile_id" not in asks[1]
    assert log_rows(tmp_path)[0]["conversation_id"] == "c-0123456789"


def test_profiles(tmp_path):
    """Bug: /api/profiles errors when Ceres is down instead of returning an empty list, or never lists Ceres's personas
    (backend JSON only; no browser test covers the page)."""
    assert make(tmp_path, FakeCeres()).get("/api/profiles").json()["profiles"][0]["id"] == "dolj-maize-dry"
    assert make(tmp_path, FakeCeres(down=True)).get("/api/profiles").json() == {"memory_enabled": False, "profiles": []}


def make_raw(tmp_path: Path, handler) -> TestClient:
    cfg = Config(ceres_url="http://ceres.test", api_key=KEY, timeout_s=5, runtime_dir=tmp_path / "rt")
    return TestClient(create_app(cfg, transport=httpx.MockTransport(handler)))


def test_malformed_ok_reply_is_502_and_logged(tmp_path):
    """Bug: a 200 from Ceres with a non-JSON (or non-object) body crashes the route: no structured error, nothing journaled."""
    for body in (httpx.Response(200, content=b"<html>proxy</html>"), httpx.Response(200, json=["not", "an", "object"])):
        c = make_raw(tmp_path, lambda req, body=body: body)
        r = c.post("/api/ask", json={"question": "q", "lang": "en"})
        assert r.status_code == 502 and r.json()["error"]["kind"] == "bad_reply"
    rows = log_rows(tmp_path)
    assert len(rows) == 2 and all(x["error"]["kind"] == "bad_reply" and "ceres" not in x for x in rows)


def test_non_object_error_body_is_logged(tmp_path):
    """Bug: a 4xx/5xx with a JSON list body raises AttributeError (.get on a list): generic 500, exchange not journaled."""
    c = make_raw(tmp_path, lambda req: httpx.Response(503, json=["down"]))
    r = c.post("/api/ask", json={"question": "q", "lang": "en"})
    assert r.status_code == 503 and r.json()["error"]["kind"] == "ceres_error"
    assert log_rows(tmp_path)[0]["error"]["status"] == 503


def test_feedback_matches_exchange_id_exactly(tmp_path):
    """Bug: has_exchange() is a substring search, so an id found inside a question or another field is accepted as an exchange."""
    c = make(tmp_path, FakeCeres())
    real = c.post("/api/ask", json={"question": "q", "lang": "en"}).json()["exchange_id"]
    crafted = "a" * 32
    c.post("/api/ask", json={"question": f'"exchange_id": "{crafted}"', "lang": "en"})   # the id text now sits inside a logged question
    assert c.post("/api/feedback", json={"exchange_id": crafted, "rating": "up"}).status_code == 404
    assert c.post("/api/feedback", json={"exchange_id": real[:12], "rating": "up"}).status_code == 422
    assert c.post("/api/feedback", json={"exchange_id": real, "rating": "up"}).status_code == 200



def test_exchange_lookup_ignores_an_id_in_a_nested_field(tmp_path):
    """Bug: has_exchange() searched each journal line for the text `"exchange_id": "<id>"`, so an id that appears only in
    a nested field (here a recorded upstream body) counted as a logged exchange."""
    nested = "b" * 32
    (tmp_path / "exchanges.jsonl").write_text(json.dumps({"exchange_id": "a" * 32, "ceres": {"exchange_id": nested}}) + "\n",
                                              encoding="utf-8")
    journal = Journal(tmp_path)
    assert journal.has_exchange("a" * 32)
    assert not journal.has_exchange(nested)

def test_memory_followup_passes_through(tmp_path):
    """Bug: a follow-up (memory) reply recorded from Ceres is altered, dropped or mislogged on its way to the page."""
    followup = json.loads((FIX / "ask-memory-followup.json").read_text())
    c = make(tmp_path, FakeCeres(ask_reply=followup))
    r = c.post("/api/ask", json={"question": "And for maize?", "lang": "en", "conversation_id": "c-0123456789"})
    assert r.status_code == 200 and r.json()["ceres"] == followup
    assert log_rows(tmp_path)[0]["ceres"] == followup
