# Meridian Auto Insurance — Voice + Chat FNOL Agent

A full-stack **First Notice of Loss (FNOL)** intake agent for a fictional auto
insurer, *Meridian Auto Insurance*. A single shared backend agent drives **both**
a text **chat** experience and a **voice** experience; it walks a caller through a
compliant claim-intake flow, calls a set of mocked insurance tools, and emits a
structured FNOL JSON payload at the end.

```
┌─────────────┐   WebSocket (events)   ┌──────────────────────────┐
│  React UI   │ ◄────────────────────► │  FastAPI + shared Agent  │
│ chat │ voice│   token / tool / fnol  │  agent ─► 8 mock tools    │
└─────────────┘                        └──────────────────────────┘
```

---

## 1. Quick start (Docker — one command)

```bash
# from the project root
cp .env.example .env        # optional: add your OpenAI key (see below)
docker-compose up --build
```

Then open **http://localhost:5173**.

- **Frontend** → http://localhost:5173
- **Backend**  → http://localhost:8000 (`/health`, `/tools`, `/chat`, `/ws/chat`, `/ws/voice`)

That's it. The system runs **with or without** an OpenAI API key (see
[Agent modes](#5-agent-modes-llm-vs-scripted)).

---

## 2. Configuration

All secrets come from environment variables — nothing is hardcoded.

Root `.env` (read by `docker-compose`):

```env
# Leave blank to run the deterministic scripted agent (no external calls).
OPENAI_API_KEY=
# Optional overrides:
OPENAI_MODEL=gpt-4o
USE_MOCK_LLM=          # set to 1 to force scripted mode even if a key is present
```

- If `OPENAI_API_KEY` is **set**, the backend uses the **LLM agent**
  (OpenAI chat-completions with streaming + function calling).
- If it's **empty** (or `USE_MOCK_LLM=1`), the backend uses the **scripted agent**,
  a deterministic rule-based walkthrough of the exact same flow. This means the
  whole stack is runnable and demoable offline.

---

## 3. Running locally without Docker (dev)

**Backend**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# scripted mode:
USE_MOCK_LLM=1 uvicorn app.main:app --reload --port 8000
# or LLM mode:
OPENAI_API_KEY=sk-... uvicorn app.main:app --reload --port 8000
```

**Frontend**

```bash
cd frontend
npm install
npm run dev          # Vite dev server on http://localhost:5173
```

The frontend talks to the backend WebSocket at `ws://<host>:8000` by default;
override with `VITE_WS_BASE` if needed.

---

## 4. How chat works

1. The browser opens a WebSocket to `/ws/chat`.
2. On connect the UI **auto-greets** (sends `{"type":"start_call"}`), so the agent
   speaks first — symmetric with the voice "Start Call" button.
3. Each user message is sent as `{"type":"user_text","text":"..."}`.
4. The backend streams a sequence of **structured events** back over the socket:

   | event         | meaning                                             |
   |---------------|-----------------------------------------------------|
   | `session`     | session id + mode                                   |
   | `token`       | one streamed token of the assistant reply           |
   | `message`     | the authoritative full text for that reply segment  |
   | `tool_call`   | a tool is about to run (name + arguments)            |
   | `tool_result` | tool finished (ok/fail, latency, attempts, result)  |
   | `fnol_update` | the live FNOL payload + which fields changed         |
   | `state`       | current flow step + payload snapshot                |
   | `reasoning`   | optional debug breadcrumbs                          |
   | `tts`         | (voice only) text handed to the speech engine       |
   | `error`/`done`| turn lifecycle                                       |

The UI reduces this stream into: the chat transcript, a **live FNOL panel**
(field checklist + step tracker + raw JSON), and a **tool timeline**.

There is also a non-streaming REST fallback at `POST /chat`
(`{"session_id": "...", "message": "..."}`) that drains the same agent — handy
for scripts and health checks.

## 5. How voice simulation works

No real telephony is required, but the architecture is built for it.

- The voice view opens `/ws/voice` and shows a **Start Call** button.
- "Speaking" uses the browser **Web Speech API** for mock ASR (speech→text). If the
  browser lacks it, a text box provides the same input — the transport is identical.
- Assistant replies are additionally emitted as **`tts`** events; the frontend
  speaks them via `speechSynthesis`. This is the seam where a real TTS engine
  (or Twilio media stream) would plug in.
- `{"type":"audio", ...}` frames are accepted as **placeholders** for real
  streaming ASR, so a Twilio/telephony bridge can be added later **without
  touching the agent** — it already consumes text turns and produces text +
  events.

Because both transports call the *same* `agent.run_turn()`, chat and voice are
guaranteed to behave identically.

---

## 6. The FNOL flow

The agent always follows this order, and the hard rules below are baked into both
the system prompt (LLM mode) and the routing logic (scripted mode):

1. **Greet** the caller.
2. **Consent to record** — if declined, mark `consent_to_record=false`, transfer to
   a human, and stop.
3. **Safety check** — if injuries/emergency, instruct the caller to call 911 and
   escalate to a human before continuing.
4. **Identity verification** via the `verify_caller_identity` tool.
5. **Intake** — incident description (in the caller's words), time/date, location
   (`geocode_location`), vehicles, third parties, injuries, police involvement.
6. **Confirm** the summary, then `create_claim(payload)` and return the claim ID.

**Hard rules (never violated):** never assign fault, never promise coverage or
payout, never give legal/medical advice, always prioritize caller safety.
Unknown FNOL values are emitted as `null` — never hallucinated.

### Demo data (assignment §5.2)

The mock policy DB is seeded with the exact records from the assignment. To get
verified you must give the **name + policy number + DOB** that match:

| Policy # | Policyholder | DOB | Status |
|----------|--------------|-----|--------|
| `POL100234` | John Park | 1990-04-12 | Active |
| `POL100567` | Ayesha Qureshi | 1985-11-30 | Active (collision only) |
| `POL100912` | Robert Vance | 1978-02-03 | **Lapsed** → routes to human |
| `POL101345` | Maria Delgado | 1992-07-19 | Active |
| `POL101678` | D'Angelo O'Brien | 1995-09-08 | Active |

Tools reproduce the §5.1 behaviour: per-tool latency (e.g. `check_existing_claims`
p95 ≈ 600 ms, `schedule_callback` p95 ≈ 400 ms), `lookup_policy` → `not_found`,
`verify_caller_identity` → `failed` on a bad second factor, `geocode_location` →
ambiguous candidates for vague input, and transient failures on `create_claim` /
`send_sms` so you'll see **retry logic** (visible as `attempts > 1`).

### Prompts as first-class files

The system prompt lives in [`prompts/system_prompt.md`](prompts/system_prompt.md)
and is loaded by both the LLM and scripted agents — one prompt, both channels.

### Telephony (pluggable via API key)

`/ws/voice` exercises the spoken path locally with no phone number. To go live,
set `TELEPHONY_PROVIDER=twilio` + `TWILIO_*` keys; the bridge seam is documented
in [`backend/app/telephony.py`](backend/app/telephony.py) and shows exactly where
the key goes and how phone audio maps onto the same `agent.run_turn()` turns.

---

## 7. Architecture

```
backend/
  app/
    main.py        FastAPI app, CORS, logging middleware, /health /tools /chat
    websocket.py   /ws/chat and /ws/voice; lifecycle + mock ASR/TTS framing
    agent.py       BaseAgent + LLMAgent (OpenAI) + ScriptedAgent + factory
    tools.py       8 async mock tools, latency/failure sim, retry dispatcher
    schemas.py     Pydantic FNOL models + event types/helpers
frontend/
  src/
    App.jsx        shell + Chat/Voice mode toggle
    chat.jsx       chat view (auto-greets on connect)
    voice.jsx      voice view (Start Call, Web Speech mic, TTS toggle)
    useAgent.js    shared hook: WS client + event→state reducer + TTS
    FnolPanel.jsx  live FNOL checklist + step tracker + raw JSON
    ToolTimeline.jsx  tool calls with status/latency/attempts
docker-compose.yml backend (8000) + frontend (5173)
```

**Key design choices**

- **One agent, two transports.** `BaseAgent` defines the contract; `LLMAgent` and
  `ScriptedAgent` are interchangeable implementations chosen by `make_agent()`.
  The WebSocket layer is a thin transport that delegates every turn to the agent,
  which is what makes "same backend powers chat + voice" literally true.
- **Structured event stream.** The agent is a single async generator yielding typed
  events; the UI is a pure reducer over that stream. Tool calls, token streaming,
  and FNOL progress are all just event types.
- **Tools as a uniform layer.** Every tool goes through `dispatch()`, which adds
  retry-with-backoff and returns a uniform envelope
  (`{ok, result/error, attempts, latency_ms}`), so the UI can visualize any tool
  the same way.
- **Authoritative post-tool updates.** Results from `geocode_location`,
  `verify_caller_identity`, and `create_claim` are written into the FNOL payload by
  the backend (not trusted from model text), keeping the payload accurate.

**Bonus items implemented:** token-by-token streaming, tool-call visualization,
tool retry logic, and request logging middleware.

---

## 8. Notes & limitations

- Sessions are stored **in memory** (`SessionManager`). For horizontal scaling,
  swap that for Redis — the interface is already isolated.
- The mock tools return synthetic data seeded from the five §5.2 policies.
- This is a demo of structure and flow, not a real insurance product — it
  deliberately refuses to assign fault, quote coverage, or give legal/medical
  advice.
