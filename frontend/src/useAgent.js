// useAgent.js — one hook drives BOTH chat and voice. It owns the WebSocket,
// reduces the backend's structured event stream into UI state (messages, tool
// timeline, live FNOL payload, flow step), and—in voice mode—optionally speaks
// replies via the Web Speech API.
import { useCallback, useEffect, useRef, useState } from "react";

const WS_BASE =
  import.meta.env.VITE_WS_BASE || `ws://${window.location.hostname}:8000`;

let _id = 0;
const nextId = () => `m${++_id}`;

export function useAgent({ mode = "chat", speak = false, autoGreet = false } = {}) {
  const path = mode === "voice" ? "/ws/voice" : "/ws/chat";
  const wsRef = useRef(null);
  const liveMsgId = useRef(null); // id of the assistant bubble currently streaming
  const greeted = useRef(false); // ensures auto-greet fires at most once
  const sessionIdRef = useRef(null); // sent on every frame so reconnects rebind

  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [messages, setMessages] = useState([]); // {id, role, text, final}
  const [tools, setTools] = useState([]); // {id, name, args, status, result, latency, attempts, error}
  const [reasoning, setReasoning] = useState([]); // debug breadcrumbs
  const [fnol, setFnol] = useState(null);
  const [step, setStep] = useState("greeting");
  const [error, setError] = useState(null);
  const [sessionId, setSessionId] = useState(null);

  const sayAloud = useCallback(
    (text) => {
      if (!speak || !("speechSynthesis" in window)) return;
      try {
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(text);
        u.rate = 1.03;
        u.pitch = 1.0;
        window.speechSynthesis.speak(u);
      } catch {
        /* ignore */
      }
    },
    [speak]
  );

  const connect = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState <= 1) return;
    const ws = new WebSocket(`${WS_BASE}${path}`);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => {
      setConnected(false);
      setBusy(false);
    };
    ws.onerror = () =>
      setError("Connection error — is the backend running on :8000?");

    ws.onmessage = (raw) => {
      let ev;
      try {
        ev = JSON.parse(raw.data);
      } catch {
        return;
      }
      switch (ev.type) {
        case "session":
          sessionIdRef.current = ev.session_id;
          setSessionId(ev.session_id);
          liveMsgId.current = null;
          break;
        case "token": {
          setBusy(true);
          setMessages((prev) => {
            if (liveMsgId.current) {
              return prev.map((m) =>
                m.id === liveMsgId.current
                  ? { ...m, text: m.text + ev.text }
                  : m
              );
            }
            const id = nextId();
            liveMsgId.current = id;
            return [...prev, { id, role: "assistant", text: ev.text, final: false }];
          });
          break;
        }
        case "message": {
          // Authoritative text for the current assistant segment.
          setMessages((prev) => {
            if (liveMsgId.current) {
              return prev.map((m) =>
                m.id === liveMsgId.current
                  ? { ...m, text: ev.text, final: true }
                  : m
              );
            }
            return [
              ...prev,
              { id: nextId(), role: "assistant", text: ev.text, final: true },
            ];
          });
          liveMsgId.current = null; // next tokens start a fresh bubble
          break;
        }
        case "tool_call":
          setTools((prev) => [
            ...prev,
            {
              id: ev.id,
              name: ev.name,
              args: ev.arguments,
              status: "running",
            },
          ]);
          break;
        case "tool_result":
          setTools((prev) =>
            prev.map((t) =>
              t.id === ev.id
                ? {
                    ...t,
                    status: ev.ok ? "ok" : "fail",
                    result: ev.result,
                    error: ev.error,
                    latency: ev.latency_ms,
                    attempts: ev.attempts,
                  }
                : t
            )
          );
          break;
        case "fnol_update":
        case "state":
          if (ev.fnol) setFnol(ev.fnol);
          if (ev.step) setStep(ev.step);
          break;
        case "reasoning":
          setReasoning((prev) => [...prev, ev.text]);
          break;
        case "tts":
          sayAloud(ev.text);
          break;
        case "error":
          setError(ev.message);
          setBusy(false);
          break;
        case "done":
          setBusy(false);
          liveMsgId.current = null;
          break;
        default:
          break;
      }
    };
  }, [path, sayAloud]);

  useEffect(() => {
    connect();
    return () => wsRef.current && wsRef.current.close();
  }, [connect]);

  const sendRaw = useCallback((obj) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== 1) {
      setError("Not connected yet — retrying…");
      connect();
      return false;
    }
    ws.send(
      JSON.stringify(
        sessionIdRef.current ? { ...obj, session_id: sessionIdRef.current } : obj
      )
    );
    return true;
  }, [connect]);

  const send = useCallback(
    (text) => {
      const t = (text || "").trim();
      if (!t) return;
      liveMsgId.current = null; // start each turn with a fresh assistant bubble
      setMessages((prev) => [
        ...prev,
        { id: nextId(), role: "user", text: t, final: true },
      ]);
      setBusy(true);
      sendRaw({ type: "user_text", text: t });
    },
    [sendRaw]
  );

  const startCall = useCallback(() => {
    liveMsgId.current = null;
    setBusy(true);
    sendRaw({ type: "start_call" });
  }, [sendRaw]);

  // Chat mode auto-greets on connect, symmetric with the voice "Start Call"
  // button. Guarded so it fires exactly once per mounted session.
  useEffect(() => {
    if (autoGreet && connected && !greeted.current) {
      greeted.current = true;
      startCall();
    }
  }, [autoGreet, connected, startCall]);

  return {
    connected,
    busy,
    messages,
    tools,
    reasoning,
    fnol,
    step,
    error,
    sessionId,
    send,
    startCall,
  };
}
