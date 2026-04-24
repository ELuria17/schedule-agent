"""Minimal fake orchestrator used only for the E2E setup-flow test.

The real orchestrator imports schedule_config, which instantiates every
configured provider and needs real credentials. For end-to-end testing
of the setup_server → orchestrator handoff, we only care that something
binds :8787 and answers `/hub` — not that it can actually plan anything.

This fake binds :8787, serves a tiny `/hub` endpoint, and exits when
killed. scripts/e2e_setup_flow.py points
`SCHEDULE_AGENT_ORCHESTRATOR_CMD` at this file to stand in for the real
orchestrator during the test.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


app = FastAPI()


@app.get("/hub", response_class=HTMLResponse)
async def hub(key: Optional[str] = None):
    """Mirror the real orchestrator's /hub route shape for assertion
    purposes. The E2E script only checks for a 200; body is cosmetic."""
    return HTMLResponse(
        "<!doctype html><html><head><title>fake hub</title></head>"
        "<body><h1>FAKE ORCHESTRATOR · hub route ok</h1>"
        f"<p>key received: {'yes' if key else 'no'}</p></body></html>"
    )


@app.head("/hub")
async def hub_head(key: Optional[str] = None):
    """The setup-server success page polls with HEAD requests. Answering
    those is the whole point of this fake."""
    return HTMLResponse("", status_code=200)


@app.get("/_fake_marker")
async def marker():
    """Lets the E2E script distinguish the fake from any other process
    that might have grabbed :8787."""
    return JSONResponse({"fake": True, "pid": os.getpid()})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("SCHEDULE_AGENT_FAKE_PORT", "8787"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
