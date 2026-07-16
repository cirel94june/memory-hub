import { useEffect, useState } from "react";
import {
  Activity,
  Anchor,
  Clock,
  Edit3,
  FileText,
  Globe2,
  Heart,
  Link2,
  Lock,
  Save,
  Tag,
  TrendingDown,
  X,
  Zap,
} from "lucide-react";
import Markdown from "react-markdown";
import { useAI } from "../contexts/AIContext";

const ROOM_LABELS = {
  psychology: "心理",
  personality: "性格",
  health: "健康",
  career: "职业",
  relationships: "关系",
  relationship: "亲密",
  game_room: "游戏室",
  living_room: "客厅",
  preferences: "偏好",
  infra: "基建",
  infra_changelog: "更新日志",
  diary: "日记",
  work_tasks: "工作",
  social: "社交",
};

const ROOM_OPTIONS = [
  "living_room",
  "psychology",
  "personality",
  "health",
  "career",
  "relationships",
  "relationship",
  "preferences",
  "diary",
  "work_tasks",
  "infra",
  "infra_changelog",
  "social",
  "game_room",
];

function parseTags(tags) {
  if (Array.isArray(tags)) return tags;
  if (!tags) return [];
  try {
    const parsed = JSON.parse(tags);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function parseTagInput(value) {
  return value
    .split(/[,，\n]/)
    .map((tag) => tag.trim().replace(/^#/, ""))
    .filter(Boolean);
}

function DecayBar({ score, threshold }) {
  const pct = Math.round(Number(score || 0) * 100);
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

function Field({ label, children }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5, fontSize: 12, color: "var(--text-secondary)" }}>
      <span style={{ fontWeight: 600 }}>{label}</span>
      {children}
    </label>
  );
}

const inputStyle = {
  width: "100%",
  padding: "8px 10px",
  border: "1px solid var(--glass-border)",
  borderRadius: "var(--radius-sm)",
  background: "var(--bg-input)",
  color: "var(--text-primary)",
  font: "inherit",
  fontSize: 13,
};

export default function MemoryDetailModal({ memoryId, onClose, onNavigate }) {
  const { getAI, profiles } = useAI();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [commentText, setCommentText] = useState("");
  const [savingComment, setSavingComment] = useState(false);
  const [editForm, setEditForm] = useState({
    content: "",
    importance: 0.5,
    room: "living_room",
    category: "",
    tags: "",
    layer: "shared",
    owner_ai: "",
    source_ai: "",
  });
  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const loadDetail = async () => {
    if (!memoryId) return;
    setLoading(true);
    try {
      const res = await fetch(`/api/memory/${memoryId}/detail`, { headers: auth });
      const nextData = await res.json();
      setData(nextData);
      const mem = nextData?.memory || {};
      setEditForm({
        content: mem.content || "",
        importance: Number(mem.importance ?? 0.5),
        room: mem.room || "living_room",
        category: mem.category || "",
        tags: parseTags(mem.tags).join("，"),
        layer: mem.layer || "shared",
        owner_ai: mem.owner_ai || "",
        source_ai: mem.source_ai || "",
      });
    } catch {
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadDetail();
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
  const tags = parseTags(mem.tags);
  const history = Array.isArray(mem.history) ? mem.history : [];
  const comments = Array.isArray(mem.comments) ? mem.comments : [];

  const createdAt = mem.created_at ? new Date(mem.created_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" }) : "";
  const eventDate = mem.event_date || "";
  const roomLabel = ROOM_LABELS[mem.room] || mem.room || "未分类";
  const aiInfo = mem.source_ai ? getAI(mem.source_ai) : null;
  const aiLabel = aiInfo?.name || mem.source_ai || "";
  const aiEmoji = aiInfo?.emoji || "🤖";
  const ownerInfo = mem.owner_ai ? getAI(mem.owner_ai) : null;
  const ownerLabel = ownerInfo?.name || mem.owner_ai || "";
  const ownerEmoji = ownerInfo?.emoji || "🤖";
  const layerLabel = mem.layer === "private" ? "私有记忆" : "公用记忆";

  const addWheelComment = async () => {
    const content = commentText.trim();
    if (!content) return;
    setSavingComment(true);
    try {
      const res = await fetch(`/api/memory/${mem.id}/comment`, {
        method: "POST",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({ content, author: "user", kind: "comment" }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCommentText("");
      await loadDetail();
    } catch (err) {
      alert(`追加年轮失败：${err.message}`);
    } finally {
      setSavingComment(false);
    }
  };

  const saveEdit = async () => {
    const importance = Math.max(0, Math.min(1, Number(editForm.importance) || 0));
    setSaving(true);
    try {
      const res = await fetch(`/api/memory/${mem.id}`, {
        method: "PUT",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({
          content: editForm.content.trim(),
          importance,
          room: editForm.room,
          category: editForm.category.trim(),
          tags: parseTagInput(editForm.tags),
          layer: editForm.layer,
          owner_ai: editForm.layer === "private" ? editForm.owner_ai : "",
          source_ai: editForm.source_ai,
          changed_by: "user",
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setEditing(false);
      await loadDetail();
    } catch (err) {
      alert(`保存失败：${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "rgba(0,0,0,0.45)", backdropFilter: "blur(4px)",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: "var(--space-md)", animation: "fadeIn 0.2s ease",
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: "100%", maxWidth: 600, maxHeight: "85vh", overflow: "auto",
        background: "var(--bg-card)", borderRadius: "var(--radius-lg)",
        border: "1px solid var(--glass-border)", boxShadow: "var(--glass-shadow)",
        padding: "var(--space-lg)", position: "relative",
      }}>
        <button onClick={onClose} style={{
          position: "sticky", top: 0, float: "right",
          background: "var(--bg-hover)", border: "none", borderRadius: "50%",
          width: 32, height: 32, display: "flex", alignItems: "center", justifyContent: "center",
          cursor: "pointer", color: "var(--text-secondary)", zIndex: 1,
        }}>
          <X size={16} />
        </button>

        {loading ? (
          <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>加载中...</div>
        ) : !data ? (
          <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>记忆不存在</div>
        ) : (
          <>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: "var(--space-md)", paddingRight: 36 }}>
              <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)", background: "var(--primary-light)", color: "var(--primary-dark)", fontWeight: 600 }}>{roomLabel}</span>
              <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)", background: mem.layer === "private" ? "rgba(239,68,68,0.10)" : "rgba(34,197,94,0.10)", color: mem.layer === "private" ? "#b91c1c" : "#15803d", display: "inline-flex", alignItems: "center", gap: 4 }}>
                {mem.layer === "private" ? <Lock size={11} /> : <Globe2 size={11} />} {layerLabel}
              </span>
              {ownerLabel && <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)", background: "var(--bg-hover)", color: "var(--text-secondary)" }}>归属 {ownerEmoji} {ownerLabel}</span>}
              {aiLabel && <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)", background: "var(--bg-hover)", color: "var(--text-secondary)" }}>{aiEmoji} {aiLabel}</span>}
              <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)", background: "hsl(45, 90%, 88%)", color: "hsl(40, 80%, 35%)" }}>重要 {Math.round(Number(mem.importance || 0) * 100)}%</span>
              {mem.anchored && <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)", background: "hsl(210, 80%, 90%)", color: "hsl(210, 70%, 35%)", fontWeight: 600 }}>📌 锚点</span>}
              {mem.status && mem.status !== "active" && <span style={{ fontSize: 11, padding: "3px 10px", borderRadius: "var(--radius-sm)", background: "hsl(0, 60%, 90%)", color: "hsl(0, 60%, 40%)" }}>{mem.status}</span>}
            </div>

            {editing ? (
              <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-lg)" }}>
                <div style={{ display: "grid", gap: "var(--space-md)" }}>
                  <Field label="记忆内容">
                    <textarea
                      value={editForm.content}
                      onChange={(e) => setEditForm((f) => ({ ...f, content: e.target.value }))}
                      rows={7}
                      style={{ ...inputStyle, resize: "vertical", lineHeight: 1.6 }}
                    />
                  </Field>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)" }}>
                    <Field label="可见范围">
                      <select
                        value={editForm.layer}
                        onChange={(e) => setEditForm((f) => ({ ...f, layer: e.target.value, owner_ai: e.target.value === "private" ? f.owner_ai : "" }))}
                        style={inputStyle}
                      >
                        <option value="shared">公用：所有 AI 都能用</option>
                        <option value="private">私有：只给指定 AI</option>
                      </select>
                    </Field>
                    <Field label="归属 AI">
                      <select
                        value={editForm.owner_ai}
                        onChange={(e) => setEditForm((f) => ({ ...f, owner_ai: e.target.value, layer: e.target.value ? "private" : f.layer }))}
                        style={inputStyle}
                        disabled={editForm.layer !== "private"}
                      >
                        <option value="">不指定</option>
                        {profiles.map((p) => <option key={p.ai_id} value={p.ai_id}>{p.emoji} {p.name}</option>)}
                      </select>
                    </Field>
                  </div>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)" }}>
                    <Field label={`重要度 ${Math.round(Number(editForm.importance || 0) * 100)}%`}>
                      <input
                        type="range"
                        min="0"
                        max="1"
                        step="0.05"
                        value={editForm.importance}
                        onChange={(e) => setEditForm((f) => ({ ...f, importance: e.target.value }))}
                      />
                    </Field>
                    <Field label="房间">
                      <select value={editForm.room} onChange={(e) => setEditForm((f) => ({ ...f, room: e.target.value }))} style={inputStyle}>
                        {ROOM_OPTIONS.map((room) => <option key={room} value={room}>{ROOM_LABELS[room] || room}</option>)}
                      </select>
                    </Field>
                  </div>
                  <Field label="分类">
                    <input value={editForm.category} onChange={(e) => setEditForm((f) => ({ ...f, category: e.target.value }))} style={inputStyle} />
                  </Field>
                  <Field label="来源 / 关联角色">
                    <select value={editForm.source_ai} onChange={(e) => setEditForm((f) => ({ ...f, source_ai: e.target.value }))} style={inputStyle}>
                      <option value="">不指定</option>
                      {profiles.map((p) => <option key={p.ai_id} value={p.ai_id}>{p.emoji} {p.name}</option>)}
                    </select>
                  </Field>
                  <Field label="标签（用逗号分隔）">
                    <input value={editForm.tags} onChange={(e) => setEditForm((f) => ({ ...f, tags: e.target.value }))} style={inputStyle} />
                  </Field>
                  <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                    <button className="btn btn-ghost" onClick={() => setEditing(false)} disabled={saving}>取消</button>
                    <button className="btn btn-primary" onClick={saveEdit} disabled={saving || !editForm.content.trim()}>
                      <Save size={14} /> {saving ? "保存中..." : "保存"}
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <div style={{ fontSize: 15, lineHeight: 1.7, color: "var(--text-primary)", marginBottom: "var(--space-lg)" }}>
                <Markdown components={{
                  p: ({ children }) => <p style={{ margin: "0 0 8px" }}>{children}</p>,
                  strong: ({ children }) => <strong style={{ color: "var(--primary-dark)" }}>{children}</strong>,
                }}>{mem.content || ""}</Markdown>
              </div>
            )}

            <div style={{ display: "flex", gap: "var(--space-lg)", flexWrap: "wrap", fontSize: 12, color: "var(--text-muted)", marginBottom: "var(--space-lg)" }}>
              {createdAt && <span style={{ display: "flex", alignItems: "center", gap: 4 }}><Clock size={12} /> 创建: {createdAt}</span>}
              {eventDate && <span style={{ display: "flex", alignItems: "center", gap: 4 }}><Clock size={12} /> 事件: {eventDate}</span>}
            </div>

            {decay.current_score !== undefined && (
              <div style={{ marginBottom: "var(--space-lg)" }}>
                {mem.anchored ? (
                  <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "hsl(210, 70%, 45%)", fontWeight: 600 }}>
                    <Anchor size={14} /> 锚点记忆 · 不参与衰减
                  </div>
                ) : (
                  <DecayBar score={decay.current_score} threshold={decay.threshold || 0.15} />
                )}
                <div style={{ display: "flex", gap: "var(--space-lg)", marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
                  <span><Activity size={10} style={{ verticalAlign: "middle" }} /> 存活 {decay.days_alive} 天</span>
                  <span><Zap size={10} style={{ verticalAlign: "middle" }} /> 被想起 {mem.activation_count || 0} 次</span>
                  {mem.last_activated && <span>上次: {new Date(mem.last_activated).toLocaleDateString("zh-CN")}</span>}
                </div>
              </div>
            )}

            {tags.length > 0 && (
              <Section icon={Tag} title={`标签 (${tags.length})`}>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {tags.map((t) => (
                    <span key={t} onClick={() => { onClose(); onNavigate?.(`/memories?search=${encodeURIComponent(t)}`); }}
                      style={{ fontSize: 11, padding: "3px 10px", borderRadius: 12, background: "var(--bg-hover)", color: "var(--text-secondary)", cursor: "pointer" }}>
                      #{t}
                    </span>
                  ))}
                </div>
              </Section>
            )}

            {mem.source_context && (
              <Section icon={FileText} title="原始对话" defaultOpen={false}>
                <div style={{ fontSize: 12, lineHeight: 1.6, color: "var(--text-secondary)", background: "var(--bg-hover)", borderRadius: "var(--radius-sm)", padding: "var(--space-md)", whiteSpace: "pre-wrap", maxHeight: 200, overflow: "auto", borderLeft: "3px solid var(--primary-light)" }}>
                  {mem.source_context}
                </div>
              </Section>
            )}

            {(mem.valence !== undefined || mem.emotion_arousal !== undefined) && (
              <Section icon={Heart} title="情感坐标" defaultOpen={false}>
                <div style={{ display: "flex", gap: "var(--space-lg)", fontSize: 12, color: "var(--text-secondary)" }}>
                  {mem.valence !== undefined && <div><span style={{ color: "var(--text-muted)", fontSize: 11 }}>效价</span><div style={{ fontWeight: 600, fontSize: 16, color: "var(--text-primary)" }}>{Number(mem.valence).toFixed(2)}</div></div>}
                  {mem.emotion_arousal !== undefined && <div><span style={{ color: "var(--text-muted)", fontSize: 11 }}>唤醒度</span><div style={{ fontWeight: 600, fontSize: 16, color: "var(--text-primary)" }}>{Number(mem.emotion_arousal).toFixed(2)}</div></div>}
                </div>
              </Section>
            )}

            {related.length > 0 && (
              <Section icon={Link2} title={`关联记忆 (${related.length})`} defaultOpen={false}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {related.map((r) => (
                    <div key={r.id} onClick={() => onNavigate?.(`memory:${r.id}`)} style={{ fontSize: 12, padding: "8px 12px", borderRadius: "var(--radius-sm)", background: "var(--bg-hover)", cursor: "pointer" }}>
                      <div style={{ color: "var(--text-primary)", marginBottom: 2 }}>{r.content}</div>
                      <div style={{ display: "flex", gap: 6 }}>
                        {r.shared_tags?.map((t) => <span key={t} style={{ fontSize: 10, color: "var(--primary)", opacity: 0.7 }}>#{t}</span>)}
                        <span style={{ fontSize: 10, color: "var(--text-muted)", marginLeft: "auto" }}>{ROOM_LABELS[r.room] || r.room}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {chain.length > 0 && (
              <Section icon={TrendingDown} title={`版本链 (${chain.length})`} defaultOpen={false}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {chain.map((s) => (
                    <div key={s.id} style={{ fontSize: 12, padding: "8px 12px", borderRadius: "var(--radius-sm)", background: "var(--bg-hover)" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{s.direction === "supersedes_current" ? "被新版取代" : "旧版本"}</span>
                        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{s.status} · {s.created_at ? new Date(s.created_at).toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai" }) : ""}</span>
                      </div>
                      <div style={{ color: "var(--text-secondary)" }}>{s.content}</div>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {mem.status === "active" && (
              <Section icon={Clock} title="追加年轮" defaultOpen={false}>
                <div style={{ display: "grid", gap: 8 }}>
                  <textarea
                    value={commentText}
                    onChange={(e) => setCommentText(e.target.value)}
                    rows={3}
                    placeholder="补充新的理解、修正、回看感受……不会改写原记忆。"
                    style={{ ...inputStyle, resize: "vertical", lineHeight: 1.55 }}
                  />
                  <div style={{ display: "flex", justifyContent: "flex-end" }}>
                    <button className="btn btn-primary" onClick={addWheelComment} disabled={savingComment || !commentText.trim()}>
                      <Save size={13} /> {savingComment ? "追加中..." : "追加年轮"}
                    </button>
                  </div>
                </div>
              </Section>
            )}

            {comments.length > 0 && (
              <Section icon={Clock} title={`年轮评论 (${comments.length})`} defaultOpen={false}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {comments.slice().reverse().map((c, i) => (
                    <div key={c.id || i} style={{ fontSize: 12, padding: "8px 10px", borderRadius: "var(--radius-sm)", background: "var(--bg-hover)", color: "var(--text-secondary)" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginBottom: 4 }}>
                        <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{c.kind || "comment"} · {c.author || "system"}</span>
                        {c.date && <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{new Date(c.date).toLocaleString("zh-CN")}</span>}
                      </div>
                      <div style={{ color: "var(--text-primary)", lineHeight: 1.55, whiteSpace: "pre-wrap" }}>{c.content || ""}</div>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {history.length > 0 && (
              <Section icon={Clock} title={`版本历史 (${history.length})`} defaultOpen={false}>
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  {history.map((h, i) => (
                    <div key={i} style={{ fontSize: 11, padding: "6px 10px", borderRadius: "var(--radius-sm)", background: "var(--bg-hover)", color: "var(--text-secondary)" }}>
                      <span style={{ color: "var(--text-muted)" }}>v{h.v || i + 1}</span> · <span>{h.by || "system"}</span>
                      {h.date && <span style={{ marginLeft: 8, color: "var(--text-muted)" }}>{h.date}</span>}
                      {h.content && <div style={{ marginTop: 2, color: "var(--text-primary)", fontSize: 12 }}>{h.content.slice(0, 100)}{h.content.length > 100 ? "..." : ""}</div>}
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {mem.status === "active" && (
              <div style={{ marginTop: "var(--space-md)", display: "flex", gap: 8, flexWrap: "wrap" }}>
                <button className="btn btn-primary" onClick={() => setEditing(true)} disabled={editing}>
                  <Edit3 size={13} /> 编辑记忆
                </button>
                <button
                  onClick={async () => {
                    const isAnchored = mem.anchored;
                    const method = isAnchored ? "DELETE" : "POST";
                    const res = await fetch(`/api/memory/${mem.id}/anchor`, { method, headers: auth });
                    const result = await res.json();
                    if (result.error) { alert(result.error); return; }
                    setData((prev) => ({ ...prev, memory: { ...prev.memory, anchored: !isAnchored } }));
                  }}
                  style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 14px", borderRadius: "var(--radius-sm)", border: `1px solid ${mem.anchored ? "hsl(210, 70%, 50%)" : "var(--glass-border)"}`, background: mem.anchored ? "hsl(210, 80%, 92%)" : "var(--bg-hover)", color: mem.anchored ? "hsl(210, 70%, 35%)" : "var(--text-secondary)", cursor: "pointer", fontSize: 12, fontWeight: 500 }}
                >
                  <Anchor size={13} /> {mem.anchored ? "取消锚定" : "设为锚点"}
                </button>
              </div>
            )}

            <div style={{ marginTop: "var(--space-md)", paddingTop: "var(--space-sm)", borderTop: "1px solid var(--glass-border)", fontSize: 10, color: "var(--text-muted)", fontFamily: "monospace" }}>
              ID: {mem.id}{mem.source_platform && <span> · 来源: {mem.source_platform}</span>}
            </div>
          </>
        )}
      </div>

      <style>{`
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        @media (max-width: 640px) {
          div[style*="max-width: 600px"] {
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
