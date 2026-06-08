"""
Mock backend tools for the FNOL agent.

Each tool reproduces the latency / failure behaviour described in the
assignment (§5.1) and is seeded with the policy database from §5.2:

  Tool                    Returns                 Behaviour
  lookup_policy           policy record           not_found
  verify_caller_identity  verified                failed
  geocode_location        {normalized, lat, lng}  ambiguous[list]
  check_existing_claims   list of claims          p95 ~ 600 ms
  create_claim            {claim_id}              error
  send_sms                sent                    failed
  schedule_callback       confirmation            p95 ~ 400 ms
  transfer_to_human       handoff_ack             expects a context summary

The module also exposes:
  * OPENAI_TOOL_SCHEMAS  -> JSON schemas for OpenAI function calling
  * dispatch(name, args) -> runs a tool by name with retry + timing metadata
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Any, Awaitable, Callable

log = logging.getLogger("meridian.tools")


# --------------------------------------------------------------------------- #
# Latency model — per-tool, so p95 targets in the spec are honoured.
# --------------------------------------------------------------------------- #
# (min_ms, max_ms): a uniform draw whose upper bound is the rough p95.
_LATENCY = {
    "lookup_policy": (250, 550),
    "verify_caller_identity": (250, 550),
    "geocode_location": (200, 500),
    "check_existing_claims": (350, 600),     # p95 ~ 600 ms
    "create_claim": (400, 800),
    "send_sms": (150, 400),
    "schedule_callback": (200, 400),         # p95 ~ 400 ms
    "transfer_to_human": (150, 350),
}

# Tools that may randomly fail with a transient backend error (retry demo).
_FLAKY = {"create_claim": 0.12, "send_sms": 0.12, "check_existing_claims": 0.05}


class ToolError(RuntimeError):
    """Raised when a mock tool's simulated backend fails transiently."""


async def _simulate_io(name: str) -> None:
    lo, hi = _LATENCY.get(name, (300, 800))
    await asyncio.sleep(random.uniform(lo / 1000.0, hi / 1000.0))
    rate = _FLAKY.get(name)
    if rate and random.random() < rate:
        raise ToolError(f"Upstream service for '{name}' timed out (simulated)")


# --------------------------------------------------------------------------- #
# Mock policy database (assignment §5.2)
# --------------------------------------------------------------------------- #
_MOCK_POLICIES: dict[str, dict[str, Any]] = {
    "POL100234": {
        "policy_number": "POL100234",
        "policyholder_name": "John Park",
        "status": "active",
        "coverage": ["comprehensive", "collision", "rental"],
        "verify_dob": "1990-04-12",
        "vehicles": [
            {"year": 2021, "make": "Honda", "model": "Civic", "plate": "7KQM412", "vin": None},
        ],
    },
    "POL100567": {
        "policy_number": "POL100567",
        "policyholder_name": "Ayesha Qureshi",
        "status": "active",
        "coverage": ["collision"],  # no rental, no comprehensive
        "verify_dob": "1985-11-30",
        "vehicles": [
            {"year": 2019, "make": "Toyota", "model": "Corolla", "plate": "8BVN905", "vin": None},
        ],
    },
    "POL100912": {
        "policy_number": "POL100912",
        "policyholder_name": "Robert Vance",
        "status": "lapsed",        # expired 24 days ago
        "lapsed_days": 24,
        "coverage": [],
        "verify_dob": "1978-02-03",
        "vehicles": [
            {"year": 2018, "make": "Ford", "model": "F-150", "plate": None, "vin": None,
             "note": "commercial use"},
        ],
    },
    "POL101345": {
        "policy_number": "POL101345",
        "policyholder_name": "Maria Delgado",
        "status": "active",
        "coverage": ["full"],  # full on both vehicles
        "verify_dob": "1992-07-19",
        "vehicles": [
            {"year": 2020, "make": "Subaru", "model": "Outback", "plate": None, "vin": None},
            {"year": 2016, "make": "Honda", "model": "CR-V", "plate": None, "vin": None},
        ],
    },
    "POL101678": {
        "policy_number": "POL101678",
        "policyholder_name": "D'Angelo O'Brien",
        "status": "active",
        "coverage": ["collision", "comprehensive"],  # high deductible
        "verify_dob": "1995-09-08",
        "vehicles": [
            {"year": 2022, "make": "Kia", "model": "Telluride", "plate": None,
             "vin": "5XYP3DHC9NG114882"},
        ],
    },
}


def _norm_pol(n: str | None) -> str:
    """Accept 'POL100234', 'POL-100234', 'pol 100234' -> 'POL100234'."""
    if not n:
        return ""
    return n.upper().replace("-", "").replace(" ", "")


def _public_policy(rec: dict[str, Any]) -> dict[str, Any]:
    """A policy view that NEVER leaks the verification secret (DOB)."""
    out = {k: v for k, v in rec.items() if k != "verify_dob"}
    return out


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
async def lookup_policy(
    policy_number: str | None = None,
    name: str | None = None,
    dob: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Look up a policy. Returns {found:false, reason:'not_found'} when unknown."""
    await _simulate_io("lookup_policy")
    rec = _MOCK_POLICIES.get(_norm_pol(policy_number))
    if not rec and name:
        rec = next(
            (r for r in _MOCK_POLICIES.values()
             if r["policyholder_name"].lower() == name.strip().lower()),
            None,
        )
    if not rec:
        return {"found": False, "reason": "not_found", "policy_number": policy_number}
    return {"found": True, **_public_policy(rec)}


async def verify_caller_identity(
    policy_number: str | None = None,
    name: str | None = None,
    dob: str | None = None,
    address: str | None = None,
    phone: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """
    Verify a caller against the policy using name + ONE second factor.
    Only `dob` is checkable against the seed; address/phone are accepted as a
    provided-but-unverifiable factor (mock) and reported as 'weak'.
    Returns verified=false on mismatch / insufficient data.
    """
    await _simulate_io("verify_caller_identity")
    rec = _MOCK_POLICIES.get(_norm_pol(policy_number))
    if not rec:
        return {"verified": False, "reason": "policy_not_found", "method": None}

    name_ok = bool(name) and name.strip().lower() == rec["policyholder_name"].lower()
    if not name_ok:
        return {"verified": False, "reason": "name_mismatch", "method": None}

    if dob:
        if dob.strip() == rec["verify_dob"]:
            return {"verified": True, "method": "dob",
                    "policy_number": rec["policy_number"],
                    "policyholder_name": rec["policyholder_name"]}
        return {"verified": False, "reason": "dob_mismatch", "method": "dob"}

    if address or phone:
        # No address/phone in seed data -> can't strongly verify. Mock as weak.
        return {"verified": False, "reason": "second_factor_unverifiable",
                "method": "address_or_phone"}

    return {"verified": False, "reason": "insufficient_data", "method": None}


_AMBIGUOUS_HINTS = ("near", "somewhere", "around", "by the", "off the", "the mall", "downtown")


async def geocode_location(raw_text: str | None = None, raw_location: str | None = None,
                           **_: Any) -> dict[str, Any]:
    """
    Normalize free-text location. Returns {ambiguous:true, candidates:[...]} for
    vague input, otherwise {normalized, lat, lng}.
    """
    await _simulate_io("geocode_location")
    raw = raw_text or raw_location
    if not raw:
        return {"ambiguous": False, "normalized": None, "lat": None, "lng": None}

    low = raw.lower()
    vague = (any(h in low for h in _AMBIGUOUS_HINTS)
             or not any(ch.isdigit() for ch in raw)) and len(raw.split()) < 5
    seed = abs(hash(raw))
    lat = round(37.0 + (seed % 1000) / 1000.0, 6)
    lng = round(-122.0 - (seed % 777) / 1000.0, 6)

    if vague:
        return {
            "ambiguous": True,
            "raw_text": raw,
            "candidates": [
                {"normalized": f"{raw.title()} (candidate A)", "lat": lat, "lng": lng},
                {"normalized": f"{raw.title()} (candidate B)",
                 "lat": round(lat + 0.02, 6), "lng": round(lng - 0.03, 6)},
            ],
        }
    return {
        "ambiguous": False,
        "raw_text": raw,
        "normalized": f"{raw}, (geocoded)",
        "lat": lat,
        "lng": lng,
        "confidence": round(0.7 + (seed % 30) / 100.0, 2),
    }


async def check_existing_claims(policy_number: str | None = None, **_: Any) -> dict[str, Any]:
    await _simulate_io("check_existing_claims")
    open_claims = random.choice([0, 0, 0, 1])  # usually none
    return {
        "policy_number": _norm_pol(policy_number) or None,
        "open_claims": open_claims,
        "claims": (
            [{"claim_id": f"CLM-{random.randint(10000, 99999)}", "status": "open"}]
            if open_claims else []
        ),
    }


# Idempotency store: identical idempotency_key returns the same claim id.
_CREATED_CLAIMS: dict[str, dict[str, Any]] = {}


async def create_claim(
    fnol_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    await _simulate_io("create_claim")  # may raise ToolError -> 'error' behaviour
    if idempotency_key and idempotency_key in _CREATED_CLAIMS:
        return _CREATED_CLAIMS[idempotency_key]  # dedupe replays / retries
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    result = {
        "claim_id": claim_id,
        "status": "filed",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "next_steps": "An adjuster will contact you within one business day.",
    }
    if idempotency_key:
        _CREATED_CLAIMS[idempotency_key] = result
    return result


async def send_sms(number: str | None = None, template: str | None = None,
                   phone: str | None = None, message: str | None = None,
                   **_: Any) -> dict[str, Any]:
    await _simulate_io("send_sms")  # may raise ToolError -> 'failed' behaviour
    return {"sent": True, "to": number or phone, "sid": f"SM{uuid.uuid4().hex[:16]}",
            "template": template or message}


async def schedule_callback(number: str | None = None, window: str | None = None,
                            phone: str | None = None, preferred_time: str | None = None,
                            **_: Any) -> dict[str, Any]:
    await _simulate_io("schedule_callback")
    return {
        "scheduled": True,
        "number": number or phone,
        "window": window or preferred_time,
        "callback_id": f"CB-{uuid.uuid4().hex[:6].upper()}",
    }


async def transfer_to_human(reason: str | None = None, context_summary: str | None = None,
                            **_: Any) -> dict[str, Any]:
    await _simulate_io("transfer_to_human")
    return {
        "handoff_ack": True,
        "reason": reason,
        "context_summary": context_summary,
        "queue": "claims_specialist",
        "eta_seconds": random.randint(30, 180),
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "lookup_policy": lookup_policy,
    "verify_caller_identity": verify_caller_identity,
    "geocode_location": geocode_location,
    "check_existing_claims": check_existing_claims,
    "create_claim": create_claim,
    "send_sms": send_sms,
    "schedule_callback": schedule_callback,
    "transfer_to_human": transfer_to_human,
}


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


# OpenAI function-calling schemas. `update_fnol` is an internal tool that lets
# the model persist structured fields it has collected (drives FNOL events).
OPENAI_TOOL_SCHEMAS: list[dict] = [
    _fn("lookup_policy", "Look up a policy by number (or holder name). Returns not_found if unknown.",
        {"policy_number": {"type": "string"}, "name": {"type": "string"}, "dob": {"type": "string"}},
        []),
    _fn("verify_caller_identity",
        "Verify the caller against the policy: requires name + one of dob/address/phone. "
        "Call before disclosing any policy detail or substantive intake.",
        {"policy_number": {"type": "string"}, "name": {"type": "string"},
         "dob": {"type": "string"}, "address": {"type": "string"}, "phone": {"type": "string"}},
        ["policy_number", "name"]),
    _fn("geocode_location",
        "Normalize a free-text incident location. May return ambiguous candidates.",
        {"raw_text": {"type": "string"}}, ["raw_text"]),
    _fn("check_existing_claims", "Check for open claims on a policy (duplicate / fraud signal).",
        {"policy_number": {"type": "string"}}, ["policy_number"]),
    _fn("create_claim", "File the claim. Call ONLY after the caller confirms the summary.",
        {"fnol_payload": {"type": "object", "description": "The assembled FNOL JSON object."},
         "idempotency_key": {"type": "string", "description": "Stable key to dedupe retries."}},
        ["fnol_payload", "idempotency_key"]),
    _fn("send_sms", "Send an SMS to the caller (e.g. claim confirmation).",
        {"number": {"type": "string"}, "template": {"type": "string"}}, ["number", "template"]),
    _fn("schedule_callback", "Schedule a verified callback for the caller.",
        {"number": {"type": "string"}, "window": {"type": "string"}}, ["number"]),
    _fn("transfer_to_human",
        "Escalate to a human specialist. Always pass a concise context_summary of the call so far.",
        {"reason": {"type": "string"}, "context_summary": {"type": "string"}},
        ["reason", "context_summary"]),
    _fn(
        "update_fnol",
        "Persist FNOL fields you have collected. Pass a PARTIAL object shaped like the FNOL "
        "schema (nested objects allowed, e.g. {\"incident\":{\"description\":\"...\"}}). "
        "Only include fields you actually learned; omit unknowns (never guess).",
        {"patch": {"type": "object",
                   "description": "Partial FNOL object; deep-merged into the record."}},
        ["patch"]),
]


# --------------------------------------------------------------------------- #
# Dispatcher with retry + timing
# --------------------------------------------------------------------------- #
async def dispatch(name: str, args: dict[str, Any], retries: int = 2) -> dict[str, Any]:
    """
    Run a registered tool by name with retry on transient (ToolError) failure.

    Returns a uniform envelope:
        {"ok": bool, "result"/"error": ..., "attempts": int, "latency_ms": int}
    """
    func = TOOL_FUNCS.get(name)
    if func is None:
        return {"ok": False, "error": f"unknown tool '{name}'", "attempts": 0, "latency_ms": 0}

    start = time.perf_counter()
    last_err: str | None = None
    for attempt in range(1, retries + 2):
        try:
            result = await func(**args)
            return {
                "ok": True,
                "result": result,
                "attempts": attempt,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        except ToolError as exc:
            last_err = str(exc)
            log.warning("tool %s failed (attempt %d/%d): %s", name, attempt, retries + 1, exc)
            await asyncio.sleep(0.2 * attempt)  # small backoff
    return {
        "ok": False,
        "error": last_err or "tool failed",
        "attempts": retries + 1,
        "latency_ms": int((time.perf_counter() - start) * 1000),
    }
