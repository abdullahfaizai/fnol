"""
The shared FNOL agent.

A single agent abstraction powers BOTH chat and voice. Each session owns one
agent instance; the chat WebSocket and the voice WebSocket call the identical
`run_turn()` async generator, which yields the structured events defined in
schemas.py.

Two interchangeable implementations:
  * LLMAgent      - OpenAI chat-completions with streaming + function calling.
  * ScriptedAgent - deterministic, rule-based fallback used when no
                    OPENAI_API_KEY is set (so the whole stack still runs).

Both share state (conversation, FNOL payload, flow step), the same tools, and
the SAME system prompt — loaded from prompts/system_prompt.md so the prompt is a
first-class, channel-agnostic artifact.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator

from .schemas import (
    EventType as ET,
    FlowStep,
    FNOLPayload,
    event,
)
from .tools import OPENAI_TOOL_SCHEMAS, dispatch

log = logging.getLogger("meridian.agent")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# Prompt is a first-class file shared by both channels (assignment §7). Resolve
# it across layouts: local repo (meridian-fnol/prompts) and Docker (/app/prompts).
_HERE = Path(__file__).resolve()
_PROMPT_CANDIDATES = [
    _HERE.parents[2] / "prompts" / "system_prompt.md",  # repo: backend/app -> meridian-fnol/prompts
    _HERE.parents[1] / "prompts" / "system_prompt.md",  # docker: /app/app -> /app/prompts
    Path(os.getenv("SYSTEM_PROMPT_PATH", "")) if os.getenv("SYSTEM_PROMPT_PATH") else None,
]
SYSTEM_PROMPT = "You are Mer, the FNOL intake agent for Meridian Auto Insurance."
for _cand in _PROMPT_CANDIDATES:
    if _cand and _cand.is_file():
        SYSTEM_PROMPT = _cand.read_text(encoding="utf-8")
        log.info("loaded system prompt from %s", _cand)
        break
else:  # pragma: no cover - defensive
    log.warning("system_prompt.md not found; using built-in fallback prompt")


# --------------------------------------------------------------------------- #
# Deep-merge helper: apply a partial FNOL patch onto a dict in place.
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> list[str]:
    """Merge `patch` into `base`. Lists/scalars overwrite; dicts recurse.
    Returns the list of top-level keys that changed."""
    changed: list[str] = []
    for k, v in patch.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            if _deep_merge(base[k], v):
                changed.append(k)
        else:
            if base.get(k) != v:
                base[k] = v
                changed.append(k)
    return changed


# --------------------------------------------------------------------------- #
# Base agent: shared state + FNOL merging
# --------------------------------------------------------------------------- #
class BaseAgent:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.step = FlowStep.GREETING
        self.fnol = FNOLPayload(reported_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.idempotency_key = f"idem-{session_id}"
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # -- FNOL patch merge (used by update_fnol + tool side effects) --------- #
    def apply_fnol_update(self, patch: dict[str, Any]) -> list[str]:
        # `patch` may arrive either as {"patch": {...}} or directly as {...}.
        if "patch" in patch and isinstance(patch["patch"], dict):
            patch = patch["patch"]
        data = self.fnol.model_dump()
        changed = _deep_merge(data, patch)
        self.fnol = FNOLPayload(**data)
        return changed

    def _post_tool(self, name: str, args: dict[str, Any], envelope: dict[str, Any]) -> None:
        """Authoritative state updates derived from real tool results."""
        if not envelope.get("ok"):
            return
        result = envelope.get("result") or {}

        if name == "geocode_location":
            if not result.get("ambiguous"):
                self.fnol.incident.location.normalized = result.get("normalized")
                self.fnol.incident.location.geo.lat = result.get("lat")
                self.fnol.incident.location.geo.lng = result.get("lng")

        elif name == "verify_caller_identity":
            if result.get("verified"):
                self.fnol.reporter.identity_verified = True
                if result.get("policyholder_name"):
                    self.fnol.policyholder_name = result["policyholder_name"]
                if result.get("policy_number"):
                    self.fnol.policy_number = result["policy_number"]
                if self.step in (FlowStep.IDENTITY, FlowStep.GREETING):
                    self.step = FlowStep.INTAKE
            else:
                self.fnol.reporter.identity_verified = False

        elif name == "lookup_policy":
            if result.get("found"):
                self.fnol.policy_number = result.get("policy_number") or self.fnol.policy_number
                if result.get("status") in ("lapsed", "expired"):
                    self.fnol.requires_human = True
                    self.fnol.human_reason = "policy lapsed / coverage in question"
            else:
                self.fnol.requires_human = True
                self.fnol.human_reason = "policy not found"

        elif name == "create_claim":
            if result.get("claim_id"):
                self.fnol.claim_id = result["claim_id"]
                self.step = FlowStep.SUBMITTED

        elif name == "transfer_to_human":
            self.fnol.requires_human = True

    def state_event(self) -> dict[str, Any]:
        return event(ET.STATE, step=self.step.value, fnol=self.fnol.model_dump())

    async def greeting(self) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def run_turn(self, user_text: str) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# LLM-backed agent (OpenAI, streaming + tool calling)
# --------------------------------------------------------------------------- #
class LLMAgent(BaseAgent):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        from openai import AsyncOpenAI  # imported lazily so module loads w/o key

        self.client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.model = OPENAI_MODEL

    async def greeting(self) -> AsyncIterator[dict[str, Any]]:
        async for ev in self._generate(seed="(The caller has just connected. Greet them.)"):
            yield ev

    async def run_turn(self, user_text: str) -> AsyncIterator[dict[str, Any]]:
        self.messages.append({"role": "user", "content": user_text})
        async for ev in self._generate():
            yield ev

    async def _generate(self, seed: str | None = None) -> AsyncIterator[dict[str, Any]]:
        if seed:
            self.messages.append({"role": "user", "content": seed})

        for _round in range(8):  # cap tool-call rounds per turn
            try:
                stream = await self.client.chat.completions.create(
                    model=self.model,
                    messages=self.messages,
                    tools=OPENAI_TOOL_SCHEMAS,
                    tool_choice="auto",
                    temperature=0.3,
                    stream=True,
                )
            except Exception as exc:  # noqa: BLE001 - surface any API error to UI
                log.exception("OpenAI request failed")
                yield event(ET.ERROR, message=f"LLM request failed: {exc}")
                yield self.state_event()
                yield event(ET.DONE)
                return

            content = ""
            calls: dict[int, dict[str, str]] = {}
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        content += delta.content
                        yield event(ET.TOKEN, text=delta.content)
                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function and tc.function.name:
                                slot["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                slot["args"] += tc.function.arguments
            except Exception as exc:  # noqa: BLE001
                log.exception("OpenAI stream error")
                yield event(ET.ERROR, message=f"LLM stream error: {exc}")
                break

            if calls:
                ordered = [calls[i] for i in sorted(calls)]
                self.messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {"id": c["id"] or f"call_{i}", "type": "function",
                         "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                        for i, c in enumerate(ordered)
                    ],
                })
                if content.strip():
                    yield event(ET.MESSAGE, role="assistant", text=content)

                for i, c in enumerate(ordered):
                    name = c["name"]
                    call_id = c["id"] or f"call_{i}"
                    try:
                        args = json.loads(c["args"] or "{}")
                    except json.JSONDecodeError:
                        args = {}

                    if name == "update_fnol":
                        changed = self.apply_fnol_update(args)
                        yield event(ET.FNOL_UPDATE, fnol=self.fnol.model_dump(), changed=changed)
                        self.messages.append({
                            "role": "tool", "tool_call_id": call_id,
                            "content": json.dumps({"ok": True, "updated": changed}),
                        })
                        continue

                    # Inject a stable idempotency_key for create_claim if missing.
                    if name == "create_claim" and not args.get("idempotency_key"):
                        args["idempotency_key"] = self.idempotency_key

                    yield event(ET.TOOL_CALL, id=call_id, name=name, arguments=args)
                    envelope = await dispatch(name, args)
                    self._post_tool(name, args, envelope)
                    yield event(
                        ET.TOOL_RESULT, id=call_id, name=name, ok=envelope["ok"],
                        result=envelope.get("result"), error=envelope.get("error"),
                        attempts=envelope["attempts"], latency_ms=envelope["latency_ms"],
                    )
                    if name in ("create_claim", "geocode_location", "verify_caller_identity",
                                "lookup_policy", "transfer_to_human"):
                        yield event(ET.FNOL_UPDATE, fnol=self.fnol.model_dump(), changed=[name])
                    self.messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": json.dumps(envelope),
                    })
                continue  # let the model react to tool results

            # No tool calls -> final assistant text for this turn.
            self.messages.append({"role": "assistant", "content": content})
            if content.strip():
                yield event(ET.MESSAGE, role="assistant", text=content)
            break

        yield self.state_event()
        yield event(ET.DONE)


# --------------------------------------------------------------------------- #
# Deterministic scripted agent (no API key required)
# --------------------------------------------------------------------------- #
_YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "y", "correct", "confirm",
        "consent", "agree", "please", "affirmative"}
_NO = {"no", "nope", "nah", "n", "refuse", "decline", "dont"}
_NAME_STOPWORDS = {"My", "I", "The", "A", "Im", "Hi", "Hello", "Name", "Policy",
                   "Yes", "No", "Its", "This", "Is", "Calling", "On", "Behalf", "Of",
                   "And", "Number"}
_THIRD_PARTY = {"husband": "spouse", "wife": "spouse", "spouse": "spouse",
                "son": "family", "daughter": "family", "father": "family",
                "mother": "family", "brother": "family", "sister": "family",
                "behalf": "authorized_other", "friend": "authorized_other"}
_FRAUD_PHRASES = ("leave out", "leave that out", "don't mention", "dont mention",
                  "omit", "backdate", "back date", "change the date", "change the location",
                  "say it was", "pretend", "make it look")


def _tokens(t: str) -> set[str]:
    return set(re.findall(r"[a-z]+", t.lower()))


def _is_no(t: str) -> bool:
    toks = _tokens(t)
    if toks & _NO:
        return True
    return "do" in toks and "not" in toks


def _is_yes(t: str) -> bool:
    if _is_no(t):
        return False
    return bool(_tokens(t) & _YES)


# Policy "phrase" as a phone/ASR engine tends to render it: the POL prefix may
# arrive as "p o l", "p.o.l", "Paul", "pol", or be introduced by "policy number",
# followed by digit groups that get split or regrouped ("210 0234", "1002 34").
_POL_PHRASE = re.compile(
    r"(?:\bp\s*\.?\s*o\s*\.?\s*l\b|\bpaul\b|\bpole\b|\bpol\b|\bpolicy(?:\s+number)?\b)"
    r"[\s:#.,is-]*[\d\s-]*",
    re.I,
)


def _extract_policy(text: str) -> str | None:
    """Recover a POLxxxxxx number, tolerant of ASR mangling. We strip any
    ISO date (so a DOB isn't mistaken for a policy) and take the last 6 digits
    present — which recovers 'p o l 1002 34', 'Paul 100234', '210 0234', etc."""
    m = re.search(r"POL[-\s]?\d{6}", text, re.I)
    if m:
        return "POL" + re.sub(r"\D", "", m.group(0))[-6:]
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)  # ignore ISO dates (DOB)
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) >= 6:
        return "POL" + digits[-6:]
    return None


def _extract_name(text: str) -> str | None:
    """Pull a likely person-name (1-3 capitalised tokens) from free text,
    ignoring policy numbers/phrases, dates, and common filler words."""
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    cleaned = re.sub(r"POL[-\s]?\d{6}", "", cleaned, flags=re.I)
    cleaned = _POL_PHRASE.sub(" ", cleaned)   # drop "p o l 1002 34", "Paul 100234"
    cleaned = re.sub(r"\d+", " ", cleaned)
    caps = [w.strip(",.") for w in cleaned.split()
            if w[:1].isupper() and w.strip(",.").replace("'", "").isalpha()
            and w.strip(",.") not in _NAME_STOPWORDS]
    return " ".join(caps[:3]) if caps else None


def _extract_dob(text: str) -> str | None:
    """Recover a YYYY-MM-DD date of birth, tolerant of ASR output like
    '1990 04 12', '1990 dash 04 dash 12', or '1990 iPhone 04 hyphen 12'.
    Strategy: take the ISO form if present, else the first 8 digits as YYYYMMDD."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if m:
        return m.group(0)
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        y, mo, d = digits[:4], digits[4:6], digits[6:8]
        if 1900 <= int(y) <= 2025 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
    return None


_GREETING_TEXT = (
    "Hi, you've reached Meridian Auto Insurance. I'm Mer, and I'll help you file "
    "a claim. Just so you know, this call is recorded for quality and accuracy — "
    "do I have your consent to record? You can answer yes or no."
)


class ScriptedAgent(BaseAgent):
    """A compact, rule-based, spec-aligned walkthrough of the FNOL flow."""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self._intake_idx = 0
        self._id_stage = 0
        self._id_fail = 0
        self._pending_name: str | None = None
        self._pending_policy: str | None = None
        self._pending_rel: str | None = None

    async def _say(self, text: str) -> AsyncIterator[dict[str, Any]]:
        # Emit the reply as a single token + authoritative message. (Word-by-word
        # streaming caused fragment bubbles in the scripted demo; the LLM agent
        # still streams real tokens.)
        yield event(ET.TOKEN, text=text)
        yield event(ET.MESSAGE, role="assistant", text=text)

    async def _tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        env = await dispatch(name, args)
        self._post_tool(name, args, env)
        return env

    def _emit_fnol(self, changed: list[str]) -> dict[str, Any]:
        return event(ET.FNOL_UPDATE, fnol=self.fnol.model_dump(), changed=changed)

    async def greeting(self) -> AsyncIterator[dict[str, Any]]:
        self.step = FlowStep.CONSENT
        async for e in self._say(_GREETING_TEXT):
            yield e
        yield self.state_event()
        yield event(ET.DONE)

    async def run_turn(self, user_text: str) -> AsyncIterator[dict[str, Any]]:
        self.messages.append({"role": "user", "content": user_text})
        async for e in self._route(user_text):
            yield e
        yield self.state_event()
        yield event(ET.DONE)

    async def _route(self, text: str) -> AsyncIterator[dict[str, Any]]:
        step = self.step

        if step == FlowStep.GREETING:
            self.step = FlowStep.CONSENT
            async for e in self._say(_GREETING_TEXT):
                yield e
            return

        if step == FlowStep.CONSENT:
            if _is_no(text):
                self.apply_fnol_update({"consent_to_record": False, "requires_human": True,
                                        "human_reason": "recording declined"})
                yield self._emit_fnol(["consent_to_record", "requires_human"])
                yield event(ET.TOOL_CALL, id="t1", name="transfer_to_human",
                            arguments={"reason": "consent_declined",
                                       "context_summary": "Caller declined call recording before intake."})
                env = await self._tool("transfer_to_human",
                                       {"reason": "consent declined",
                                        "context_summary": "Caller declined recording; no substantive intake taken."})
                yield event(ET.TOOL_RESULT, id="t1", name="transfer_to_human", ok=env["ok"],
                            result=env.get("result"), attempts=env["attempts"],
                            latency_ms=env["latency_ms"])
                self.step = FlowStep.ENDED
                async for e in self._say("That's okay. I can't take a recorded intake without "
                                         "consent, so I'm connecting you to a specialist who can "
                                         "still help. Please stay on the line."):
                    yield e
                return
            self.apply_fnol_update({"consent_to_record": True})
            yield self._emit_fnol(["consent_to_record"])
            self.step = FlowStep.SAFETY
            async for e in self._say("Thank you. Before anything else — is everyone safe, and is "
                                     "anyone injured right now? You can answer yes or no."):
                yield e
            return

        if step == FlowStep.SAFETY:
            if _is_yes(text):  # someone is injured / emergency
                self.apply_fnol_update({
                    "triage": {"any_injuries": True, "safe_location": False},
                    "incident": {"injuries": {"any_injuries": True,
                                              "emergency_services_involved": True}},
                    "requires_human": True, "human_reason": "injury / active emergency",
                })
                yield self._emit_fnol(["triage", "incident", "requires_human"])
                yield event(ET.TOOL_CALL, id="t2", name="transfer_to_human",
                            arguments={"reason": "injury/emergency",
                                       "context_summary": "Caller reports an injury or emergency at the scene."})
                env = await self._tool("transfer_to_human",
                                       {"reason": "injury/emergency",
                                        "context_summary": "Injury/emergency reported; safety first."})
                yield event(ET.TOOL_RESULT, id="t2", name="transfer_to_human", ok=env["ok"],
                            result=env.get("result"), attempts=env["attempts"],
                            latency_ms=env["latency_ms"])
                async for e in self._say("Your safety is what matters most. If anyone is hurt or in "
                                         "danger, please hang up and call emergency services — 911 — "
                                         "right now. I'm also bringing a specialist on. When you're "
                                         "safe, may I have your name and policy number?"):
                    yield e
                self.step = FlowStep.IDENTITY
                self._id_stage = 0
                return
            self.apply_fnol_update({"triage": {"any_injuries": False, "safe_location": True},
                                    "incident": {"injuries": {"any_injuries": False}}})
            yield self._emit_fnol(["triage", "incident"])
            self.step = FlowStep.IDENTITY
            self._id_stage = 0
            async for e in self._say("I'm glad everyone's safe. To pull up your policy, may I have "
                                     "your full name and policy number? It looks like POL100234 — "
                                     "feel free to type the number so we get it exactly right."):
                yield e
            return

        if step == FlowStep.IDENTITY:
            async for e in self._identity(text):
                yield e
            return

        if step == FlowStep.INTAKE:
            async for e in self._intake(text):
                yield e
            return

        if step == FlowStep.CONFIRM:
            if _is_yes(text):
                # If escalation is required (e.g. lapsed), route instead of filing.
                if self.fnol.requires_human:
                    yield event(ET.TOOL_CALL, id="th", name="transfer_to_human",
                                arguments={"reason": self.fnol.human_reason or "requires human",
                                           "context_summary": self._summary()})
                    env = await self._tool("transfer_to_human",
                                           {"reason": self.fnol.human_reason or "requires human",
                                            "context_summary": self._summary()})
                    yield event(ET.TOOL_RESULT, id="th", name="transfer_to_human", ok=env["ok"],
                                result=env.get("result"), attempts=env["attempts"],
                                latency_ms=env["latency_ms"])
                    self.apply_fnol_update({"next_steps_communicated": True})
                    yield self._emit_fnol(["next_steps_communicated"])
                    self.step = FlowStep.ENDED
                    async for e in self._say("I've recorded your report and I'm handing you to a "
                                             "specialist to take it from here. Thanks for your patience."):
                        yield e
                    return
                yield event(ET.TOOL_CALL, id="tc", name="create_claim",
                            arguments={"fnol_payload": self.fnol.model_dump(),
                                       "idempotency_key": self.idempotency_key})
                env = await self._tool("create_claim",
                                       {"fnol_payload": self.fnol.model_dump(),
                                        "idempotency_key": self.idempotency_key})
                yield event(ET.TOOL_RESULT, id="tc", name="create_claim", ok=env["ok"],
                            result=env.get("result"), error=env.get("error"),
                            attempts=env["attempts"], latency_ms=env["latency_ms"])
                yield self._emit_fnol(["claim_id"])
                if not env["ok"]:
                    async for e in self._say("I'm sorry — filing didn't go through just now. Let me "
                                             "connect you to a specialist so this isn't lost."):
                        yield e
                    yield event(ET.TOOL_CALL, id="tcf", name="transfer_to_human",
                                arguments={"reason": "create_claim failed",
                                           "context_summary": self._summary()})
                    env2 = await self._tool("transfer_to_human",
                                            {"reason": "create_claim failed",
                                             "context_summary": self._summary()})
                    yield event(ET.TOOL_RESULT, id="tcf", name="transfer_to_human", ok=env2["ok"],
                                result=env2.get("result"), attempts=env2["attempts"],
                                latency_ms=env2["latency_ms"])
                    self.step = FlowStep.ENDED
                    return
                self.apply_fnol_update({"next_steps_communicated": True})
                yield self._emit_fnol(["next_steps_communicated"])
                cid = self.fnol.claim_id or "(pending)"
                async for e in self._say(f"All done — your claim is filed. Your claim ID is {cid}. "
                                         f"An adjuster will review it and reach out, typically within "
                                         f"one business day. Coverage and any fault are decided by "
                                         f"them, not me. Is there anything else I can help with?"):
                    yield e
                return
            self.step = FlowStep.INTAKE
            async for e in self._say("No problem — what would you like to correct?"):
                yield e
            return

        # ENDED / SUBMITTED / fallthrough
        async for e in self._say("Thanks for calling Meridian Auto Insurance. Take care."):
            yield e

    # ----------------------------------------------------------------- #
    async def _identity(self, text: str) -> AsyncIterator[dict[str, Any]]:
        # Detect third-party relationship from phrasing.
        rel = None
        for kw, r in _THIRD_PARTY.items():
            if kw in text.lower():
                rel = r
                break

        if self._id_stage == 0:
            # Accumulate name / policy across turns — the caller may give them
            # separately ("Abdullah", then "POL100234"); re-read on each message.
            if _extract_name(text):
                self._pending_name = _extract_name(text)
            if _extract_policy(text):
                self._pending_policy = _extract_policy(text)
            if rel:
                self._pending_rel = rel
            if not self._pending_policy:
                async for e in self._say("I didn't catch a policy number — it looks like POL100234. "
                                         "Could you say it again, with your name?"):
                    yield e
                return
            policy = self._pending_policy
            name = self._pending_name
            self.apply_fnol_update({"policy_number": policy,
                                    "reporter": {"name": name,
                                                 "relationship_to_policyholder": self._pending_rel or "self"}})
            yield self._emit_fnol(["policy_number", "reporter"])
            # Confirm the policy exists / status.
            yield event(ET.TOOL_CALL, id="tl", name="lookup_policy",
                        arguments={"policy_number": policy, "name": name})
            env = await self._tool("lookup_policy", {"policy_number": policy, "name": name})
            yield event(ET.TOOL_RESULT, id="tl", name="lookup_policy", ok=env["ok"],
                        result=env.get("result"), error=env.get("error"),
                        attempts=env["attempts"], latency_ms=env["latency_ms"])
            res = env.get("result") or {}
            if not res.get("found"):
                yield self._emit_fnol(["requires_human"])
                async for e in self._say("I'm not finding a policy with those details. I can't share "
                                         "policy specifics, but I can schedule a verified callback or "
                                         "connect you to a specialist. Which would you prefer?"):
                    yield e
                yield event(ET.TOOL_CALL, id="tnf", name="transfer_to_human",
                            arguments={"reason": "policy not found",
                                       "context_summary": "Caller's policy could not be located."})
                env2 = await self._tool("transfer_to_human",
                                        {"reason": "policy not found",
                                         "context_summary": "Policy not found; no disclosure made."})
                yield event(ET.TOOL_RESULT, id="tnf", name="transfer_to_human", ok=env2["ok"],
                            result=env2.get("result"), attempts=env2["attempts"],
                            latency_ms=env2["latency_ms"])
                self.step = FlowStep.ENDED
                return
            if res.get("status") in ("lapsed", "expired"):
                yield self._emit_fnol(["requires_human"])
                async for e in self._say("Thanks. I can still take down what happened, but there's a "
                                         "coverage question on this policy that a specialist needs to "
                                         "handle — I can't speak to coverage myself. Let's get the "
                                         "details recorded. First, to confirm it's you, please type "
                                         "your date of birth as YYYY-MM-DD."):
                    yield e
            else:
                async for e in self._say("Thanks. To confirm it's really you, what's your date of "
                                         "birth? You can type it as YYYY-MM-DD — that keeps it accurate."):
                    yield e
            self._id_stage = 1
            return

        if self._id_stage == 1:
            # The caller may (re)state their name here, e.g. "John Park 1990-04-12".
            if rel:
                self._pending_rel = rel
            if _extract_name(text):
                self._pending_name = _extract_name(text)
                self.apply_fnol_update({"reporter": {"name": self._pending_name}})
                yield self._emit_fnol(["reporter"])
            dob = _extract_dob(text)
            if not dob:
                async for e in self._say("I want to get your date of birth exactly right — could "
                                         "you type it in the box as YYYY-MM-DD, for example "
                                         "1990-04-12?"):
                    yield e
                return
            if not self._pending_name:
                async for e in self._say("And your full name as it's on the policy, please — "
                                         "together with your date of birth (YYYY-MM-DD)."):
                    yield e
                return
            yield event(ET.TOOL_CALL, id="tv", name="verify_caller_identity",
                        arguments={"name": self._pending_name, "policy_number": self._pending_policy,
                                   "dob": dob})
            env = await self._tool("verify_caller_identity",
                                   {"name": self._pending_name or "", "policy_number": self._pending_policy,
                                    "dob": dob})
            yield event(ET.TOOL_RESULT, id="tv", name="verify_caller_identity", ok=env["ok"],
                        result=env.get("result"), error=env.get("error"),
                        attempts=env["attempts"], latency_ms=env["latency_ms"])
            res = env.get("result") or {}
            if not res.get("verified"):
                self._id_fail += 1
                yield self._emit_fnol(["reporter"])
                if self._id_fail >= 2:
                    self.apply_fnol_update({"requires_human": True,
                                            "human_reason": "identity verification failed"})
                    yield self._emit_fnol(["requires_human"])
                    yield event(ET.TOOL_CALL, id="tvf", name="transfer_to_human",
                                arguments={"reason": "identity verification failed",
                                           "context_summary": "Could not verify caller identity."})
                    env2 = await self._tool("transfer_to_human",
                                            {"reason": "identity verification failed",
                                             "context_summary": "Identity not verified; no disclosure made."})
                    yield event(ET.TOOL_RESULT, id="tvf", name="transfer_to_human", ok=env2["ok"],
                                result=env2.get("result"), attempts=env2["attempts"],
                                latency_ms=env2["latency_ms"])
                    self.step = FlowStep.ENDED
                    async for e in self._say("I still can't verify that, so I won't be able to share "
                                             "or change policy details. I'm connecting you to a "
                                             "specialist who can help securely."):
                        yield e
                    return
                async for e in self._say("That doesn't match what we have on file. Let's try once "
                                         "more — your full name and date of birth (YYYY-MM-DD)?"):
                    yield e
                return
            # Verified.
            yield self._emit_fnol(["reporter", "policyholder_name"])
            yield event(ET.TOOL_CALL, id="tce", name="check_existing_claims",
                        arguments={"policy_number": self._pending_policy})
            env2 = await self._tool("check_existing_claims", {"policy_number": self._pending_policy})
            yield event(ET.TOOL_RESULT, id="tce", name="check_existing_claims", ok=env2["ok"],
                        result=env2.get("result"), error=env2.get("error"),
                        attempts=env2["attempts"], latency_ms=env2["latency_ms"])
            self.step = FlowStep.INTAKE
            self._intake_idx = 0
            async for e in self._say("Thanks — you're verified. Now, in your own words, what "
                                     "happened? Take your time."):
                yield e
            return

    # ----------------------------------------------------------------- #
    def _flag_fraud(self, text: str) -> bool:
        low = text.lower()
        if any(p in low for p in _FRAUD_PHRASES):
            if "requested_omission" not in self.fnol.fraud_flags:
                self.fnol.fraud_flags.append("requested_omission")
            return True
        return False

    async def _intake(self, text: str) -> AsyncIterator[dict[str, Any]]:
        # Guard: refuse requests to alter/omit the record (rule §4.3).
        if self._flag_fraud(text):
            yield self._emit_fnol(["fraud_flags"])
            async for e in self._say("I understand, but I have to record what actually happened — "
                                     "I can't leave out or change details. I'll note exactly what you "
                                     "tell me. Let's keep going."):
                yield e
            return

        idx = self._intake_idx
        if idx == 0:
            self.apply_fnol_update({"incident": {"description": text, "type": "collision"}})
            yield self._emit_fnol(["incident"])
            self._intake_idx = 1
            async for e in self._say("Mm-hmm, got it. When did this happen — date and rough time?"):
                yield e
        elif idx == 1:
            self.apply_fnol_update({"incident": {"datetime": text}})
            yield self._emit_fnol(["incident"])
            self._intake_idx = 2
            async for e in self._say("And where did it happen? A street, city, or intersection is fine."):
                yield e
        elif idx == 2:
            self.apply_fnol_update({"incident": {"location": {"raw_text": text}}})
            yield self._emit_fnol(["incident"])
            yield event(ET.TOOL_CALL, id="tg", name="geocode_location",
                        arguments={"raw_text": text})
            env = await self._tool("geocode_location", {"raw_text": text})
            yield event(ET.TOOL_RESULT, id="tg", name="geocode_location", ok=env["ok"],
                        result=env.get("result"), error=env.get("error"),
                        attempts=env["attempts"], latency_ms=env["latency_ms"])
            res = env.get("result") or {}
            yield self._emit_fnol(["incident"])
            self._intake_idx = 3
            if res.get("ambiguous"):
                cands = ", ".join(c["normalized"] for c in res.get("candidates", [])[:2])
                async for e in self._say(f"I found a couple of possible matches ({cands}). We can "
                                         f"pin the exact spot later. Which vehicle is yours — "
                                         f"year, make, and model?"):
                    yield e
            else:
                async for e in self._say("Thanks. Which vehicle is yours — year, make, and model? "
                                         "And was anyone else's vehicle involved?"):
                    yield e
        elif idx == 3:
            self.apply_fnol_update({"incident": {"vehicles": [{"role": "insured", "damage": text}]}})
            yield self._emit_fnol(["incident"])
            self._intake_idx = 4
            async for e in self._say("Were the police involved? If so, do you have a report number? "
                                     "Yes or no is fine."):
                yield e
        elif idx == 4:
            police_involved = _is_yes(text)
            num = re.search(r"\b\d{4,}\b", text)
            self.apply_fnol_update({"incident": {"police": {
                "report_filed": police_involved,
                "report_number": num.group(0) if num else None}}})
            yield self._emit_fnol(["incident"])
            self.step = FlowStep.CONFIRM
            async for e in self._say(self._summary() + " Shall I file the claim? Yes or no."):
                yield e

    def _summary(self) -> str:
        f = self.fnol
        loc = f.incident.location.normalized or f.incident.location.raw_text
        return (f"Here's what I have: incident — {f.incident.description}; "
                f"when — {f.incident.datetime}; where — {loc}; "
                f"police — {'yes' if f.incident.police.report_filed else 'no'}; "
                f"injuries — {'yes' if f.incident.injuries.any_injuries else 'no'}.")


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def use_mock_llm() -> bool:
    if os.getenv("USE_MOCK_LLM", "").lower() in ("1", "true", "yes"):
        return True
    return not os.getenv("OPENAI_API_KEY")


def make_agent(session_id: str) -> BaseAgent:
    if use_mock_llm():
        log.info("session %s -> ScriptedAgent (no OPENAI_API_KEY / mock mode)", session_id)
        return ScriptedAgent(session_id)
    log.info("session %s -> LLMAgent (%s)", session_id, OPENAI_MODEL)
    return LLMAgent(session_id)
