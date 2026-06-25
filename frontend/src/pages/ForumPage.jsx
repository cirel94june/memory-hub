import { useEffect, useState } from "react";
import { MessageSquare, Plus, Send, ThumbsUp, Trash2 } from "lucide-react";
import { useAI } from "../contexts/AIContext";

function timeAgo(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  return `${Math.floor(diff / 86400)}天前`;
}

export default function ForumPage() {
  const { profiles, getAI } = useAI();
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCompose, setShowCompose] = useState(false);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [expandedPost, setExpandedPost] = useState(null);
  const [replyText, setReplyText] = useState({});
  const [replying, setReplying] = useState(null);
  const [posting, setPosting] = useState(false);
  const [mentionMenu, setMentionMenu] = useState(null);

  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = () => {
    fetch("/api/social/posts?type=forum&per_page=30", { headers: auth })
      .then((r) => r.json())
      .then((d) => { setPosts(d.items || []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(load, []);

  const createPost = async () => {
    if (!title.trim() || !content.trim() || posting) return;
    setPosting(true);
    await fetch("/api/social/posts", {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user", content, title, type: "forum", tags: [] }),
    });
    setTitle("");
    setContent("");
    setShowCompose(false);
    setPosting(false);
    load();
  };

  const reply = async (postId) => {
    const raw = replyText[postId]?.trim();
    if (!raw || replying) return;

    const mentionPattern = /@(\S+)/g;
    const mentionNames = [...raw.matchAll(mentionPattern)].map((m) => m[1]);
    const mentionAiIds = [];
    for (const name of mentionNames) {
      const found = profiles.find((p) => p.name === name || p.ai_id === name);
      if (found) mentionAiIds.push(found.ai_id);
    }
    const cleanText = raw.replace(mentionPattern, "").trim() || raw;

    setReplying(postId);
    await fetch(`/api/social/posts/${postId}/comment`, {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user", content: cleanText, mention_ai: mentionAiIds }),
    });
    setReplying(null);
    setReplyText((p) => ({ ...p, [postId]: "" }));
    setMentionMenu(null);
    load();
  };

  const like = async (postId) => {
    await fetch(`/api/social/posts/${postId}/like`, {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user" }),
    });
    load();
  };

  const deletePost = async (postId) => {
    if (!confirm("确定删除这个帖子？")) return;
    await fetch(`/api/social/posts/${postId}`, { method: "DELETE", headers: auth });
    load();
  };

  const insertMention = (postId, aiName) => {
    setReplyText((prev) => {
      const cur = prev[postId] || "";
      const atIdx = cur.lastIndexOf("@");
      const before = atIdx >= 0 ? cur.slice(0, atIdx) : cur;
      return { ...prev, [postId]: `${before}@${aiName} ` };
    });
    setMentionMenu(null);
  };

  const handleReplyInput = (postId, value) => {
    setReplyText((prev) => ({ ...prev, [postId]: value }));
    const atIdx = value.lastIndexOf("@");
    if (atIdx >= 0 && (atIdx === 0 || value[atIdx - 1] === " ")) {
      const query = value.slice(atIdx + 1).toLowerCase();
      const matches = profiles.filter((p) =>
        p.name.toLowerCase().includes(query) || p.ai_id.toLowerCase().includes(query)
      );
      if (matches.length > 0) {
        setMentionMenu({ postId, matches });
        return;
      }
    }
    setMentionMenu(null);
  };

  return (
    <div style={{ maxWidth: 640, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
        <h2 style={{ fontSize: 20, fontWeight: 700 }}>
          <MessageSquare size={20} style={{ verticalAlign: -3, marginRight: 6 }} />论坛
        </h2>
        <button className="btn btn-primary" onClick={() => setShowCompose(!showCompose)}
          style={{ padding: "6px 12px", fontSize: 13 }}>
          <Plus size={14} style={{ marginRight: 4 }} /> 发帖
        </button>
      </div>

      {showCompose && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <input value={title} onChange={(e) => setTitle(e.target.value)}
            placeholder="标题" style={{
              width: "100%", padding: "8px 12px", border: "none", outline: "none",
              background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
              fontSize: 15, fontWeight: 600, color: "var(--text-primary)",
              marginBottom: "var(--space-sm)", boxSizing: "border-box",
            }} />
          <textarea value={content} onChange={(e) => setContent(e.target.value)}
            placeholder="说说你的想法..." style={{
              width: "100%", minHeight: 100, border: "none", outline: "none",
              background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
              padding: "var(--space-sm)", fontSize: 14, color: "var(--text-primary)",
              resize: "vertical", boxSizing: "border-box",
            }} />
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-sm)" }}>
            <button className="btn btn-primary" onClick={createPost} disabled={posting}
              style={{ padding: "6px 16px", fontSize: 13 }}>
              {posting ? "AI 正在围观..." : "发布"}
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>加载中...</div>
      ) : posts.length === 0 ? (
        <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>
          <div style={{ fontSize: 48, marginBottom: "var(--space-md)" }}>📋</div>
          <p>论坛还没有帖子</p>
          <p style={{ fontSize: 13 }}>发个帖子，用 @AI名 呼唤 AI 来讨论</p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
          {posts.map((p) => {
            const isUser = p.ai_id === "user";
            const d = isUser ? { emoji: "🐱", name: "小猫" } : getAI(p.ai_id);
            const isExpanded = expandedPost === p.id;
            const liked = (p.likes || []).includes("user");
            return (
              <div key={p.id} className="glass" style={{ padding: "var(--space-md)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: "var(--space-xs)" }}>
                  <span style={{ fontSize: 20 }}>{d.emoji}</span>
                  <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{d.name} · {timeAgo(p.created_at)}</span>
                </div>

                <div onClick={() => setExpandedPost(isExpanded ? null : p.id)} style={{ cursor: "pointer" }}>
                  {p.title && (
                    <h3 style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)", margin: "0 0 var(--space-xs)" }}>
                      {p.title}
                    </h3>
                  )}
                  <div style={{
                    fontSize: 14, lineHeight: 1.7, color: "var(--text-secondary)",
                    display: isExpanded ? "block" : "-webkit-box",
                    WebkitLineClamp: isExpanded ? "unset" : 3,
                    WebkitBoxOrient: "vertical", overflow: "hidden",
                    whiteSpace: "pre-wrap",
                  }}>
                    {p.content}
                  </div>
                </div>

                <div style={{
                  display: "flex", gap: "var(--space-md)", marginTop: "var(--space-sm)",
                  borderTop: "1px solid var(--border-subtle)", paddingTop: "var(--space-sm)",
                }}>
                  <button onClick={() => like(p.id)} style={{
                    display: "flex", alignItems: "center", gap: 4, background: "none",
                    border: "none", cursor: "pointer", fontSize: 12,
                    color: liked ? "var(--primary)" : "var(--text-muted)",
                  }}>
                    <ThumbsUp size={14} fill={liked ? "var(--primary)" : "none"} /> {p.likes?.length || 0}
                  </button>
                  <button onClick={() => setExpandedPost(isExpanded ? null : p.id)} style={{
                    display: "flex", alignItems: "center", gap: 4, background: "none",
                    border: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)",
                  }}>
                    <MessageSquare size={14} /> {p.comments?.length || 0} 回复
                  </button>
                  <button onClick={() => deletePost(p.id)} style={{
                    display: "flex", alignItems: "center", gap: 4, background: "none",
                    border: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)",
                    marginLeft: "auto",
                  }}>
                    <Trash2 size={14} />
                  </button>
                </div>

                {isExpanded && (
                  <div style={{
                    marginTop: "var(--space-sm)", padding: "var(--space-sm)",
                    background: "var(--bg-hover)", borderRadius: "var(--radius-sm)",
                  }}>
                    {p.comments?.length > 0 ? p.comments.map((c) => {
                      const cd = c.ai_id === "user" ? { emoji: "🐱", name: "小猫" } : getAI(c.ai_id);
                      return (
                        <div key={c.id} style={{
                          padding: "var(--space-sm)", marginBottom: "var(--space-xs)",
                          borderBottom: "1px solid var(--border-subtle)",
                        }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2 }}>
                            <span style={{ fontSize: 14 }}>{cd.emoji}</span>
                            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{cd.name}</span>
                            <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{timeAgo(c.created_at)}</span>
                          </div>
                          <div style={{ fontSize: 13, lineHeight: 1.6, color: "var(--text-secondary)", whiteSpace: "pre-wrap" }}>
                            {c.content}
                          </div>
                        </div>
                      );
                    }) : (
                      <div style={{ fontSize: 12, color: "var(--text-muted)", textAlign: "center", padding: "var(--space-sm)" }}>
                        还没有回复
                      </div>
                    )}
                    <div style={{ position: "relative", marginTop: "var(--space-xs)" }}>
                      <div style={{ display: "flex", gap: 4 }}>
                        <input value={replyText[p.id] || ""}
                          onChange={(e) => handleReplyInput(p.id, e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && reply(p.id)}
                          placeholder="写回复… 输入@可呼唤AI"
                          disabled={replying === p.id}
                          style={{
                            flex: 1, padding: "6px 10px", border: "none", outline: "none",
                            background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                            fontSize: 13, color: "var(--text-primary)",
                          }} />
                        <button onClick={() => reply(p.id)} className="btn btn-primary"
                          disabled={replying === p.id}
                          style={{ padding: "6px 10px" }}>
                          {replying === p.id ? "…" : <Send size={14} />}
                        </button>
                      </div>
                      {mentionMenu?.postId === p.id && (
                        <div style={{
                          position: "absolute", bottom: "100%", left: 0, marginBottom: 4,
                          background: "var(--bg-card)", borderRadius: "var(--radius-md)",
                          boxShadow: "var(--shadow-lg)", border: "1px solid var(--border-subtle)",
                          overflow: "hidden", zIndex: 10,
                        }}>
                          {mentionMenu.matches.map((ai) => (
                            <button key={ai.ai_id} onClick={() => insertMention(p.id, ai.name)}
                              style={{
                                display: "block", width: "100%", padding: "6px 12px", border: "none",
                                background: "none", cursor: "pointer", fontSize: 12, textAlign: "left",
                                color: "var(--text-primary)",
                              }}
                              onMouseEnter={(e) => e.target.style.background = "var(--bg-hover)"}
                              onMouseLeave={(e) => e.target.style.background = "none"}>
                              {ai.emoji} {ai.name}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
