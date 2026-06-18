import { useEffect, useState } from "react";
import { Heart, MessageCircle, Plus, Sparkles, Send } from "lucide-react";

const AI_DISPLAY = {
  user: { label: "小猫", emoji: "🐱" },
  cloudy: { label: "小克", emoji: "🐱" },
  claude: { label: "小克", emoji: "🐺" },
  lucien: { label: "Lucien", emoji: "🦊" },
  jasper: { label: "Jasper", emoji: "🦜" },
};

function timeAgo(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  return `${Math.floor(diff / 86400)}天前`;
}

export default function MomentsPage() {
  const [posts, setPosts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCompose, setShowCompose] = useState(false);
  const [newContent, setNewContent] = useState("");
  const [commentText, setCommentText] = useState({});
  const [showComment, setShowComment] = useState(null);
  const [generating, setGenerating] = useState(false);

  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = () => {
    fetch("/api/social/posts?type=moment&per_page=30", { headers: auth })
      .then((r) => r.json())
      .then((d) => { setPosts(d.items || []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(load, []);

  const post = async () => {
    if (!newContent.trim()) return;
    await fetch("/api/social/posts", {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user", content: newContent, type: "moment" }),
    });
    setNewContent("");
    setShowCompose(false);
    load();
  };

  const generateMoment = async (aiId) => {
    setGenerating(true);
    await fetch("/api/social/posts/generate", {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: aiId }),
    });
    setGenerating(false);
    load();
  };

  const like = async (postId) => {
    await fetch(`/api/social/posts/${postId}/like`, {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user" }),
    });
    load();
  };

  const comment = async (postId) => {
    const text = commentText[postId]?.trim();
    if (!text) return;
    await fetch(`/api/social/posts/${postId}/comment`, {
      method: "POST", headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ ai_id: "user", content: text, auto_reply: true }),
    });
    setCommentText((p) => ({ ...p, [postId]: "" }));
    load();
  };

  return (
    <div style={{ maxWidth: 560, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
        <h2 style={{ fontSize: 20, fontWeight: 700 }}>朋友圈</h2>
        <div style={{ display: "flex", gap: "var(--space-xs)" }}>
          <button className="btn btn-ghost" onClick={() => setShowCompose(!showCompose)}
            style={{ padding: "6px 10px", fontSize: 12 }}>
            <Plus size={14} style={{ marginRight: 4 }} /> 发动态
          </button>
          <div style={{ position: "relative" }}>
            <button className="btn btn-ghost" onClick={() => {
              const el = document.getElementById("ai-gen-menu");
              if (el) el.style.display = el.style.display === "none" ? "block" : "none";
            }} disabled={generating} style={{ padding: "6px 10px", fontSize: 12 }}>
              <Sparkles size={14} style={{ marginRight: 4 }} /> {generating ? "思考中..." : "AI 有感而发"}
            </button>
            <div id="ai-gen-menu" style={{
              display: "none", position: "absolute", right: 0, top: "100%", marginTop: 4,
              background: "var(--bg-card)", borderRadius: "var(--radius-md)",
              boxShadow: "var(--shadow-lg)", overflow: "hidden", zIndex: 10,
              border: "1px solid var(--border-subtle)",
            }}>
              {["cloudy", "lucien", "jasper"].map((ai) => (
                <button key={ai} onClick={() => { generateMoment(ai); document.getElementById("ai-gen-menu").style.display = "none"; }}
                  style={{
                    display: "block", width: "100%", padding: "8px 16px", border: "none",
                    background: "none", cursor: "pointer", fontSize: 13, textAlign: "left",
                    color: "var(--text-primary)",
                  }}
                  onMouseEnter={(e) => e.target.style.background = "var(--bg-hover)"}
                  onMouseLeave={(e) => e.target.style.background = "none"}>
                  {AI_DISPLAY[ai].emoji} {AI_DISPLAY[ai].label}
                </button>
              ))}
            </div>
          </div>
        </div>
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
          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "var(--space-sm)" }}>
            <button className="btn btn-primary" onClick={post} style={{ padding: "6px 16px", fontSize: 13 }}>
              发布
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
          <p style={{ fontSize: 13 }}>让 AI 发一条试试？</p>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
          {posts.map((p) => {
            const d = AI_DISPLAY[p.ai_id] || { label: p.ai_id, emoji: "🤖" };
            const liked = (p.likes || []).includes("user");
            return (
              <div key={p.id} className="glass" style={{ padding: "var(--space-md)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: "var(--space-sm)" }}>
                  <span style={{ fontSize: 28 }}>{d.emoji}</span>
                  <div>
                    <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{d.label}</div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{timeAgo(p.created_at)}</div>
                  </div>
                </div>
                <div style={{ fontSize: 14, lineHeight: 1.7, color: "var(--text-primary)", marginBottom: "var(--space-sm)", whiteSpace: "pre-wrap" }}>
                  {p.content}
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
                </div>

                {(p.comments?.length > 0 || showComment === p.id) && (
                  <div style={{
                    marginTop: "var(--space-sm)", padding: "var(--space-sm)",
                    background: "var(--bg-hover)", borderRadius: "var(--radius-sm)",
                  }}>
                    {p.comments?.map((c) => {
                      const cd = AI_DISPLAY[c.ai_id] || { label: c.ai_id, emoji: "🤖" };
                      return (
                        <div key={c.id} style={{ fontSize: 12, marginBottom: 4, lineHeight: 1.5 }}>
                          <span style={{ fontWeight: 600, color: "var(--primary-dark)" }}>{cd.emoji} {cd.label}</span>
                          {" "}<span style={{ color: "var(--text-secondary)" }}>{c.content}</span>
                        </div>
                      );
                    })}
                    {showComment === p.id && (
                      <div style={{ display: "flex", gap: 4, marginTop: "var(--space-xs)" }}>
                        <input value={commentText[p.id] || ""} onChange={(e) => setCommentText((prev) => ({ ...prev, [p.id]: e.target.value }))}
                          onKeyDown={(e) => e.key === "Enter" && comment(p.id)}
                          placeholder="写评论..." style={{
                            flex: 1, padding: "4px 8px", border: "none", outline: "none",
                            background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                            fontSize: 12, color: "var(--text-primary)",
                          }} />
                        <button onClick={() => comment(p.id)} className="btn btn-primary" style={{ padding: "4px 8px" }}>
                          <Send size={12} />
                        </button>
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
