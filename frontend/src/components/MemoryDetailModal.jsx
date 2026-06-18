import { useEffect, useState } from "react";
import { X, Clock, Zap, Tag, Link2, FileText, Heart, TrendingDown, Activity } from "lucide-react";
import Markdown from "react-markdown";

const ROOM_LABELS = {
  psychology: "心理", personality: "性格", health: "健康", career: "职业",
  relationships: "关系", relationship: "亲密", game_room: "游戏室",
  living_room: "客厅", preferences: "偏好", infra: "基建",
  infra_changelog: "更新日志", diary: "日记", work_tasks: "工作", social: "社交",
};

const AI_LABELS = { claude: "小克", lucien: "Lucien", jasper: "Jasper", import: "导入" };
const AI_EMOJI = { claude: "🐱", lucien: "🦊", jasper: "🦜", import: "📥" };

function DecayBar({ score, threshold }) {
  const pct = Math.round(score * 100);
  const color = score >= 0.6 ? "var(--success, #4caf50)" : score >= threshold ? "var(--warning, #ff9800)" : "var(--danger, #f44336)";
  const label = score >= 0.6 ? "健康" : score >= threshold ? "衰减中" : "即将归档";
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 4 }}>
        <span style={{ color: "var(--text-secondary)" }}>生命力</span>
        <span style={{ color, fontWeight: 600 }}>{label} {pct}%</span>
      </div>
      <div style={{ height: 6, background: "var(--bg-hover)", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: color, borderRadius: 3, transition: "width 0.4s ease" }} />
      </div>
    </div>
  );
}

function Section({ icon: Icon, title, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <div onClick={() => setOpen(!open)} style={{
        display: "flex", alignItems: "center", gap: 6, cursor: "pointer",
        padding: "6px 0", borderBottom: "1px solid var(--glass-border)",
        marginBottom: open ? "var(--space-sm)" : 0,
      }}>
        <Icon size={14} style={{ color: "var(--primary)" }} />
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", flex: 1 }}>{title}</span>
        <span style={{ fontSize: 11, color: "var(--text-muted)", transform: open ? "rotate(90deg)" : "none", transition: "transform 0.2s" }}>▶</span>
      </div>
      {open && children}
    </div>
  );
}

export default function MemoryDetailModal({ memoryId, onClose, onNavigate }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  useEffect(() => {
    if (!memoryId) return;
    setLoading(true);
    fetch(`/api/memory/${memoryId}/detail`, { headers: auth })
      .then((r) => r.json())
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [memoryId]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!memoryId) return null;

  const mem = data?.memory || {};
  const decay = data?.decay || {};
  const related = data?.related_memories || [];
  const chain = data?.supersede_chain || [];
  const tags = Array.isArray(mem.tags) ? mem.tags : [];
  const history = Array.isArray(mem.history) ? mem.history : [];

  const createdAt = mem.created_at ? new Date(mem.created_at).toLocaleString("zh-CN") : "";
  const eventDate = mem.event_date || "";
  const roomLabel = ROOM_LABELS[mem.room] || mem.room || "未分类";
  const aiLabel = AI_LABELS[mem.source_ai] || mem.source_ai || "";
  const aiEmoji = AI_EMOJI[mem.source_ai] || "🤖";

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "rgba(0,0,0,0.45)", backdropFilter: "blur(4px)",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: "var(--space-md)", animation: "fadeIn 0.2s ease",
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: "100%", maxWidth: 560, maxHeight: "85vh", overflow: "auto",
        background: "var(--bg-card)", borderRadius: "var(--radius-lg)",
        border: "1px solid var(--glass-border)", boxShadow: "var(--glass-shadow)",
        padding: "var(--space-lg)", position: "relative",
      }}>
        {/* Close button */}
        <button onClick={onClose} style={{
          position: "sticky", top: 0, float: "right",
          background: "var(--bg-hover)", border: "none", borderRadius: "50%",
          width: 32, height: 32, display: "flex", alignItems: "center", justifyContent: "center",
          cursor: "pointer", color: "var(--text-secondary)", zIndex: 1,
        }}>
          <X size={16} />
        </button>

        {loading ? (
          <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>
            加载中...
          </div>
        ) : !data ? (
          <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>
            记忆不存在
          </div>
        ) : (
          <>
            {/* Header: metadata badges */}
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: "var(--space-md)", paddingRight: 36 }}>
              <span style={{
                fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)",
                background: "var(--primary-light)", color: "var(--primary-dark)", fontWeight: 600,
              }}>{roomLabel}</span>
              {aiLabel && (
                <span style={{
                  fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)",
                  background: "var(--bg-hover)", color: "var(--text-secondary)",
                }}>{aiEmoji} {aiLabel}</span>
              )}
              {mem.importance >= 0.7 && (
                <span style={{
                  fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)",
                  background: "hsl(45, 90%, 88%)", color: "hsl(40, 80%, 35%)",
                }}>⭐ 重要 {Math.round(mem.importance * 100)}%</span>
              )}
              {mem.status && mem.status !== "active" && (
                <span style={{
                  fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)",
                  background: "hsl(0, 60%, 90%)", color: "hsl(0, 60%, 40%)",
                }}>{mem.status}</span>
              )}
            </div>

            {/* Main content */}
            <div style={{
              fontSize: 15, lineHeight: 1.7, color: "var(--text-primary)",
              marginBottom: "var(--space-lg)",
            }}>
              <Markdown components={{
                p: ({ children }) => <p style={{ margin: "0 0 8px" }}>{children}</p>,
                strong: ({ children }) => <strong style={{ color: "var(--primary-dark)" }}>{children}</strong>,
              }}>{mem.content || ""}</Markdown>
            </div>

            {/* Time info */}
            <div style={{
              display: "flex", gap: "var(--space-lg)", flexWrap: "wrap",
              fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-lg)",
            }}>
              {createdAt && (
                <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                  <Clock size={12} /> 创建: {createdAt}
                </span>
              )}
              {eventDate && (
                <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                  <Clock size={12} /> 事件: {eventDate}
                </span>
              )}
            </div>

            {/* Decay bar */}
            {decay.current_score !== undefined && (
              <div style={{ marginBottom: "var(--space-lg)" }}>
                <DecayBar score={decay.current_score} threshold={decay.threshold || 0.15} />
                <div style={{ display: "flex", gap: "var(--space-lg)", marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
                  <span><Activity size={10} style={{ verticalAlign: "middle" }} /> 存活 {decay.days_alive} 天</span>
                  <span><Zap size={10} style={{ verticalAlign: "middle" }} /> 被想起 {mem.activation_count || 0} 次</span>
                  {mem.last_activated && (
                    <span>上次: {new Date(mem.last_activated).toLocaleDateString("zh-CN")}</span>
                  )}
                </div>
              </div>
            )}

            {/* Tags */}
            {tags.length > 0 && (
              <Section icon={Tag} title={`标签 (${tags.length})`}>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {tags.map((t) => (
                    <span key={t} onClick={() => { onClose(); onNavigate?.(`/memories?search=${encodeURIComponent(t)}`); }}
                      style={{
                        fontSize: 11, padding: "3px 10px", borderRadius: 12,
                        background: "var(--bg-hover)", color: "var(--text-secondary)",
                        cursor: "pointer", transition: "var(--transition-fast)",
                      }}
                      onMouseEnter={(e) => { e.target.style.background = "var(--primary-light)"; e.target.style.color = "var(--primary-dark)"; }}
                      onMouseLeave={(e) => { e.target.style.background = "var(--bg-hover)"; e.target.style.color = "var(--text-secondary)"; }}
                    >#{t}</span>
                  ))}
                </div>
              </Section>
            )}

            {/* Source context */}
            {mem.source_context && (
              <Section icon={FileText} title="原始对话" defaultOpen={false}>
                <div style={{
                  fontSize: 12, lineHeight: 1.6, color: "var(--text-secondary)",
                  background: "var(--bg-hover)", borderRadius: "var(--radius-sm)",
                  padding: "var(--space-md)", whiteSpace: "pre-wrap",
                  maxHeight: 200, overflow: "auto",
                  borderLeft: "3px solid var(--primary-light)",
                }}>
                  {mem.source_context}
                </div>
              </Section>
            )}

            {/* Emotion */}
            {(mem.valence !== undefined || mem.emotion_arousal !== undefined) && (
              <Section icon={Heart} title="情感坐标" defaultOpen={false}>
                <div style={{ display: "flex", gap: "var(--space-lg)", fontSize: 12, color: "var(--text-secondary)" }}>
                  {mem.valence !== undefined && (
                    <div>
                      <span style={{ color: "var(--text-muted)", fontSize: 11 }}>效价</span>
                      <div style={{ fontWeight: 600, fontSize: 16, color: "var(--text-primary)" }}>
                        {Number(mem.valence).toFixed(2)}
                      </div>
                      <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                        {mem.valence > 0.6 ? "积极" : mem.valence < 0.4 ? "消极" : "中性"}
                      </span>
                    </div>
                  )}
                  {mem.emotion_arousal !== undefined && (
                    <div>
                      <span style={{ color: "var(--text-muted)", fontSize: 11 }}>唤醒度</span>
                      <div style={{ fontWeight: 600, fontSize: 16, color: "var(--text-primary)" }}>
                        {Number(mem.emotion_arousal).toFixed(2)}
                      </div>
                      <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                        {mem.emotion_arousal > 0.6 ? "强烈" : mem.emotion_arousal < 0.3 ? "平静" : "温和"}
                      </span>
                    </div>
                  )}
                </div>
              </Section>
            )}

            {/* Related memories */}
            {related.length > 0 && (
              <Section icon={Link2} title={`关联记忆 (${related.length})`} defaultOpen={false}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {related.map((r) => (
                    <div key={r.id} onClick={() => onNavigate?.(`memory:${r.id}`)}
                      style={{
                        fontSize: 12, padding: "8px 12px", borderRadius: "var(--radius-sm)",
                        background: "var(--bg-hover)", cursor: "pointer",
                        transition: "var(--transition-fast)",
                      }}
                      onMouseEnter={(e) => e.currentTarget.style.background = "var(--primary-light)"}
                      onMouseLeave={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
                    >
                      <div style={{ color: "var(--text-primary)", marginBottom: 2 }}>{r.content}</div>
                      <div style={{ display: "flex", gap: 6 }}>
                        {r.shared_tags?.map((t) => (
                          <span key={t} style={{ fontSize: 10, color: "var(--primary)", opacity: 0.7 }}>#{t}</span>
                        ))}
                        <span style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: "auto" }}>
                          {ROOM_LABELS[r.room] || r.room}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {/* Supersede chain */}
            {chain.length > 0 && (
              <Section icon={TrendingDown} title={`版本链 (${chain.length})`} defaultOpen={false}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {chain.map((s) => (
                    <div key={s.id} style={{
                      fontSize: 12, padding: "8px 12px", borderRadius: "var(--radius-sm)",
                      background: "var(--bg-hover)", borderLeft: `3px solid ${s.direction === "supersedes_current" ? "var(--success, #4caf50)" : "var(--text-muted)"}`,
                    }}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                          {s.direction === "supersedes_current" ? "⬆ 被新版取代" : "⬇ 旧版本"}
                        </span>
                        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                          {s.status} · {s.created_at ? new Date(s.created_at).toLocaleDateString("zh-CN") : ""}
                        </span>
                      </div>
                      <div style={{ color: "var(--text-secondary)" }}>{s.content}</div>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {/* History */}
            {history.length > 0 && (
              <Section icon={Clock} title={`历史年轮 (${history.length})`} defaultOpen={false}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  {history.map((h, i) => (
                    <div key={i} style={{
                      fontSize: 11, padding: "6px 10px", borderRadius: "var(--radius-sm)",
                      background: "var(--bg-hover)", color: "var(--text-secondary)",
                    }}>
                      <span style={{ color: "var(--text-muted)" }}>v{h.v || i + 1}</span>
                      {" · "}
                      <span>{h.by || "system"}</span>
                      {h.date && <span style={{ marginLeft: 8, color: "var(--text-muted)" }}>{h.date}</span>}
                      {h.content && (
                        <div style={{ marginTop: 2, color: "var(--text-primary)", fontSize: 12 }}>
                          {h.content.slice(0, 100)}{h.content.length > 100 ? "..." : ""}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {/* Footer: ID */}
            <div style={{
              marginTop: "var(--space-md)", paddingTop: "var(--space-sm)",
              borderTop: "1px solid var(--glass-border)",
              fontSize: 10, color: "var(--text-muted)", fontFamily: "monospace",
            }}>
              ID: {mem.id}
              {mem.source_platform && <span> · 来源: {mem.source_platform}</span>}
            </div>
          </>
        )}
      </div>

      <style>{`
        @keyframes fadeIn {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        @media (max-width: 640px) {
          /* fullscreen on mobile */
          div[style*="max-width: 560px"] {
            max-width: 100% !important;
            max-height: 100vh !important;
            height: 100vh;
            border-radius: 0 !important;
          }
        }
      `}</style>
    </div>
  );
}
