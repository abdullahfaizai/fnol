"""
WebSocket transport for chat and voice.

Both endpoints are thin: they own connection lifecycle and (for voice) the
mock ASR/TTS framing, then delegate every turn to the SAME shared agent
(`agent.run_turn`). This is what makes "one backend powers chat + voice" true.

Routes
  /ws/chat   - inbound {"type":"user_text","text": "..."} ; streams agent events.
  /ws/voice  - same, plus {"type":"start_call"} to trigger the greeting and
               {"type":"audio", ...} placeholders for real ASR later. Assistant
               messages are echoed as `tts` events (text handed to a TTS engine).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .agent import BaseAgent, make_agent
from .schemas import EventType as ET, event

log = logging.getLogger("meridian.ws")
router = APIRouter()


class SessionManager:
    """In-memory session store. Swap for Redis to scale horizontally."""

    def __init__(self) -> None:
        self._sessions: dict[str, BaseAgent] = {}

    def get_or_create(self, session_id: str | None) -> tuple[str, BaseAgent]:
        if session_id and session_id in self._sessions:
            return session_id, self._sessions[session_id]
        sid = session_id or uuid.uuid4().hex
        agent = make_agent(sid)
        self._sessions[sid] = agent
        return sid, agent

    def get(self, session_id: str) -> BaseAgent | None:
        return self._sessions.get(session_id)

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


sessions = SessionManager()


async def _pump(
    ws: WebSocket, events: AsyncIterator[dict], voice: bool = False
) -> None:
    """Forward agent events to the socket, adding TTS frames in voice mode."""
    async for ev in events:
        await ws.send_text(json.dumps(ev))
        if voice and ev.get("type") == ET.MESSAGE.value and ev.get("text"):
            await ws.send_text(json.dumps(event(ET.TTS, text=ev["text"])))


async def _handle(ws: WebSocket, voice: bool) -> None:
    await ws.accept()
    session_id: str | None = None
    agent: BaseAgent | None = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                msg = {"type": "user_text", "text": raw}

            # Lazily bind a session; client may pass its own session_id.
            if agent is None:
                session_id, agent = sessions.get_or_create(msg.get("session_id"))
                await ws.send_text(json.dumps(
                    event(ET.SESSION, session_id=session_id, mode="voice" if voice else "chat")
                ))

            mtype = msg.get("type", "user_text")

            if mtype == "start_call":
                await _pump(ws, agent.greeting(), voice=voice)
            elif mtype == "audio":
                # Placeholder for real streaming ASR. Mock: ack the chunk only.
                await ws.send_text(json.dumps(event(ET.REASONING, text="audio chunk received (mock ASR)")))
            elif mtype == "end":
                await ws.send_text(json.dumps(event(ET.DONE)))
                break
            else:  # user_text
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                await _pump(ws, agent.run_turn(text), voice=voice)
    except WebSocketDisconnect:
        log.info("client disconnected (session=%s)", session_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("ws handler error")
        try:
            await ws.send_text(json.dumps(event(ET.ERROR, message=str(exc))))
        except Exception:  # noqa: BLE001
            pass


@router.websocket("/ws/chat")
async def ws_chat(ws: WebSocket) -> None:
    await _handle(ws, voice=False)


@router.websocket("/ws/voice")
async def ws_voice(ws: WebSocket) -> None:
    await _handle(ws, voice=True)
