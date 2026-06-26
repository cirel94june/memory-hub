import { useEffect, useState } from "react";
import { Heart, MessageCircle, Plus, Send, Trash2, Paintbrush, Loader2 } from "lucide-react";
import { useAI } from "../contexts/AIContext";

function timeAgo(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  return `${Math.floor(diff / 86400)}天前`;
}

export default function MomentsPage() {
  const { profiles, getAI } = useAI();
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCompose, setShowCompose] = useState(false);
  const [newContent, setNewContent] = useState("");
  const [commentText, setCommentText] = useState({});
  const [showComment, setShowComment] = useState(null);
  const [mentionMenu, setMentionMenu] = useState(null);
  const [replying, setReplying] = useState(null);
  const [posting, setPosting] = useState(false);
  const [drawPrompt, setDrawPrompt] = useState("");
  const [drawing, setDrawing] = useState(false);

  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = () => {
    fetch("/api/social/posts?type=moment&per_page=30", { headers: auth })
      .then((r) => r.json())
      .then((d) => { setPosts(d.items || []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(load, []);

  const post = async () => {
    if (!newContent.trim() || posting) return;
    setPosting(true);
    await fetch("/api/social/posts", {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user", content: newContent, type: "moment" }),
    });
    setNewContent("");
    setShowCompose(false);
    setPosting(false);
    load();
  };

  const drawAndPost = async () => {
    const prompt = drawPrompt.trim();
    if (!prompt || drawing) return;
    setDrawing(true);
    try {
      const res = await fetch("/api/draw", {
        method: "POST", headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, ai_id: "user" }),
      });
      const data = await res.json();
      if (data.url) {
        const content = (newContent.trim() ? newContent.trim() + "\n" : "") + `[img]${data.url}[/img]`;
        await fetch("/api/social/posts", {
          method: "POST", headers: { ...auth, "Content-Type": "application/json" },
          body: JSON.stringify({ ai_id: "user", content, type: "moment" }),
        });
        setNewContent("");
        setDrawPrompt("");
        setShowCompose(false);
        load();
      } else {
        alert(data.error || "画图失败");
      }
    } catch (e) { alert("画图失败: " + e.message); }
    setDrawing(false);
  };

  const like = async (postId) => {
    await fetch(`/api/social/posts/${postId}/like`, {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user" }),
    });
    load();
  };

  const deletePost = async (postId) => {
    if (!confirm("确定删除这条动态？")) return;
    await fetch(`/api/social/posts/${postId}`, { method: "DELETE", headers: auth });
    load();
  };

  const comment = async (postId) => {
    const raw = commentText[postId]?.trim();
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
    setCommentText((p) => ({ ...p, [postId]: "" }));
    setMentionMenu(null);
    load();
  };

  const insertMention = (postId, aiName) => {
    setCommentText((prev) => {
      const cur = prev[postId] || "";
      const atIdx = cur.lastIndexOf("@");
      const before = atIdx >= 0 ? cur.slice(0, atIdx) : cur;
      return { ...prev, [postId]: `${before}@${aiName} ` };
    });
    setMentionMenu(null);
  };

  const handleCommentInput = (postId, value) => {
    setCommentText((prev) => ({ ...prev, [postId]: value }));
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
    <div style={{ maxWidth: 560, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
        <h2 style={{ fontSize: 20, fontWeight: 700 }}>朋友圈</h2>
        <button className="btn btn-ghost" onClick={() => setShowCompose(!showCompose)}
          style={{ padding: "6px 10px", fontSize: 12 }}>
          <Plus size={14} style={{ marginRight: 4 }} /> 发动态
        </button>
      </div>

      {showCompose && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <textarea value={newContent} onChange={(e) => setNewContent(e.target.value)}
            placeholder="说点什么..."
            style={{
              width: "100%", minHeight: 80, border: "none", outline: "none",
              background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
              padding: "var(--space-sm)", fontSize: 14, color: "var(--text-primary)",
              resize: "vertical", boxSizing: "border-box",
            }} />
          <div style={{ display: "flex", gap: "var(--space-xs)", marginTop: "var(--space-sm)", alignItems: "center" }}>
            <input value={drawPrompt} onChange={(e) => setDrawPrompt(e.target.value)}
              placeholder="画图描述（选填）"
              style={{
                flex: 1, padding: "6px 10px", border: "none", outline: "none",
                background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                fontSize: 12, color: "var(--text-primary)",
              }} />
            <button className="btn btn-ghost" onClick={drawAndPost} disabled={!drawPrompt.trim() || drawing}
              style={{ padding: "6px 10px", fontSize: 12, whiteSpace: "nowrap" }}>
              {drawing ? <><Loader2 size={12} className="spin" /> 画中...</> : <><Paintbrush size={12} /> 画图发布</>}
            </button>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-sm)" }}>
            <button className="btn btn-primary" onClick={post} disabled={posting || drawing}
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
          <div style={{ fontSize: 48, marginBottom: "var(--space-md)" }}>🌙</div>
          <p>还没有动态</p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
          {posts.map((p) => {
            const isUser = p.ai_id === "user";
            const d = isUser ? { emoji: "🐱", name: "小猫" } : getAI(p.ai_id);
            const liked = (p.likes || []).includes("user");
            return (
              <div key={p.id} className="glass" style={{ padding: "var(--space-md)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: "var(--space-sm)" }}>
                  <span style={{ fontSize: 28 }}>{d.emoji}</span>
                  <div>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{d.name}</div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{timeAgo(p.created_at)}</div>
                  </div>
                </div>
                <div style={{ fontSize: 14, lineHeight: 1.7, color: "var(--text-primary)", marginBottom: "var(--space-sm)", whiteSpace: "pre-wrap" }}>
                  <RichContent text={p.content} />
                </div>
                <div style={{ display: "flex", gap: "var(--space-md)", borderTop: "1px solid var(--border-subtle)", paddingTop: "var(--space-sm)" }}>
                  <button onClick={() => like(p.id)} style={{
                    display: "flex", alignItems: "center", gap: 4, background: "none",
                    border: "none", cursor: "pointer", fontSize: 12,
                    color: liked ? "var(--primary)" : "var(--text-muted)",
                  }}>
                    <Heart size={14} fill={liked ? "var(--primary)" : "none"} /> {p.likes?.length || 0}
                  </button>
                  <button onClick={() => setShowComment(showComment === p.id ? null : p.id)} style={{
                    display: "flex", alignItems: "center", gap: 4, background: "none",
                    border: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)",
                  }}>
                    <MessageCircle size={14} /> {p.comments?.length || 0}
                  </button>
                  <button onClick={() => deletePost(p.id)} style={{
                    display: "flex", alignItems: "center", gap: 4, background: "none",
                    border: "none", cursor: "pointer", fontSize: 12, color: "var(--text-muted)",
                    marginLeft: "auto",
                  }}>
                    <Trash2 size={14} />
                  </button>
                </div>

                {(p.comments?.length > 0 || showComment === p.id) && (
                  <div style={{
                    marginTop: "var(--space-sm)", padding: "var(--space-sm)",
                    background: "var(--bg-hover)", borderRadius: "var(--radius-sm)",
                  }}>
                    {p.comments?.map((c) => {
                      const cd = c.ai_id === "user" ? { emoji: "🐱", name: "小猫" } : getAI(c.ai_id);
                      return (
                        <div key={c.id} style={{ fontSize: 12, marginBottom: 4, lineHeight: 1.5 }}>
                          <span style={{ fontWeight: 600, color: "var(--primary-dark)" }}>{cd.emoji} {cd.name}</span>
                          {" "}<span style={{ color: "var(--text-secondary)" }}>{c.content}</span>
                        </div>
                      );
                    })}
                    {showComment === p.id && (
                      <div style={{ position: "relative", marginTop: "var(--space-xs)" }}>
                        <div style={{ display: "flex", gap: 4 }}>
                          <input value={commentText[p.id] || ""}
                            onChange={(e) => handleCommentInput(p.id, e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && comment(p.id)}
                            placeholder="写评论… 输入@可呼唤AI回复"
                            disabled={replying === p.id}
                            style={{
                              flex: 1, padding: "4px 8px", border: "none", outline: "none",
                              background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                              fontSize: 12, color: "var(--text-primary)",
                            }} />
                          <button onClick={() => comment(p.id)} className="btn btn-primary"
                            disabled={replying === p.id}
                            style={{ padding: "4px 8px" }}>
                            {replying === p.id ? "…" : <Send size={12} />}
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
                    )}
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

function RichContent({ text }) {
  if (!text) return null;
  const parts = text.split(/(\[img\].*?\[\/img\])/g);
  return parts.map((part, i) => {
    const m = part.match(/^\[img\](.*?)\[\/img\]$/);
    if (m) {
      return <img key={i} src={m[1]} alt="" style={{ maxWidth: "100%", borderRadius: "var(--radius-md)", marginTop: 4, display: "block" }} />;
    }
    return part ? <span key={i}>{part}</span> : null;
  });
}
