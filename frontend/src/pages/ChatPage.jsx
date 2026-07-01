import { useState, useRef, useEffect, useCallback } from "react";
import { Send, Loader2, ChevronDown, Paintbrush, Trash2, CheckSquare, Square, X } from "lucide-react";
import { useAI } from "../contexts/AIContext";

const CHAT_STORAGE_KEY = "mh-chat-conversations-v1";

function hubRequestAiId(aiId) {
  return aiId === "cloudy" ? "claude" : aiId;
}

function loadSavedConversations() {
  try {
    const raw = localStorage.getItem(CHAT_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function trimConversations(next) {
  return Object.fromEntries(
    Object.entries(next).map(([ai, msgs]) => [ai, Array.isArray(msgs) ? msgs.slice(-200) : []])
  );
}

export default function ChatPage() {
  const { profiles } = useAI();
  const [currentId, setCurrentId] = useState(() => localStorage.getItem("mh-chat-ai") || "");
  const [conversations, setConversations] = useState(loadSavedConversations);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedMessages, setSelectedMessages] = useState({});
  const bottomRef = useRef(null);
  const inputRef = useRef(null);
  const abortRef = useRef(null);
  const [drawing, setDrawing] = useState(false);

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
  const selectedKeys = Object.keys(selectedMessages).filter((key) => key.startsWith(`${aiId}:`) && selectedMessages[key]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(trimConversations(conversations)));
  }, [conversations]);

  const switchAi = (p) => {
    setCurrentId(p.ai_id);
    localStorage.setItem("mh-chat-ai", p.ai_id);
    localStorage.setItem("mh-ai-id", p.ai_id);
    setShowPicker(false);
    setSelectMode(false);
    setSelectedMessages({});
    if (!conversations[p.ai_id]) {
      const greeting = p.greeting || `你好，我是${p.name}`;
      setConversations((prev) => ({
        ...prev,
        [p.ai_id]: [{ role: "assistant", content: greeting }],
      }));
    }
    setTimeout(() => inputRef.current?.focus(), 100);
  };

  const toggleSelect = (index) => {
    const key = `${aiId}:${index}`;
    setSelectedMessages((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const deleteSelected = () => {
    if (!aiId || selectedKeys.length === 0) return;
    const selectedIndexes = new Set(selectedKeys.map((key) => Number(key.split(":")[1])));
    setConversations((prev) => ({
      ...prev,
      [aiId]: (prev[aiId] || []).filter((_, index) => !selectedIndexes.has(index)),
    }));
    setSelectedMessages({});
    setSelectMode(false);
  };

  const clearCurrentChat = () => {
    if (!aiId) return;
    const greeting = currentAi.greeting || `你好，我是${currentAi.name}`;
    setConversations((prev) => ({
      ...prev,
      [aiId]: [{ role: "assistant", content: greeting }],
    }));
    setSelectedMessages({});
    setSelectMode(false);
  };

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || loading || !aiId) return;

    const targetAi = aiId;
    const requestAi = hubRequestAiId(targetAi);
    setInput("");
    const userMsg = { role: "user", content: text };

    setConversations((prev) => ({
      ...prev,
      [targetAi]: [...(prev[targetAi] || []), userMsg],
    }));
    setLoading(true);

    try {
      const secret = localStorage.getItem("mh-secret") || "";
      const controller = new AbortController();
      abortRef.current = controller;

      const res = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": "Bearer " + secret + ":" + requestAi,
        },
        body: JSON.stringify({
          model: "current",
          messages: [...(conversations[targetAi] || []), userMsg].slice(-20),
          stream: true,
        }),
        signal: controller.signal,
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error("HTTP " + res.status + ": " + errText.slice(0, 300));
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let assistantContent = "";
      let gotContent = false;

      setConversations((prev) => ({
        ...prev,
        [targetAi]: [...(prev[targetAi] || []), { role: "assistant", content: "" }],
      }));

      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || !trimmed.startsWith("data: ")) continue;
          const data = trimmed.slice(6);
          if (data === "[DONE]") continue;
          try {
            const parsed = JSON.parse(data);
            if (parsed.error) {
              throw new Error(parsed.error.message || JSON.stringify(parsed.error));
            }
            const delta = parsed.choices?.[0]?.delta?.content;
            if (delta) {
              gotContent = true;
              assistantContent += delta;
              const snap = assistantContent;
              setConversations((prev) => {
                const msgs = [...(prev[targetAi] || [])];
                msgs[msgs.length - 1] = { role: "assistant", content: snap };
                return { ...prev, [targetAi]: msgs };
              });
            }
          } catch (e) {
            if (e.message && !e.message.includes("JSON")) throw e;
          }
        }
      }

      if (!gotContent) {
        setConversations((prev) => {
          const msgs = [...(prev[targetAi] || [])];
          msgs[msgs.length - 1] = {
            role: "assistant",
            content: "⚠️ AI 没有返回任何内容。请检查该 AI 的模型配置（AI 档案页 → 模型设置）。",
          };
          return { ...prev, [targetAi]: msgs };
        });
      }

      // Process [draw:xxx] tags if present
      if (assistantContent.includes("[draw:")) {
        const drawPattern = /\[draw:(.*?)\]/g;
        const drawMatches = [...assistantContent.matchAll(drawPattern)];
        if (drawMatches.length > 0) {
          setConversations((prev) => {
            const msgs = [...(prev[targetAi] || [])];
            msgs[msgs.length - 1] = { role: "assistant", content: assistantContent.replace(drawPattern, "🎨 画图中...") };
            return { ...prev, [targetAi]: msgs };
          });
          let processed = assistantContent;
          for (const dm of drawMatches) {
            try {
              const drawRes = await fetch("/api/draw", {
                method: "POST",
                headers: { "Content-Type": "application/json", Authorization: `Bearer ${secret}` },
                body: JSON.stringify({ prompt: dm[1], ai_id: requestAi }),
              });
              const drawData = await drawRes.json();
              if (drawData.url) {
                processed = processed.replace(dm[0], `[img]${drawData.url}[/img]`);
              } else {
                processed = processed.replace(dm[0], `（画图失败: ${drawData.error || "未知错误"}）`);
              }
            } catch {
              processed = processed.replace(dm[0], "（画图失败）");
            }
          }
          setConversations((prev) => {
            const msgs = [...(prev[targetAi] || [])];
            msgs[msgs.length - 1] = { role: "assistant", content: processed };
            return { ...prev, [targetAi]: msgs };
          });
        }
      }
    } catch (err) {
      if (err.name === "AbortError") return;
      setConversations((prev) => {
        const msgs = [...(prev[targetAi] || [])];
        if (msgs.length > 0 && msgs[msgs.length - 1].role === "assistant" && !msgs[msgs.length - 1].content) {
          msgs[msgs.length - 1] = {
            role: "assistant",
            content: "⚠️ " + err.message + "\n\n请去 AI 档案页配置模型和 API Key。",
          };
        } else {
          msgs.push({
            role: "assistant",
            content: "⚠️ " + err.message + "\n\n请去 AI 档案页配置模型和 API Key。",
          });
        }
        return { ...prev, [targetAi]: msgs };
      });
    }
    setLoading(false);
    abortRef.current = null;
    inputRef.current?.focus();
  }, [input, loading, aiId, conversations]);

  const draw = useCallback(async () => {
    const text = input.trim();
    if (!text || loading || drawing || !aiId) return;
    const targetAi = aiId;
    const requestAi = hubRequestAiId(targetAi);
    setInput("");
    setConversations((prev) => ({
      ...prev,
      [targetAi]: [...(prev[targetAi] || []), { role: "user", content: `🎨 画画：${text}` }],
    }));
    setDrawing(true);
    setConversations((prev) => ({
      ...prev,
      [targetAi]: [...(prev[targetAi] || []), { role: "assistant", content: "🎨 正在画画..." }],
    }));
    try {
      const secret = localStorage.getItem("mh-secret") || "";
      const res = await fetch("/api/draw", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${secret}` },
        body: JSON.stringify({ prompt: text, ai_id: requestAi }),
      });
      const data = await res.json();
      if (data.error) {
        setConversations((prev) => {
          const msgs = [...(prev[targetAi] || [])];
          msgs[msgs.length - 1] = { role: "assistant", content: `⚠️ ${data.error}${data.text_reply ? "\n\n" + data.text_reply : ""}` };
          return { ...prev, [targetAi]: msgs };
        });
      } else if (data.url) {
        setConversations((prev) => {
          const msgs = [...(prev[targetAi] || [])];
          msgs[msgs.length - 1] = { role: "assistant", content: `[img]${data.url}[/img]` };
          return { ...prev, [targetAi]: msgs };
        });
      }
    } catch (err) {
      setConversations((prev) => {
        const msgs = [...(prev[targetAi] || [])];
        msgs[msgs.length - 1] = { role: "assistant", content: `⚠️ 画图失败: ${err.message}` };
        return { ...prev, [targetAi]: msgs };
      });
    }
    setDrawing(false);
    inputRef.current?.focus();
  }, [input, loading, drawing, aiId]);

  if (!currentAi) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", maxWidth: 720, margin: "0 auto" }}>
      {/* AI Picker Header */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: "var(--space-sm) 0", position: "relative",
      }}>
        <div style={{ position: "absolute", left: 0, display: "flex", gap: 6 }}>
          <button onClick={() => { setSelectMode((v) => !v); setSelectedMessages({}); }}
            title={selectMode ? "退出选择" : "选择消息"}
            style={{
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              width: 34, height: 34, border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-md)", background: selectMode ? "var(--bg-hover)" : "var(--bg-card)",
              color: "var(--text-primary)", cursor: "pointer",
            }}>
            {selectMode ? <X size={16} /> : <CheckSquare size={16} />}
          </button>
          {selectMode && (
            <button onClick={deleteSelected} disabled={selectedKeys.length === 0}
              title="删除选中的消息"
              style={{
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                width: 34, height: 34, border: "1px solid var(--border-subtle)",
                borderRadius: "var(--radius-md)", background: "var(--bg-card)",
                color: selectedKeys.length ? "var(--danger, #dc2626)" : "var(--text-muted)",
                cursor: selectedKeys.length ? "pointer" : "not-allowed",
              }}>
              <Trash2 size={16} />
            </button>
          )}
        </div>

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

        <button onClick={clearCurrentChat} title="清空当前聊天"
          style={{
            position: "absolute", right: 0,
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            width: 34, height: 34, border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-md)", background: "var(--bg-card)",
            color: "var(--text-muted)", cursor: "pointer",
          }}>
          <Trash2 size={16} />
        </button>
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-md) 0" }}>
        {messages.map((msg, i) => (
          <MessageBubble
            key={`${aiId}-${i}`}
            message={msg}
            ai={currentAi}
            selectable={selectMode}
            selected={!!selectedMessages[`${aiId}:${i}`]}
            onToggle={() => toggleSelect(i)}
          />
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
        <button onClick={draw} disabled={loading || drawing || !input.trim()}
          title="让 AI 画画"
          style={{
            padding: "10px 12px", borderRadius: "var(--radius-md)", border: "none",
            background: drawing ? "var(--bg-hover)" : "var(--bg-card)",
            color: drawing ? "var(--primary)" : "var(--text-muted)",
            cursor: loading || drawing ? "not-allowed" : "pointer",
          }}>
          {drawing ? <Loader2 size={18} className="spin" /> : <Paintbrush size={18} />}
        </button>
        <button className="btn btn-primary" onClick={send} disabled={loading}
          style={{ padding: "10px 14px", borderRadius: "var(--radius-md)" }}>
          {loading ? <Loader2 size={18} className="spin" /> : <Send size={18} />}
        </button>
      </div>
    </div>
  );
}

function MessageBubble({ message, ai, selectable = false, selected = false, onToggle }) {
  const isUser = message.role === "user";
  const isError = !isUser && message.content?.startsWith("⚠️");
  const hasImage = message.content?.includes("[img]");
  return (
    <div style={{
      display: "flex", justifyContent: isUser ? "flex-end" : "flex-start",
      marginBottom: "var(--space-sm)", padding: "0 var(--space-sm)",
      alignItems: "flex-end", gap: 6,
    }}>
      {selectable && !isUser && (
        <button onClick={onToggle} title={selected ? "取消选择" : "选择消息"}
          style={{
            width: 24, height: 24, border: "none", background: "transparent",
            color: selected ? "var(--primary)" : "var(--text-muted)",
            cursor: "pointer", padding: 0, marginBottom: 8,
          }}>
          {selected ? <CheckSquare size={18} /> : <Square size={18} />}
        </button>
      )}
      {!isUser && (
        <span style={{ fontSize: 20, marginBottom: 4, flexShrink: 0 }}>{ai.emoji}</span>
      )}
      <div style={{
        maxWidth: "78%",
        padding: "10px 14px",
        borderRadius: isUser
          ? "var(--radius-lg) var(--radius-lg) var(--radius-sm) var(--radius-lg)"
          : "var(--radius-lg) var(--radius-lg) var(--radius-lg) var(--radius-sm)",
        background: isError ? "rgba(239, 68, 68, 0.1)" : isUser ? "var(--primary)" : "var(--bg-card)",
        color: isError ? "var(--text-primary)" : isUser ? "var(--text-on-primary)" : "var(--text-primary)",
        backdropFilter: isUser ? "none" : "blur(var(--glass-blur))",
        border: isError ? "1px solid rgba(239, 68, 68, 0.3)" : isUser ? "none" : "1px solid var(--glass-border)",
        fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word",
        overflow: "hidden",
        outline: selected ? "2px solid var(--primary)" : "none",
        cursor: selectable ? "pointer" : "default",
      }}>
        <div onClick={selectable ? onToggle : undefined}>
          {hasImage ? <RichContent text={message.content} /> : (message.content || "...")}
        </div>
      </div>
      {selectable && isUser && (
        <button onClick={onToggle} title={selected ? "取消选择" : "选择消息"}
          style={{
            width: 24, height: 24, border: "none", background: "transparent",
            color: selected ? "var(--primary)" : "var(--text-muted)",
            cursor: "pointer", padding: 0, marginBottom: 8,
          }}>
          {selected ? <CheckSquare size={18} /> : <Square size={18} />}
        </button>
      )}
    </div>
  );
}

function RichContent({ text }) {
  if (!text) return null;
  const parts = text.split(/(\[img\].*?\[\/img\])/g);
  return parts.map((part, i) => {
    const m = part.match(/^\[img\](.*?)\[\/img\]$/);
    if (m) {
      return <img key={i} src={m[1]} alt="AI 画的图" style={{
        maxWidth: "100%", borderRadius: "var(--radius-md)", marginTop: 4, display: "block",
      }} />;
    }
    return part ? <span key={i}>{part}</span> : null;
  });
}
