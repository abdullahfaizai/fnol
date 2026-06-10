// voice.jsx — voice simulation against the SAME agent (/ws/voice). Replies are
// spoken via the Web Speech API (TTS). Mic input uses the browser Speech
// Recognition API when available; otherwise type to "simulate caller speaking".
import { useEffect, useRef, useState } from "react";
import { useAgent } from "./useAgent.js";
import FnolPanel from "./FnolPanel.jsx";
import ToolTimeline from "./ToolTimeline.jsx";

const SR =
  typeof window !== "undefined" &&
  (window.SpeechRecognition || window.webkitSpeechRecognition);

export default function Voice() {
  const [tts, setTts] = useState(true);
  const a = useAgent({ mode: "voice", speak: tts });
  const [started, setStarted] = useState(false);
  const [draft, setDraft] = useState("");
  const [listening, setListening] = useState(false);
  const recRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [a.messages, a.busy]);

  const begin = () => {
    setStarted(true);
    a.startCall();
  };

  const speakLine = (text) => {
    const t = (text ?? draft).trim();
    if (!t || a.busy) return;
    a.send(t);
    setDraft("");
  };

  const toggleMic = () => {
    if (!SR) return;
    if (listening) {
      recRef.current?.stop();
      return;
    }
    const rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = false;
    rec.onresult = (e) => {
      const said = e.results[0][0].transcript;
      setDraft(said);
      speakLine(said);
    };
    rec.onend = () => setListening(false);
    recRef.current = rec;
    setListening(true);
    rec.start();
  };

  return (
    <div className="layout">
      <main className="conversation">
        <div className="callbar">
          {!started ? (
            <button className="callbtn callbtn--start" onClick={begin} disabled={!a.connected}>
              ● Start Call
            </button>
          ) : (
            <span className={`livedot ${a.busy ? "is-busy" : ""}`}>
              {a.busy ? "Agent speaking…" : "On call"}
            </span>
          )}
          <label className="toggle">
            <input
              type="checkbox"
              checked={tts}
              onChange={(e) => setTts(e.target.checked)}
            />
            Speak replies
          </label>
        </div>

        <div className="conversation__scroll">
          {!started && (
            <div className="hint">
              Press <strong>Start Call</strong> to hear the agent greet you, then
              speak (or type) as the caller. Demo policy <code>POL100234</code>{" "}
              (John Park, DOB 1990-04-12).
            </div>
          )}
          {a.messages.map((m) => (
            <div key={m.id} className={`bubble bubble--${m.role}`}>
              <span className="bubble__who">{m.role === "user" ? "Caller" : "Mer"}</span>
              {m.text || (m.role === "assistant" && a.busy ? "…" : "")}
            </div>
          ))}
          {a.error && <div className="bubble bubble--error">{a.error}</div>}
          <div ref={endRef} />
        </div>

        {started && (
          <div className="composer">
            {SR && (
              <button
                className={`micbtn ${listening ? "is-on" : ""}`}
                onClick={toggleMic}
                title="Speak"
              >
                🎙
              </button>
            )}
            <input
              value={draft}
              placeholder={SR ? "Speak or type as the caller…" : "Simulate caller speaking…"}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && speakLine()}
            />
            <button onClick={() => speakLine()} disabled={a.busy}>
              Say
            </button>
          </div>
        )}
      </main>

      <div className="sidebar">
        <FnolPanel fnol={a.fnol} step={a.step} />
        <ToolTimeline tools={a.tools} reasoning={a.reasoning} />
      </div>
    </div>
  );
}
