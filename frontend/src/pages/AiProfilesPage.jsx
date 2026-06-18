import { useEffect, useState } from "react";
import { Bot, Save, ChevronDown, ChevronUp, Globe, Key, Cpu, MessageSquare, Brain } from "lucide-react";

const ROOM_LABELS = {
  psychology: "心理", personality: "性格", health: "健康", career: "职业",
  relationships: "关系", relationship: "亲密", game_room: "游戏室",
  living_room: "客厅", preferences: "偏好", infra: "基建",
  infra_changelog: "更新日志", diary: "日记", work_tasks: "工作", social: "社交",
};

export default function AiProfilesPage() {
  const [profiles, setProfiles] = useState([]);
  const [activeTab, setActiveTab] = useState(null);
  const [editing, setEditing] = useState({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [memories, setMemories] = useState({});
  const [showMemories, setShowMemories] = useState(false);

  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = () => {
    fetch("/api/ai-profiles", { headers: auth })
      .then((r) => r.json())
      .then((d) => {
        setProfiles(d.profiles || []);
        if (!activeTab && d.profiles?.length) setActiveTab(d.profiles[0].ai_id);
      })
      .catch(() => {});
  };

  useEffect(load, []);

  const loadMemories = (aiId) => {
    fetch(`/api/ai-profiles/${aiId}/memories?limit=30`, { headers: auth })
      .then((r) => r.json())
      .then((d) => setMemories((prev) => ({ ...prev, [aiId]: d })))
      .catch(() => {});
  };

  useEffect(() => {
    if (activeTab && showMemories && !memories[activeTab]) {
      loadMemories(activeTab);
    }
  }, [activeTab, showMemories]);

  const profile = profiles.find((p) => p.ai_id === activeTab);
  const edits = editing[activeTab] || {};

  const getVal = (field) => edits[field] !== undefined ? edits[field] : (profile?.[field] || "");
  const setVal = (field, val) => setEditing((prev) => ({
    ...prev,
    [activeTab]: { ...(prev[activeTab] || {}), [field]: val },
  }));

  const save = async () => {
    if (!activeTab) return;
    setSaving(true);
    const body = { ...edits };
    // Don't send empty api_key (means no change)
    if (body.llm_api_key === "") delete body.llm_api_key;
    try {
      await fetch(`/api/ai-profiles/${activeTab}`, {
        method: "PUT",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setSaved(activeTab);
      setEditing((prev) => ({ ...prev, [activeTab]: {} }));
      load();
      setTimeout(() => setSaved(""), 2000);
    } catch {}
    setSaving(false);
  };

  const hasChanges = Object.keys(edits).length > 0;

  return (
    <div style={{ maxWidth: 640, margin: "0 auto" }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: "var(--space-md)" }}>
        <Bot size={20} style={{ verticalAlign: -3, marginRight: 6 }} />
        AI 档案
      </h2>

      {/* Tab bar */}
      <div style={{
        display: "flex", gap: "var(--space-xs)", marginBottom: "var(--space-md)",
        overflowX: "auto", paddingBottom: 2,
      }}>
        {profiles.map((p) => (
          <button key={p.ai_id} onClick={() => { setActiveTab(p.ai_id); setShowMemories(false); }}
            style={{
              display: "flex", alignItems: "center", gap: 6,
              padding: "8px 16px", borderRadius: "var(--radius-md)",
              border: activeTab === p.ai_id ? "2px solid var(--primary)" : "1px solid var(--border-subtle)",
              background: activeTab === p.ai_id ? "var(--primary-light)" : "var(--bg-card)",
              cursor: "pointer", fontSize: 14, fontWeight: activeTab === p.ai_id ? 600 : 400,
              color: "var(--text-primary)", transition: "var(--transition-fast)",
              whiteSpace: "nowrap",
            }}>
            <span style={{ fontSize: 20 }}>{p.emoji}</span>
            {p.name}
          </button>
        ))}
      </div>

      {profile && (
        <>
          {/* Identity */}
          <Section title="身份" icon={<Bot size={14} />}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "var(--space-sm)" }}>
              <Field label="名字" value={getVal("name")} onChange={(v) => setVal("name", v)} />
              <Field label="Emoji" value={getVal("emoji")} onChange={(v) => setVal("emoji", v)} />
              <Field label="颜色" value={getVal("color")} onChange={(v) => setVal("color", v)}
                type="color" style={{ height: 38 }} />
            </div>
            <Field label="招呼语" value={getVal("greeting")} onChange={(v) => setVal("greeting", v)}
              placeholder="进入聊天时的第一句话" />
          </Section>

          {/* Persona */}
          <Section title="人设" icon={<MessageSquare size={14} />}>
            <textarea value={getVal("persona")} onChange={(e) => setVal("persona", e.target.value)}
              placeholder="描述这个 AI 的性格、说话风格、行为特点..."
              style={{
                width: "100%", minHeight: 140, border: "none", outline: "none",
                background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                padding: "var(--space-sm)", fontSize: 13, color: "var(--text-primary)",
                resize: "vertical", boxSizing: "border-box", lineHeight: 1.7,
              }} />
            <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
              这段人设会在社交场景（群聊、朋友圈、论坛）和对话中注入给模型。写得越具体，角色越鲜明。
            </p>
          </Section>

          {/* LLM Config */}
          <Section title="模型配置（社交场景用）" icon={<Cpu size={14} />}>
            <Field label="API Base URL" value={getVal("llm_base_url")} onChange={(v) => setVal("llm_base_url", v)}
              placeholder="留空则用全局默认" mono />
            <Field label="API Key" value={getVal("llm_api_key")}
              onChange={(v) => setVal("llm_api_key", v)}
              type="password"
              placeholder={profile.llm_api_key_set ? "已设置（留空不修改）" : "留空则用全局默认"} mono />
            <Field label="模型名称" value={getVal("llm_model")} onChange={(v) => setVal("llm_model", v)}
              placeholder="留空则用全局默认" mono />
            <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap", marginTop: 4 }}>
              {["claude-haiku-4-5-20241022", "claude-sonnet-4-5-20250514", "deepseek-chat", "gemini-2.0-flash"].map((m) => (
                <button key={m} onClick={() => setVal("llm_model", m)}
                  className="btn btn-ghost" style={{ padding: "2px 8px", fontSize: 11, fontFamily: "monospace" }}>
                  {m}
                </button>
              ))}
            </div>
            <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
              留空的字段会自动 fallback 到全局 LLM 配置（设置页的小模型）。
              可以给不同 AI 用不同模型——比如小克用 Sonnet，Jasper 用 DeepSeek。
            </p>
          </Section>

          {/* Save button */}
          <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
            <button className="btn btn-primary" onClick={save}
              disabled={!hasChanges || saving}
              style={{ padding: "8px 20px", fontSize: 14 }}>
              <Save size={14} style={{ marginRight: 4 }} />
              {saved === activeTab ? "已保存" : saving ? "保存中..." : "保存"}
            </button>
            {hasChanges && (
              <button className="btn btn-ghost" onClick={() => setEditing((prev) => ({ ...prev, [activeTab]: {} }))}
                style={{ padding: "8px 16px", fontSize: 13 }}>
                撤销
              </button>
            )}
          </div>

          {/* Memory viewer */}
          <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
            <button onClick={() => {
              setShowMemories(!showMemories);
              if (!showMemories && !memories[activeTab]) loadMemories(activeTab);
            }} style={{
              display: "flex", alignItems: "center", gap: 6, width: "100%",
              background: "none", border: "none", cursor: "pointer",
              fontSize: 14, fontWeight: 600, color: "var(--text-primary)",
              padding: 0,
            }}>
              <Brain size={14} />
              {profile.emoji} {profile.name} 的记忆
              {showMemories ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>

            {showMemories && memories[activeTab] && (
              <div style={{ marginTop: "var(--space-md)" }}>
                {memories[activeTab].total === 0 ? (
                  <p style={{ fontSize: 13, color: "var(--text-muted)", textAlign: "center" }}>
                    还没有记忆
                  </p>
                ) : (
                  Object.entries(memories[activeTab].rooms || {}).map(([room, mems]) => (
                    <div key={room} style={{ marginBottom: "var(--space-md)" }}>
                      <div style={{
                        fontSize: 12, fontWeight: 600, color: "var(--primary)",
                        marginBottom: "var(--space-xs)",
                        padding: "2px 8px", background: "var(--primary-light)",
                        borderRadius: "var(--radius-sm)", display: "inline-block",
                      }}>
                        {ROOM_LABELS[room] || room} ({mems.length})
                      </div>
                      {mems.map((m) => (
                        <div key={m.id} style={{
                          padding: "6px 0", borderBottom: "1px solid var(--border-subtle)",
                          fontSize: 12, lineHeight: 1.6,
                        }}>
                          <span style={{ color: "var(--text-secondary)" }}>{m.content}</span>
                          {m.category && (
                            <span style={{
                              marginLeft: 6, fontSize: 10, color: "var(--text-muted)",
                              background: "var(--bg-hover)", padding: "1px 4px",
                              borderRadius: "var(--radius-sm)",
                            }}>{m.category}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Section({ title, icon, children }) {
  return (
    <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
      <h3 style={{
        fontSize: 13, fontWeight: 600, color: "var(--text-secondary)",
        marginBottom: "var(--space-sm)", display: "flex", alignItems: "center", gap: 6,
      }}>
        {icon} {title}
      </h3>
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
        {children}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, type = "text", placeholder, mono, style: extraStyle }) {
  return (
    <div>
      <label style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 2, display: "block" }}>{label}</label>
      <input value={value} onChange={(e) => onChange(e.target.value)} type={type}
        placeholder={placeholder}
        style={{
          width: "100%", padding: "8px 10px",
          background: "var(--bg-input)", border: "1px solid var(--glass-border)",
          borderRadius: "var(--radius-sm)", fontSize: 13, color: "var(--text-primary)",
          outline: "none", boxSizing: "border-box",
          ...(mono ? { fontFamily: "monospace", fontSize: 12 } : {}),
          ...extraStyle,
        }} />
    </div>
  );
}
