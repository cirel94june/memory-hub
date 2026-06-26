import { useEffect, useState } from "react";
import { Bot, Save, ChevronDown, ChevronUp, Globe, Key, Cpu, MessageSquare, Brain, Plus, Trash2, X, Activity, Paintbrush } from "lucide-react";

const ROOM_LABELS = {
  psychology: "心理", personality: "性格", health: "健康", career: "职业",
  relationships: "关系", relationship: "亲密", game_room: "游戏室",
  living_room: "客厅", preferences: "偏好", infra: "基建",
  infra_changelog: "更新日志", diary: "日记", work_tasks: "工作", social: "社交",
};

const CORE_IDS = new Set(["cloudy", "lucien", "jasper", "claude", "gemini", "gpt"]);

export default function AiProfilesPage() {
  const [profiles, setProfiles] = useState([]);
  const [activeTab, setActiveTab] = useState(null);
  const [editing, setEditing] = useState({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState("");
  const [memories, setMemories] = useState({});
  const [showMemories, setShowMemories] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [newAi, setNewAi] = useState({ ai_id: "", name: "", emoji: "🤖", color: "#6366f1", platform: "telegram" });
  const [addError, setAddError] = useState("");
  const [debugInfo, setDebugInfo] = useState(null);
  const [debugLoading, setDebugLoading] = useState(false);
  const [imgCfg, setImgCfg] = useState({ base_url: "", model: "", api_key: "", has_key: false });
  const [imgEditing, setImgEditing] = useState({});
  const [imgSaving, setImgSaving] = useState(false);
  const [imgSaved, setImgSaved] = useState(false);

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

  useEffect(() => {
    fetch("/api/image-config", { headers: auth })
      .then((r) => r.json())
      .then((d) => setImgCfg(d))
      .catch(() => {});
  }, []);

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
        <button onClick={() => { setShowAdd(true); setAddError(""); }}
          style={{
            display: "flex", alignItems: "center", gap: 4,
            padding: "8px 14px", borderRadius: "var(--radius-md)",
            border: "1px dashed var(--border-subtle)",
            background: "transparent", cursor: "pointer",
            fontSize: 13, color: "var(--text-muted)",
          }}>
          <Plus size={14} /> 添加角色
        </button>
      </div>

      {/* 添加角色对话框 */}
      {showAdd && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <h3 style={{ fontSize: 14, fontWeight: 600, margin: 0 }}>添加新 AI 角色</h3>
            <button onClick={() => setShowAdd(false)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)" }}>
              <X size={16} />
            </button>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)" }}>
            <Field label="ID（英文，不可改）" value={newAi.ai_id}
              onChange={(v) => setNewAi({ ...newAi, ai_id: v.toLowerCase().replace(/[^a-z0-9]/g, "") })}
              placeholder="如 miyuki" />
            <Field label="名字" value={newAi.name}
              onChange={(v) => setNewAi({ ...newAi, name: v })}
              placeholder="如 美雪" />
            <Field label="Emoji" value={newAi.emoji}
              onChange={(v) => setNewAi({ ...newAi, emoji: v })} />
            <Field label="颜色" value={newAi.color} type="color"
              onChange={(v) => setNewAi({ ...newAi, color: v })} style={{ height: 38 }} />
          </div>
          {addError && <p style={{ fontSize: 12, color: "#ef4444", marginTop: 8 }}>{addError}</p>}
          <button className="btn btn-primary" style={{ marginTop: 12, padding: "8px 20px", fontSize: 13 }}
            onClick={async () => {
              if (!newAi.ai_id || !newAi.name) { setAddError("ID 和名字不能为空"); return; }
              try {
                const resp = await fetch("/api/ai-profiles", {
                  method: "POST",
                  headers: { ...auth, "Content-Type": "application/json" },
                  body: JSON.stringify(newAi),
                });
                if (!resp.ok) { const e = await resp.json(); setAddError(e.detail || "创建失败"); return; }
                setShowAdd(false);
                setNewAi({ ai_id: "", name: "", emoji: "🤖", color: "#6366f1", platform: "telegram" });
                load();
                const d = await resp.json();
                setActiveTab(d.ai_id);
              } catch { setAddError("网络错误"); }
            }}>
            创建
          </button>
          <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 8 }}>
            创建后可以在身份栏编辑人设、模型配置等。情绪面板会自动显示新角色。
          </p>
        </div>
      )}

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
          <Section title="模型配置（聊天+社交全场景）" icon={<Cpu size={14} />}>
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
              ⚠️ 此配置影响所有场景：前端聊天、朋友圈、论坛、群聊。
              三个字段必须都填才能生效——留空会 fallback 到全局小模型（daemon 用的 DeepSeek），可能导致 OOC。
            </p>
          </Section>

          {/* LLM Debug */}
          <Section title="模型诊断" icon={<Activity size={14} />}>
            <button className="btn btn-ghost" onClick={async () => {
              setDebugLoading(true);
              try {
                const r = await fetch("/api/ai-profiles/debug-llm", { headers: auth });
                setDebugInfo(await r.json());
              } catch (e) { setDebugInfo({ error: e.message }); }
              setDebugLoading(false);
            }} style={{ padding: "6px 14px", fontSize: 12 }}>
              {debugLoading ? "检测中..." : "🔍 检测所有 AI 的模型配置"}
            </button>
            {debugInfo && !debugInfo.error && (
              <div style={{ marginTop: "var(--space-sm)" }}>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>
                  全局默认：{debugInfo.global_fallback?.model} @ {debugInfo.global_fallback?.base_url?.replace(/https?:\/\//, "").slice(0, 30)}
                </div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
                  profile 存储 keys: {debugInfo.raw_profile_keys?.join(", ")}
                </div>
                {Object.entries(debugInfo.ai_configs || {}).map(([aid, cfg]) => {
                  const p = profiles.find((x) => x.ai_id === aid);
                  return (
                    <div key={aid} style={{
                      padding: "6px 8px", marginBottom: 4, borderRadius: "var(--radius-sm)",
                      background: cfg.is_global_fallback ? "rgba(239,68,68,0.08)" : "rgba(34,197,94,0.08)",
                      border: `1px solid ${cfg.is_global_fallback ? "rgba(239,68,68,0.2)" : "rgba(34,197,94,0.2)"}`,
                      fontSize: 12,
                    }}>
                      <span style={{ fontWeight: 600 }}>{p?.emoji || "🤖"} {p?.name || aid}</span>
                      <span style={{ color: "var(--text-muted)", marginLeft: 8 }}>
                        {cfg.model}
                      </span>
                      <span style={{ color: "var(--text-muted)", marginLeft: 8, fontSize: 10 }}>
                        @ {cfg.base_url?.replace(/https?:\/\//, "").slice(0, 35)}
                      </span>
                      {!cfg.has_key && <span style={{ color: "#ef4444", marginLeft: 8, fontSize: 10 }}>❌ 无API Key</span>}
                      {cfg.is_global_fallback && <span style={{ color: "#ef4444", marginLeft: 8, fontSize: 10 }}>⚠️ 用的全局默认</span>}
                      {cfg.alias_of && <span style={{ color: "var(--text-muted)", marginLeft: 8, fontSize: 10 }}>(→{cfg.alias_of})</span>}
                    </div>
                  );
                })}
              </div>
            )}
            {debugInfo?.error && (
              <div style={{ fontSize: 12, color: "#ef4444", marginTop: 4 }}>错误：{debugInfo.error}</div>
            )}
          </Section>

          {/* Image API Config (global) */}
          <Section title="画图 API 配置（全局共用）" icon={<Paintbrush size={14} />}>
            <Field label="API Base URL" value={imgEditing.base_url ?? imgCfg.base_url} onChange={(v) => setImgEditing((p) => ({ ...p, base_url: v }))}
              placeholder="如 https://api.example.com/v1" mono />
            <Field label="API Key" value={imgEditing.api_key ?? ""} onChange={(v) => setImgEditing((p) => ({ ...p, api_key: v }))}
              type="password" placeholder={imgCfg.has_key ? "已设置（留空不修改）" : "填写中转站的 API Key"} mono />
            <Field label="模型名称" value={imgEditing.model ?? imgCfg.model} onChange={(v) => setImgEditing((p) => ({ ...p, model: v }))}
              placeholder="如 gpt-5.5" mono />
            <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap", marginTop: 4 }}>
              {["gpt-5.5", "gpt-4o-image", "dall-e-3"].map((m) => (
                <button key={m} onClick={() => setImgEditing((p) => ({ ...p, model: m }))}
                  className="btn btn-ghost" style={{ padding: "2px 8px", fontSize: 11, fontFamily: "monospace" }}>
                  {m}
                </button>
              ))}
            </div>
            <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
              所有 AI 共用这个画图 API。在聊天和朋友圈里都可以让 AI 画图。
            </p>
            <button className="btn btn-primary" onClick={async () => {
              if (!Object.keys(imgEditing).length) return;
              setImgSaving(true);
              const body = { ...imgEditing };
              if (body.api_key === "") delete body.api_key;
              try {
                await fetch("/api/image-config", {
                  method: "PUT", headers: { ...auth, "Content-Type": "application/json" },
                  body: JSON.stringify(body),
                });
                setImgSaved(true);
                setImgEditing({});
                const r = await fetch("/api/image-config", { headers: auth });
                setImgCfg(await r.json());
                setTimeout(() => setImgSaved(false), 2000);
              } catch {}
              setImgSaving(false);
            }} disabled={!Object.keys(imgEditing).length || imgSaving}
              style={{ padding: "6px 16px", fontSize: 12, marginTop: 8 }}>
              <Save size={12} style={{ marginRight: 4 }} />
              {imgSaved ? "已保存" : imgSaving ? "保存中..." : "保存画图配置"}
            </button>
          </Section>

          {/* Save / Delete buttons */}
          <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)", alignItems: "center" }}>
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
            <div style={{ flex: 1 }} />
            {activeTab && !CORE_IDS.has(activeTab) && (
              <button style={{
                background: "none", border: "1px solid #ef444444", borderRadius: "var(--radius-sm)",
                padding: "6px 12px", fontSize: 12, color: "#ef4444", cursor: "pointer",
                display: "flex", alignItems: "center", gap: 4,
              }} onClick={async () => {
                if (!confirm(`确定删除 ${profile.name}？记忆不会被删除。`)) return;
                await fetch(`/api/ai-profiles/${activeTab}`, { method: "DELETE", headers: auth });
                setActiveTab(null);
                load();
              }}>
                <Trash2 size={12} /> 删除角色
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
