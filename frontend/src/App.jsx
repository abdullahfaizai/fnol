// App.jsx — shell with the Chat / Voice mode toggle. Both modes talk to the
// same backend agent over their respective WebSockets.
import { useState } from "react";
import Chat from "./chat.jsx";
import Voice from "./voice.jsx";

export default function App() {
  const [mode, setMode] = useState("chat");
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand__mark">◆</span>
          <div>
            <div className="brand__name">Meridian Auto Insurance</div>
            <div className="brand__sub">First Notice of Loss · AI intake</div>
          </div>
        </div>
        <nav className="modes">
          <button
            className={mode === "chat" ? "is-active" : ""}
            onClick={() => setMode("chat")}
          >
            Chat
          </button>
          <button
            className={mode === "voice" ? "is-active" : ""}
            onClick={() => setMode("voice")}
          >
            Voice
          </button>
        </nav>
      </header>

      {/* Remount on mode change so each mode gets a fresh session/socket. */}
      {mode === "chat" ? <Chat key="chat" /> : <Voice key="voice" />}

      <footer className="foot">
        Mer never assigns fault, promises coverage, or gives legal/medical
        advice. Safety always comes first.
      </footer>
    </div>
  );
}
