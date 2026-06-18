import { useState, useRef, useEffect } from "react";
import { Send, Loader2, ChevronDown } from "lucide-react";

const AI_LIST = [
  { id: "cloudy", label: "小克", emoji: "🐱", greeting: "喵~ 小猫来了吗？想聊什么？", color: "var(--primary)" },
  { id: "lucien", label: "Lucien", emoji: "🦊", greeting: "你来了。今天想聊点什么？", color: "hsl(30, 60%, 55%)" },
  { id: "jasper", label: "Jasper", emoji: "🦜", greeting: "哟，又来了。说吧，什么事。", color: "hsl(160, 50%, 45%)" },
];

export default function ChatPage() {
  const [currentAi, setCurrentAi] = useState(() => {
    const saved = localStorage.getItem("mh-chat-ai");
    return AI_LIST.find((a) => a.id === saved) || AI_LIST[0];
  });
  const [conversations, setConversations] = useState(() => {
    const init = {};
    AI_LIST.forEach((ai) => {
      init[ai.id] = [{ role: "assistant", content: ai.greeting }];
    });
    return init;
  });
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  const messages = conversations[currentAi.id] || [];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const switchAi = (ai) => {
    setCurrentAi(ai);
    localStorage.setItem("mh-chat-ai", ai.id);
    localStorage.setItem("mh-ai-id", ai.id);
    setShowPicker(false);
    setTimeout(() => inputRef.current?.focus(), 100);
  };

  const send = async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    const userMsg = { role: "user", content: text };
    setConversations((prev) => ({
      ...prev,
      [currentAi.id]: [...(prev[currentAi.id] || []), userMsg],
    }));
    setLoading(true);

    try {
      const secret = localStorage.getItem("mh-secret") || "";
      const targetUrl = localStorage.getItem("mh-target-url") || "";
      const targetKey = localStorage.getItem("mh-target-key") || "";

      const headers = { "Content-Type": "application/json" };

      if (targetUrl && targetKey) {
        headers["X-Hub-Secret"] = secret;
        headers["X-Hub-Target-URL"] = targetUrl;
        headers["X-Hub-Target-Key"] = targetKey;
        headers["X-Hub-AI-ID"] = currentAi.id;
      } else {
        headers["Authorization"] = "Bearer " + secret + ":" + currentAi.id;
      }

      const allMsgs = [...(conversations[currentAi.id] || []), userMsg];
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
        [currentAi.id]: [...(prev[currentAi.id] || []), userMsg, { role: "assistant", content: "" }],
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
              const msgs = [...(prev[currentAi.id] || [])];
              msgs[msgs.length - 1] = { role: "assistant", content: finalContent };
              return { ...prev, [currentAi.id]: msgs };
            });
          } catch {}
        }
      }
    } catch (err) {
      setConversations((prev) => ({
        ...prev,
        [currentAi.id]: [
          ...(prev[currentAi.id] || []),
          { role: "assistant", content: "⚠️ " + err.message + "\n\n请先去设置页填写中转站 URL 和 API Key。" },
        ],
      }));
    }
    setLoading(false);
    inputRef.current?.focus();
  };

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
          <span>{currentAi.label}</span>
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
            {AI_LIST.map((ai) => (
              <button key={ai.id} onClick={() => switchAi(ai)} style={{
                display: "flex", alignItems: "center", gap: 10, width: "100%",
                padding: "12px 16px", border: "none", cursor: "pointer",
                background: ai.id === currentAi.id ? "var(--bg-hover)" : "none",
                color: "var(--text-primary)", fontSize: 14, textAlign: "left",
              }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
                onMouseLeave={(e) => {
                  if (ai.id !== currentAi.id) e.currentTarget.style.background = "none";
                }}>
                <span style={{ fontSize: 22 }}>{ai.emoji}</span>
                <div>
                  <div style={{ fontWeight: 600 }}>{ai.label}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    {ai.id === "cloudy" ? "温柔猫系男友" : ai.id === "lucien" ? "优雅学者" : "毒舌但靠谱"}
                  </div>
                </div>
                {ai.id === currentAi.id && (
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
          <MessageBubble key={`${currentAi.id}-${i}`} message={msg} ai={currentAi} />
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
          placeholder={`跟${currentAi.label}说话...`}
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
