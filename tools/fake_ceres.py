"""A stand-in Ceres for checking the page without the GPU: /health, /v1/info, and /v1/ask replying with the recorded fixtures
(questions containing "2008" or "yield" get the UNSUPPORTED one, the rest the ANSWER one; "slow" waits 3 s; "busy" returns 429).
Usage: .venv/bin/python tools/fake_ceres.py [port]   (key: any)."""
import json
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
app = FastAPI()


@app.get("/health")
def health():
    return {"api": "ok", "ready": True, "store": "ready", "model": "ready", "embedder": "ready"}


@app.get("/v1/info")
def info():
    return {"release_attempt_id": "all/release-attempt-0001 (fake)", "route_id": "a6f94ada3ea20a0f2d11bd4a737dbd95cbc6a8ae3ff312ca93af9552261972bf"}


@app.post("/v1/ask")
async def ask(req: Request):
    q = (await req.json())["question"].lower()
    if "busy" in q:
        return JSONResponse(status_code=429, content={"detail": "busy: one answer at a time (fake)"})
    if "slow" in q:
        time.sleep(3)
    name = "ask-unsupported.json" if ("2008" in q or "yield" in q) else "ask-answer.json"
    return json.loads((FIX / name).read_text())


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8399)
