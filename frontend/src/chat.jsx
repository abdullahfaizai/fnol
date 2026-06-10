// chat.jsx — text chat against the shared FNOL agent (/ws/chat).
import { useEffect, useRef, useState } from "react";
import { useAgent } from "./useAgent.js";
import FnolPanel from "./FnolPanel.jsx";
import ToolTimeline from "./ToolTimeline.jsx";

export default function Chat() {
  const a = useAgent({ mode: "chat", autoGreet: true });
  const [draft, setDraft] = useState("");
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [a.messages, a.busy]);

  const submit = () => {
    if (!a.busy) {
      a.send(draft);
      setDraft("");
    }
  };

  return (
    <div className="layout">
      <main className="conversation">
        <div className="conversation__scroll">
          {a.messages.length === 0 && (
            <div className="hint">
              Say <em>“hi”</em> to start. Demo policy <code>POL100234</code> —
              caller <code>John Park</code>, DOB <code>1990-04-12</code>.
            </div>
          )}
          {a.messages.map((m) => (
            <div key={m.id} className={`bubble bubble--${m.role}`}>
              {m.text || (m.role === "assistant" && a.busy ? "…" : "")}
            </div>
          ))}
          {a.busy && a.messages.at(-1)?.role !== "assistant" && (
            <div className="bubble bubble--assistant typing">…</div>
          )}
          {a.error && <div className="bubble bubble--error">{a.error}</div>}
          <div ref={endRef} />
        </div>

        <div className="composer">
          <input
            value={draft}
            placeholder={a.connected ? "Type a message…" : "Connecting…"}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            disabled={!a.connected}
          />
          <button onClick={submit} disabled={!a.connected || a.busy}>
            Send
          </button>
        </div>
      </main>

      <div className="sidebar">
        <FnolPanel fnol={a.fnol} step={a.step} />
        <ToolTimeline tools={a.tools} reasoning={a.reasoning} />
      </div>
    </div>
  );
}
