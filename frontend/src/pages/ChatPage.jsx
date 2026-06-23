import { useState, useRef, useEffect } from "react";
import { Send, Loader2, ChevronDown } from "lucide-react";
import { useAI } from "../contexts/AIContext";

export default function ChatPage() {
  const { profiles } = useAI();
  const [currentId, setCurrentId] = useState(() => localStorage.getItem("mh-chat-ai") || "");
  const [conversations, setConversations] = useState({});
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  const currentAi = profiles.find((p) => p.ai_id === currentId) || profiles[0];
  const aiId = currentAi?.ai_id;

  useEffect(() => {
    if (!currentId && profiles.length) {
      setCurrentId(profiles[0].ai_id);
    }
  }, [profiles, currentId]);

  useEffect(() => {
    if (aiId && !conversations[aiId]) {
      const greeting = currentAi.greeting || `你好，我是${currentAi.name}`;
      setConversations((prev) => ({
        ...prev,
        [aiId]: [{ role: "assistant", content: greeting }],
      }));
    }
  }, [aiId]);

  const messages = conversations[aiId] || [];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const switchAi = (p) => {
    setCurrentId(p.ai_id);
    localStorage.setItem("mh-chat-ai", p.ai_id);
    localStorage.setItem("mh-ai-id", p.ai_id);
    setShowPicker(false);
    if (!conversations[p.ai_id]) {
      const greeting = p.greeting || `你好，我是${p.name}`;
      setConversations((prev) => ({
        ...prev,
        [p.ai_id]: [{ role: "assistant", content: greeting }],
      }));
    }
    setTimeout(() => inputRef.current?.focus(), 100);
  };

  const send = async () => {
    const text = input.trim();
    if (!text || loading || !aiId) return;
    setInput("");
    const userMsg = { role: "user", content: text };
    setConversations((prev) => ({
      ...prev,
      [aiId]: [...(prev[aiId] || []), userMsg],
    }));
    setLoading(true);

    try {
      const secret = localStorage.getItem("mh-secret") || "";
      const headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + secret + ":" + aiId,
      };

      const allMsgs = [...(conversations[aiId] || []), userMsg];
      const res = await fetch("/v1/chat/completions", {
        method: "POST",
        headers,
        body: JSON.stringify({
          model: "current",
          messages: allMsgs.slice(-20),
          stream: true,
        }),
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error("HTTP " + res.status + ": " + errText.slice(0, 200));
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let assistantContent = "";

      setConversations((prev) => ({
        ...prev,
        [aiId]: [...(prev[aiId] || []), { role: "assistant", content: "" }],
      }));

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        const lines = chunk.split("\n").filter((l) => l.startsWith("data: "));
        for (const line of lines) {
          const data = line.slice(6);
          if (data === "[DONE]") break;
          try {
            const parsed = JSON.parse(data);
            const delta = parsed.choices?.[0]?.delta?.content || "";
            assistantContent += delta;
            const finalContent = assistantContent;
            setConversations((prev) => {
              const msgs = [...(prev[aiId] || [])];
              msgs[msgs.length - 1] = { role: "assistant", content: finalContent };
              return { ...prev, [aiId]: msgs };
            });
          } catch {}
        }
      }
    } catch (err) {
      setConversations((prev) => ({
        ...prev,
        [aiId]: [
          ...(prev[aiId] || []),
          { role: "assistant", content: "⚠️ " + err.message + "\n\n请去 AI 档案页配置模型和 API Key。" },
        ],
      }));
    }
    setLoading(false);
    inputRef.current?.focus();
  };

  if (!currentAi) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", maxWidth: 720, margin: "0 auto" }}>
      {/* AI Picker Header */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: "var(--space-sm) 0", position: "relative",
      }}>
        <button onClick={() => setShowPicker(!showPicker)} style={{
          display: "flex", alignItems: "center", gap: 8,
          background: "var(--bg-card)", border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-lg)", padding: "8px 16px",
          cursor: "pointer", fontSize: 15, fontWeight: 600,
          color: "var(--text-primary)", transition: "var(--transition-fast)",
        }}>
          <span style={{ fontSize: 22 }}>{currentAi.emoji}</span>
          <span>{currentAi.name}</span>
          <ChevronDown size={16} style={{
            color: "var(--text-muted)",
            transform: showPicker ? "rotate(180deg)" : "none",
            transition: "transform 0.2s",
          }} />
        </button>

        {showPicker && (
          <div style={{
            position: "absolute", top: "100%", zIndex: 20,
            background: "var(--bg-card)", borderRadius: "var(--radius-md)",
            boxShadow: "var(--shadow-lg)", border: "1px solid var(--border-subtle)",
            overflow: "hidden", minWidth: 200,
          }}>
            {profiles.map((p) => (
              <button key={p.ai_id} onClick={() => switchAi(p)} style={{
                display: "flex", alignItems: "center", gap: 10, width: "100%",
                padding: "12px 16px", border: "none", cursor: "pointer",
                background: p.ai_id === aiId ? "var(--bg-hover)" : "none",
                color: "var(--text-primary)", fontSize: 14, textAlign: "left",
              }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
                onMouseLeave={(e) => {
                  if (p.ai_id !== aiId) e.currentTarget.style.background = "none";
                }}>
                <span style={{ fontSize: 22 }}>{p.emoji}</span>
                <div>
                  <div style={{ fontWeight: 600 }}>{p.name}</div>
                  {p.persona && (
                    <div style={{ fontSize: 11, color: "var(--text-muted)", maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {p.persona.slice(0, 30)}
                    </div>
                  )}
                </div>
                {p.ai_id === aiId && (
                  <span style={{ marginLeft: "auto", color: "var(--primary)", fontSize: 12 }}>当前</span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-md) 0" }}>
        {messages.map((msg, i) => (
          <MessageBubble key={`${aiId}-${i}`} message={msg} ai={currentAi} />
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="glass" style={{
        display: "flex", gap: "var(--space-sm)", alignItems: "center",
        padding: "var(--space-sm) var(--space-md)", marginTop: "var(--space-sm)",
      }}>
        <input
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
          placeholder={`跟${currentAi.name}说话...`}
          style={{
            flex: 1, padding: "10px 14px", border: "none", outline: "none",
            background: "var(--bg-input)", borderRadius: "var(--radius-md)",
            fontSize: 14, color: "var(--text-primary)",
          }}
        />
        <button className="btn btn-primary" onClick={send} disabled={loading}
          style={{ padding: "10px 14px", borderRadius: "var(--radius-md)" }}>
          {loading ? <Loader2 size={18} className="spin" /> : <Send size={18} />}
        </button>
      </div>
    </div>
  );
}

function MessageBubble({ message, ai }) {
  const isUser = message.role === "user";
  return (
    <div style={{
      display: "flex", justifyContent: isUser ? "flex-end" : "flex-start",
      marginBottom: "var(--space-sm)", padding: "0 var(--space-sm)",
      alignItems: "flex-end", gap: 6,
    }}>
      {!isUser && (
        <span style={{ fontSize: 20, marginBottom: 4, flexShrink: 0 }}>{ai.emoji}</span>
      )}
      <div style={{
        maxWidth: "78%",
        padding: "10px 14px",
        borderRadius: isUser
          ? "var(--radius-lg) var(--radius-lg) var(--radius-sm) var(--radius-lg)"
          : "var(--radius-lg) var(--radius-lg) var(--radius-lg) var(--radius-sm)",
        background: isUser ? "var(--primary)" : "var(--bg-card)",
        color: isUser ? "var(--text-on-primary)" : "var(--text-primary)",
        backdropFilter: isUser ? "none" : "blur(var(--glass-blur))",
        border: isUser ? "none" : "1px solid var(--glass-border)",
        fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word",
      }}>
        {message.content || "..."}
      </div>
    </div>
  );
}
