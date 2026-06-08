"""
Telephony seam — pluggable via an API key (assignment §6).

This module is the single place a real telephony provider (e.g. Twilio) plugs
in. Nothing here is wired to a live provider; the point is to show exactly WHERE
the key goes and HOW the audio bridges into the existing agent without
re-architecting. The agent and /ws/voice already speak in text turns + `tts`
events, so a provider only needs to translate phone audio <-> those turns.

Production flow with Twilio (Media Streams):

    PSTN call ──> Twilio number ──> TwiML <Connect><Stream> ──┐
                                                              │ (mulaw 8k frames)
                                  wss://OUR_HOST/telephony/twilio
                                                              │
        TwilioBridge:  base64 mulaw  ─►  ASR  ─►  text  ─►  agent.run_turn(text)
                       agent `tts` text  ─►  TTS  ─►  mulaw  ─►  back to Twilio

The browser `/ws/voice` path already exercises the same agent end-to-end with
mock ASR/TTS, so telephony is additive, not a rewrite.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class TelephonyConfig:
    provider: str | None = os.getenv("TELEPHONY_PROVIDER") or None
    twilio_account_sid: str | None = os.getenv("TWILIO_ACCOUNT_SID") or None
    twilio_auth_token: str | None = os.getenv("TWILIO_AUTH_TOKEN") or None
    twilio_phone_number: str | None = os.getenv("TWILIO_PHONE_NUMBER") or None

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.twilio_auth_token)


config = TelephonyConfig()


# --------------------------------------------------------------------------- #
# Provider bridge stub. Implement `handle_media_stream` to go live; it consumes
# provider audio frames and drives the SAME agent (`agent.run_turn`).
# --------------------------------------------------------------------------- #
class TwilioBridge:
    """Skeleton bridge. Real impl: ASR provider frames -> agent; agent tts -> TTS."""

    def __init__(self, agent) -> None:  # agent: BaseAgent
        if not config.enabled:
            raise RuntimeError(
                "Telephony not configured. Set TELEPHONY_PROVIDER=twilio and "
                "TWILIO_* in the environment to enable the phone bridge."
            )
        self.agent = agent

    async def handle_media_stream(self, ws) -> None:  # pragma: no cover - stub
        """
        Wire-up outline (left unimplemented for the take-home):
          1. Accept the Twilio Media Stream WebSocket.
          2. Decode inbound base64 mulaw frames and stream them to an ASR engine.
          3. On a final transcript, call `self.agent.run_turn(text)` and read the
             resulting `tts` events.
          4. Synthesize each `tts` text to mulaw and send it back to Twilio.
        Because steps 3-4 are identical to what /ws/voice already does, no agent
        changes are required to go live — only this bridge.
        """
        raise NotImplementedError("Connect a live ASR/TTS provider to go live.")
