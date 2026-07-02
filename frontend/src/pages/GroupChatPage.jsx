import { useEffect, useState, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { Send, Plus, ArrowLeft, Users, Loader, Settings, Reply, Trash2, X } from "lucide-react";
import { useAI } from "../contexts/AIContext";

const USER_DISPLAY = { ai_id: "user", name: "小猫", emoji: "🐱", color: "hsl(330, 65%, 55%)" };

export default function GroupChatPage() {
  const { profiles, getAI } = useAI();
  const [searchParams, setSearchParams] = useSearchParams();
  const chatId = searchParams.get("id");

  const [groups, setGroups] = useState([]);
  const [messages, setMessages] = useState([]);
  const [group, setGroup] = useState(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [showInfo, setShowInfo] = useState(false);
  const [newName, setNewName] = useState("");
  const [selectedMembers, setSelectedMembers] = useState({});
  const [replyTo, setReplyTo] = useState(null);
  const [activity, setActivity] = useState([]);
  const messagesEnd = useRef(null);

  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const loadGroups = () => {
    fetch("/api/social/groups", { headers: auth })
      .then((r) => r.json())
      .then((d) => setGroups(d.groups || []))
      .catch(() => {});
  };

  const loadMessages = (id) => {
    fetch(`/api/social/groups/${id}/messages?per_page=100`, { headers: auth })
      .then((r) => r.json())
      .then((d) => setMessages(d.messages || []))
      .catch(() => {});
    fetch(`/api/social/groups/${id}`, { headers: auth })
      .then((r) => r.json())
      .then(setGroup)
      .catch(() => {});
  };

  useEffect(loadGroups, []);
  useEffect(() => {
    if (chatId) loadMessages(chatId);
  }, [chatId]);
  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const openCreate = () => {
    const sel = {};
    profiles.forEach((p) => { sel[p.ai_id] = true; });
    setSelectedMembers(sel);
    setShowCreate(true);
  };

  const toggleMember = (aiId) => {
    setSelectedMembers((prev) => ({ ...prev, [aiId]: !prev[aiId] }));
  };

  const createGroup = async () => {
    if (!newName.trim()) return;
    const members = ["user", ...profiles.filter((p) => selectedMembers[p.ai_id]).map((p) => p.ai_id)];
    if (members.length < 2) return;
    const resp = await fetch("/api/social/groups", {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName, members }),
    });
    const d = await resp.json();
    setNewName("");
    setShowCreate(false);
    loadGroups();
    setSearchParams({ id: d.id });
  };

  const sendMsg = async () => {
    if (!input.trim() || sending) return;
    const text = input;
    const target = replyTo;
    const mentionPattern = /@(\S+)/g;
    const mentionAiIds = [...text.matchAll(mentionPattern)].map((m) => {
      const key = m[1].toLowerCase();
      const found = profiles.find((p) => (p.name || "").toLowerCase() === key || (p.ai_id || "").toLowerCase() === key);
      return found?.ai_id;
    }).filter(Boolean);
    const tempId = Date.now();
    setInput("");
    setReplyTo(null);
    setSending(true);

    setMessages((prev) => [...prev, {
      id: tempId, chat_id: parseInt(chatId), ai_id: "user",
      content: text, reply_to: target?.id || null, created_at: new Date().toISOString(),
    }]);

    try {
      const resp = await fetch(`/api/social/groups/${chatId}/messages`, {
        method: "POST", headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({ ai_id: "user", content: text, mention_ai: mentionAiIds, reply_to: target?.id || null }),
      });
      const d = await resp.json();
      if (d.id) {
        setMessages((prev) => prev.map((m) => m.id === tempId ? { ...m, id: d.id } : m));
      }
      if (d.ai_replies) {
        setMessages((prev) => [
          ...prev,
          ...d.ai_replies.map((r) => ({
            id: r.id, chat_id: parseInt(chatId), ai_id: r.ai_id,
            content: r.content, reply_to: r.reply_to || null, created_at: new Date().toISOString(),
          })),
        ]);
      }
      if (d.trace?.length) setActivity(d.trace);
    } catch (e) {
      console.error(e);
    }
    setSending(false);
  };

  const deleteMessage = async (messageId) => {
    if (!confirm("删除这条消息？")) return;
    await fetch(`/api/social/groups/messages/${messageId}`, { method: "DELETE", headers: auth });
    setMessages((prev) => prev.filter((m) => m.id !== messageId).map((m) => m.reply_to === messageId ? { ...m, reply_to: null } : m));
  };

  const mentionMember = (memberId) => {
    const d = getAI(memberId);
    setInput((prev) => `${prev}${prev && !prev.endsWith(" ") ? " " : ""}@${d.name || memberId} `);
  };

  // ── Group list view ──
  if (!chatId) {
    return (
      <div style={{ maxWidth: 560, margin: "0 auto" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
          <h2 style={{ fontSize: 20, fontWeight: 700 }}>
            <Users size={20} style={{ verticalAlign: -3, marginRight: 6 }} />群聊
          </h2>
          <button className="btn btn-primary" onClick={openCreate}
            style={{ padding: "6px 12px", fontSize: 13 }}>
            <Plus size={14} style={{ marginRight: 4 }} /> 新建群聊
          </button>
        </div>

        {showCreate && (
          <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
            <input value={newName} onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createGroup()}
              placeholder="群聊名称..." style={{
                width: "100%", padding: "8px 12px", border: "none", outline: "none",
                background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                fontSize: 14, color: "var(--text-primary)", boxSizing: "border-box",
                marginBottom: "var(--space-sm)",
              }} />
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-xs)" }}>
              选择群成员：
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: "var(--space-sm)" }}>
              <div style={{
                padding: "4px 10px", borderRadius: 14, fontSize: 12,
                background: "var(--primary)", color: "white",
              }}>🐱 小猫 (你)</div>
              {profiles.map((p) => {
                const on = selectedMembers[p.ai_id];
                return (
                  <button key={p.ai_id} onClick={() => toggleMember(p.ai_id)}
                    style={{
                      padding: "4px 10px", borderRadius: 14, fontSize: 12, border: "none", cursor: "pointer",
                      background: on ? `${p.color || "var(--primary)"}22` : "var(--glass-bg, rgba(255,255,255,0.06))",
                      color: on ? (p.color || "var(--primary)") : "var(--text-muted)",
                      outline: on ? `1.5px solid ${p.color || "var(--primary)"}` : "1px solid var(--border-subtle)",
                    }}>
                    {p.emoji} {p.name}
                  </button>
                );
              })}
            </div>
            <button className="btn btn-primary" onClick={createGroup} style={{ padding: "6px 16px", fontSize: 13 }}>
              创建
            </button>
          </div>
        )}

        {groups.length === 0 ? (
          <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>
            <div style={{ fontSize: 48, marginBottom: "var(--space-md)" }}>💬</div>
            <p>还没有群聊</p>
            <p style={{ fontSize: 13 }}>创建一个，和 AI 们一起聊天吧</p>
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
            {groups.map((g) => (
              <div key={g.id} className="glass" style={{ padding: "var(--space-md)", cursor: "pointer" }}
                onClick={() => setSearchParams({ id: g.id })}
                onMouseEnter={(e) => e.currentTarget.style.transform = "translateY(-1px)"}
                onMouseLeave={(e) => e.currentTarget.style.transform = "none"}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)" }}>{g.name}</div>
                    <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                      {(g.members || []).map((m) => m === "user" ? "🐱" : getAI(m).emoji).join(" ")}
                      {" · "}{g.message_count || 0} 条消息
                    </div>
                  </div>
                  {g.last_message && (
                    <div style={{ fontSize: 11, color: "var(--text-muted)", maxWidth: 160, textAlign: "right", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {g.last_message.ai_id === "user" ? "小猫" : getAI(g.last_message.ai_id).name}: {g.last_message.content}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  // ── Chat view ──
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 120px)", maxWidth: 640, margin: "0 auto" }}>
      {/* Header */}
      <div style={{
        display: "flex", alignItems: "center", gap: "var(--space-sm)",
        padding: "var(--space-sm) 0", borderBottom: "1px solid var(--border-subtle)",
        marginBottom: "var(--space-sm)", flexShrink: 0,
      }}>
        <button className="btn btn-ghost" onClick={() => { setSearchParams({}); setGroup(null); setMessages([]); setShowInfo(false); }}
          style={{ padding: "4px 8px" }}>
          <ArrowLeft size={16} />
        </button>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: "var(--text-primary)" }}>{group?.name || "群聊"}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
            {(group?.members || []).length} 人
          </div>
        </div>
        <button className="btn btn-ghost" onClick={() => setShowInfo(!showInfo)} style={{ padding: "4px 8px" }}>
          <Settings size={16} />
        </button>
      </div>

      {/* Info panel */}
      {showInfo && group && (
        <div className="glass" style={{
          padding: "var(--space-md)", marginBottom: "var(--space-sm)",
          flexShrink: 0,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: "var(--space-xs)" }}>
            群成员 ({(group.members || []).length})
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {(group.members || []).map((m) => {
              const d = m === "user" ? USER_DISPLAY : getAI(m);
              return (
                <div key={m} style={{
                  display: "flex", alignItems: "center", gap: 4,
                  padding: "4px 10px", borderRadius: 14, fontSize: 12,
                  background: "var(--glass-bg, rgba(255,255,255,0.06))",
                  border: `1px solid ${d.color || "var(--border-subtle)"}`,
                  color: "var(--text-primary)",
                }}>
                  <span>{d.emoji}</span>
                  <span>{d.name}</span>
                </div>
              );
            })}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: "var(--space-xs)" }}>
            创建于 {group.created_at?.slice(0, 10)}
          </div>
        </div>
      )}

      {/* Messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-sm) 0" }}>
        {messages.length === 0 && (
          <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)", fontSize: 13 }}>
            开始聊天吧！发送消息后 AI 会自动回复
          </div>
        )}
        {messages.map((m) => {
          const isUser = m.ai_id === "user";
          const d = m.ai_id === "user" ? USER_DISPLAY : getAI(m.ai_id);
          const parent = m.reply_to ? messages.find((item) => item.id === m.reply_to) : null;
          const parentName = parent ? (parent.ai_id === "user" ? "小猫" : getAI(parent.ai_id).name) : "";
          return (
            <div key={m.id} style={{
              display: "flex", flexDirection: isUser ? "row-reverse" : "row",
              gap: "var(--space-xs)", marginBottom: "var(--space-sm)",
              alignItems: "flex-start",
            }}>
              <span style={{ fontSize: 22, flexShrink: 0 }}>{d.emoji}</span>
              <div style={{
                maxWidth: "75%", padding: "8px 12px",
                borderRadius: isUser ? "var(--radius-md) var(--radius-md) 4px var(--radius-md)" : "var(--radius-md) var(--radius-md) var(--radius-md) 4px",
                background: isUser ? "var(--primary)" : "var(--bg-card)",
                color: isUser ? "white" : "var(--text-primary)",
                fontSize: 14, lineHeight: 1.6,
                border: isUser ? "none" : "1px solid var(--border-subtle)",
              }}>
                {!isUser && (
                  <div style={{ fontSize: 11, fontWeight: 600, color: d.color, marginBottom: 2 }}>{d.name}</div>
                )}
                {parent && (
                  <div style={{
                    fontSize: 11, opacity: 0.75, padding: "3px 6px", marginBottom: 4,
                    borderRadius: 6, background: isUser ? "rgba(255,255,255,0.18)" : "var(--bg-hover)",
                  }}>
                    回复 {parentName}: {parent.content.slice(0, 38)}
                  </div>
                )}
                <div style={{ whiteSpace: "pre-wrap" }}>{m.content}</div>
                <div style={{ fontSize: 10, marginTop: 2, opacity: 0.6, textAlign: "right" }}>
                  {m.created_at?.slice(11, 16)}
                </div>
                <div style={{ display: "flex", justifyContent: isUser ? "flex-start" : "flex-end", gap: 6, marginTop: 4, opacity: 0.7 }}>
                  <button onClick={() => setReplyTo(m)} title="回复" style={{ border: "none", background: "transparent", color: "inherit", cursor: "pointer", padding: 0 }}>
                    <Reply size={12} />
                  </button>
                  <button onClick={() => deleteMessage(m.id)} title="删除" style={{ border: "none", background: "transparent", color: "inherit", cursor: "pointer", padding: 0 }}>
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            </div>
          );
        })}
        {sending && (
          <div style={{ display: "flex", gap: "var(--space-xs)", alignItems: "center", padding: "var(--space-sm)", color: "var(--text-muted)", fontSize: 13 }}>
            <Loader size={14} style={{ animation: "spin 1s linear infinite" }} /> AI 们正在打字...
          </div>
        )}
        {activity.length > 0 && (
          <div style={{ fontSize: 11, color: "var(--text-muted)", padding: "0 var(--space-sm) var(--space-xs)" }}>
            {activity.map((a, idx) => {
              const d = getAI(a.ai_id);
              const reason = a.reason === "mentioned" ? "被 @ 后读取记忆并回复" : a.reason === "reply_target" ? "被回复后读取记忆并回复" : a.reason === "bot_mentioned" ? "被其他 AI @ 后回复" : "读取群聊上下文并回复";
              return <div key={`${a.ai_id}-${idx}`}>{d.emoji} {d.name}：{reason}</div>;
            })}
          </div>
        )}
        <div ref={messagesEnd} />
      </div>

      {/* Input */}
      <div style={{
        padding: "var(--space-sm) 0", borderTop: "1px solid var(--border-subtle)", flexShrink: 0,
      }}>
        {replyTo && (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 12, color: "var(--text-secondary)", marginBottom: 6 }}>
            <span>回复 {replyTo.ai_id === "user" ? "小猫" : getAI(replyTo.ai_id).name}: {replyTo.content.slice(0, 48)}</span>
            <button onClick={() => setReplyTo(null)} style={{ border: "none", background: "transparent", color: "var(--text-muted)", cursor: "pointer" }}><X size={14} /></button>
          </div>
        )}
        <div style={{ display: "flex", gap: 6, marginBottom: 6, overflowX: "auto" }}>
          {(group?.members || []).filter((m) => m !== "user").map((m) => {
            const d = getAI(m);
            return (
              <button key={m} onClick={() => mentionMember(m)}
                style={{
                  border: "1px solid var(--border-subtle)", background: "var(--bg-card)", color: "var(--text-secondary)",
                  borderRadius: 999, padding: "3px 8px", fontSize: 11, cursor: "pointer", whiteSpace: "nowrap",
                }}>
                @{d.name}
              </button>
            );
          })}
        </div>
        <div style={{ display: "flex", gap: "var(--space-sm)" }}>
          <input value={input} onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && sendMsg()}
            placeholder="说点什么，或 @某个 AI..." disabled={sending}
            style={{
              flex: 1, padding: "10px 14px", border: "none", outline: "none",
              background: "var(--bg-input)", borderRadius: "var(--radius-md)",
              fontSize: 14, color: "var(--text-primary)",
            }} />
          <button className="btn btn-primary" onClick={sendMsg} disabled={sending || !input.trim()}
            style={{ padding: "10px 14px", borderRadius: "var(--radius-md)" }}>
            <Send size={16} />
          </button>
        </div>
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
