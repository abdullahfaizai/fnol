"""
Meridian Auto Insurance - FNOL Voice/Chat Agent API.

Run (dev):   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .agent import OPENAI_MODEL, use_mock_llm
from .schemas import ChatRequest, ChatResponse, EventType as ET, FlowStep
from .tools import OPENAI_TOOL_SCHEMAS
from .websocket import router as ws_router, sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("meridian.api")

app = FastAPI(title="Meridian FNOL Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    dur = (time.perf_counter() - start) * 1000
    log.info("%s %s -> %s (%.0f ms)", request.method, request.url.path,
             response.status_code, dur)
    return response


app.include_router(ws_router)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "mode": "scripted" if use_mock_llm() else "llm",
        "model": None if use_mock_llm() else OPENAI_MODEL,
    }


@app.get("/tools")
async def list_tools() -> dict:
    return {"tools": [t["function"]["name"] for t in OPENAI_TOOL_SCHEMAS]}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """
    Non-streaming convenience endpoint. Drains the SAME shared agent the
    WebSocket uses and returns the assembled reply + collected events.
    """
    session_id, agent = sessions.get_or_create(req.session_id)

    # First message on a fresh session: greet, then process the message.
    events: list[dict] = []
    if not getattr(agent, "_greeted", False) and not req.session_id:
        async for ev in agent.greeting():
            events.append(ev)
        setattr(agent, "_greeted", True)

    async for ev in agent.run_turn(req.message):
        events.append(ev)

    reply = " ".join(
        ev["text"] for ev in events
        if ev.get("type") == ET.MESSAGE.value and ev.get("text")
    ).strip()

    return ChatResponse(
        session_id=session_id,
        reply=reply or "(no text response)",
        step=FlowStep(agent.step.value),
        fnol=agent.fnol,
        events=events,
    )


@app.get("/")
async def root() -> dict:
    return {"service": "Meridian FNOL Agent", "docs": "/docs", "health": "/health"}
