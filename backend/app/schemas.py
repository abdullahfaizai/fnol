"""
Pydantic models for the FNOL (First Notice of Loss) intake flow and the
structured events streamed to clients over chat / voice WebSockets.

The FNOLPayload mirrors the EXACT schema in the assignment (§3). Design rule:
unknown values are `null`, never hallucinated. Every collectible field
therefore defaults to None / empty so an unfinished call serialises honestly.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# FNOL payload (matches assignment §3 minimum schema)
# --------------------------------------------------------------------------- #
class Reporter(BaseModel):
    name: Optional[str] = None
    # self | spouse | family | authorized_other | unknown
    relationship_to_policyholder: Optional[str] = None
    callback_number: Optional[str] = None        # E.164
    identity_verified: Optional[bool] = None


class Geo(BaseModel):
    lat: Optional[float] = None
    lng: Optional[float] = None


class Location(BaseModel):
    raw_text: Optional[str] = None        # what the caller said
    normalized: Optional[str] = None      # geocoder's formatted address
    geo: Geo = Field(default_factory=Geo)


class Vehicle(BaseModel):
    role: Optional[str] = None            # insured | third_party
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    plate: Optional[str] = None
    vin: Optional[str] = None
    damage: Optional[str] = None
    drivable: Optional[bool] = None


class Injuries(BaseModel):
    any_injuries: Optional[bool] = None
    details: Optional[str] = None
    emergency_services_involved: Optional[bool] = None


class Police(BaseModel):
    report_filed: Optional[bool] = None
    report_number: Optional[str] = None
    department: Optional[str] = None


class OtherParty(BaseModel):
    name: Optional[str] = None
    carrier: Optional[str] = None
    policy_number: Optional[str] = None
    contact: Optional[str] = None


class Incident(BaseModel):
    datetime: Optional[str] = None        # ISO-8601 | approximate string
    location: Location = Field(default_factory=Location)
    # collision | theft | vandalism | weather | glass | animal | fire | other
    type: Optional[str] = None
    description: Optional[str] = None     # free-form, caller's words
    vehicles: list[Vehicle] = Field(default_factory=list)
    injuries: Injuries = Field(default_factory=Injuries)
    police: Police = Field(default_factory=Police)
    other_party: OtherParty = Field(default_factory=OtherParty)


class Triage(BaseModel):
    safe_location: Optional[bool] = None
    any_injuries: Optional[bool] = None
    vehicle_drivable: Optional[bool] = None


class FNOLPayload(BaseModel):
    """The structured claim record assembled during intake (assignment §3)."""
    claim_id: Optional[str] = None                 # returned by create_claim
    reported_at: Optional[str] = None              # ISO-8601
    consent_to_record: Optional[bool] = None
    reporter: Reporter = Field(default_factory=Reporter)
    policy_number: Optional[str] = None
    policyholder_name: Optional[str] = None
    incident: Incident = Field(default_factory=Incident)
    triage: Triage = Field(default_factory=Triage)
    fraud_flags: list[str] = Field(default_factory=list)
    requires_human: bool = False
    human_reason: Optional[str] = None
    next_steps_communicated: bool = False


# --------------------------------------------------------------------------- #
# Conversation / flow state
# --------------------------------------------------------------------------- #
class FlowStep(str, Enum):
    GREETING = "greeting"
    CONSENT = "consent"
    SAFETY = "safety"
    IDENTITY = "identity"
    INTAKE = "intake"
    CONFIRM = "confirm"
    SUBMITTED = "submitted"
    ENDED = "ended"


# --------------------------------------------------------------------------- #
# Streamed events (server -> client). `type` discriminates them client-side.
# --------------------------------------------------------------------------- #
class EventType(str, Enum):
    SESSION = "session"
    TOKEN = "token"            # one streamed token of the assistant reply
    MESSAGE = "message"        # a complete assistant message
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FNOL_UPDATE = "fnol_update"
    STATE = "state"
    REASONING = "reasoning"    # optional debug-panel breadcrumbs
    TTS = "tts"               # voice mode: text handed to (mock) TTS
    ERROR = "error"
    DONE = "done"             # the current turn is complete


def event(type_: EventType | str, **payload: Any) -> dict[str, Any]:
    """Tiny helper to build a JSON-serialisable event dict."""
    t = type_.value if isinstance(type_, EventType) else type_
    return {"type": t, **payload}


# --------------------------------------------------------------------------- #
# Inbound messages (client -> server)
# --------------------------------------------------------------------------- #
class InboundMessage(BaseModel):
    type: str                          # "user_text" | "audio" | "start_call" | "end"
    text: Optional[str] = None
    data: Optional[str] = None         # placeholder for base64 audio chunks


# --------------------------------------------------------------------------- #
# REST chat (non-streaming convenience endpoint)
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    step: FlowStep
    fnol: FNOLPayload
    events: list[dict[str, Any]] = Field(default_factory=list)
