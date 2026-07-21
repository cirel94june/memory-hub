import { useEffect, useState } from "react";
import { Users, Plus, X, Save, Trash2, Search, UserCircle, Bot, User } from "lucide-react";

const TYPE_LABELS = { user: "用户", ai: "AI", other: "其他人物" };
const TYPE_COLORS = { user: "#e879a0", ai: "#6e9fff", other: "#a3a3a3" };
const SCOPE_LABELS = { household: "家", game_world: "游戏" };

export default function PersonsPage() {
  const [persons, setPersons] = useState([]);
  const [selected, setSelected] = useState(null);
  const [editing, setEditing] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [saving, setSaving] = useState(false);
  const [searchQ, setSearchQ] = useState("");
  const [resolveResult, setResolveResult] = useState(null);

  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = () => {
    fetch("/api/persons", { headers: auth })
      .then((r) => r.json())
      .then((d) => setPersons(Array.isArray(d) ? d : []))
      .catch(() => {});
  };

  useEffect(load, []);

  const save = async (data) => {
    setSaving(true);
    try {
      const method = showAdd ? "POST" : "PUT";
      const url = showAdd ? "/api/persons" : `/api/persons/${data.person_id}`;
      const resp = await fetch(url, {
        method,
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!resp.ok) {
        const e = await resp.json();
        alert(e.detail || "保存失败");
        setSaving(false);
        return;
      }
      setEditing(null);
      setShowAdd(false);
      load();
    } catch {
      alert("网络错误");
    }
    setSaving(false);
  };

  const deletePerson = async (pid) => {
    if (!confirm("确定删除这个人物？记忆不会被删除。")) return;
    await fetch(`/api/persons/${pid}`, { method: "DELETE", headers: auth });
    setSelected(null);
    setEditing(null);
    load();
  };

  const doResolve = async () => {
    if (!searchQ.trim()) return;
    try {
      const r = await fetch(`/api/persons/resolve/${encodeURIComponent(searchQ.trim())}`, { headers: auth });
      if (r.ok) {
        const p = await r.json();
        setResolveResult(p);
        setSelected(p.person_id);
      } else {
        setResolveResult(null);
        alert(`找不到"${searchQ}"对应的人物`);
      }
    } catch {
      alert("网络错误");
    }
  };

  const filtered = persons.filter((p) => {
    if (!searchQ.trim()) return true;
    const q = searchQ.toLowerCase();
    if (p.canonical_name.toLowerCase().includes(q)) return true;
    if (p.person_id.toLowerCase().includes(q)) return true;
    const aliases = p.aliases || [];
    return aliases.some((a) => {
      const name = typeof a === "string" ? a : a.name || "";
      return name.toLowerCase().includes(q);
    });
  });

  const current = persons.find((p) => p.person_id === selected);

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: "var(--space-md)" }}>
        <Users size={20} style={{ verticalAlign: -3, marginRight: 6 }} />
        人物名片
      </h2>

      {/* Search + Add */}
      <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
        <div style={{ flex: 1, position: "relative" }}>
          <Search size={14} style={{
            position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
            color: "var(--text-muted)",
          }} />
          <input
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doResolve()}
            placeholder="搜索名字或别名..."
            style={{
              width: "100%", padding: "8px 10px 8px 30px",
              background: "var(--bg-input)", border: "1px solid var(--glass-border)",
              borderRadius: "var(--radius-md)", fontSize: 13, color: "var(--text-primary)",
              outline: "none", boxSizing: "border-box",
            }}
          />
        </div>
        <button
          onClick={() => {
            setShowAdd(true);
            setEditing({
              person_id: "", entity_type: "other", canonical_name: "",
              aliases: [], linked_agent_id: "", note: "",
            });
            setSelected(null);
          }}
          style={{
            display: "flex", alignItems: "center", gap: 4,
            padding: "8px 14px", borderRadius: "var(--radius-md)",
            border: "1px dashed var(--border-subtle, var(--glass-border))",
            background: "transparent", cursor: "pointer",
            fontSize: 13, color: "var(--text-muted)", whiteSpace: "nowrap",
          }}
        >
          <Plus size={14} /> 添加人物
        </button>
      </div>

      {/* Cards grid */}
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
        gap: "var(--space-sm)",
        marginBottom: "var(--space-md)",
      }}>
        {filtered.map((p) => (
          <PersonCard
            key={p.person_id}
            person={p}
            isSelected={selected === p.person_id}
            onClick={() => {
              setSelected(p.person_id === selected ? null : p.person_id);
              setEditing(null);
              setShowAdd(false);
            }}
          />
        ))}
      </div>

      {/* Add form */}
      {showAdd && editing && (
        <EditPanel
          data={editing}
          onChange={setEditing}
          onSave={() => save(editing)}
          onCancel={() => { setShowAdd(false); setEditing(null); }}
          saving={saving}
          isNew
        />
      )}

      {/* Detail / Edit panel */}
      {current && !showAdd && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          {editing ? (
            <EditPanel
              data={editing}
              onChange={setEditing}
              onSave={() => save(editing)}
              onCancel={() => setEditing(null)}
              saving={saving}
            />
          ) : (
            <DetailPanel
              person={current}
              onEdit={() => setEditing({ ...current })}
              onDelete={() => deletePerson(current.person_id)}
            />
          )}
        </div>
      )}
    </div>
  );
}

function PersonCard({ person, isSelected, onClick }) {
  const { entity_type, canonical_name, aliases = [], person_id } = person;
  const color = TYPE_COLORS[entity_type] || TYPE_COLORS.other;
  const Icon = entity_type === "ai" ? Bot : entity_type === "user" ? User : UserCircle;

  return (
    <div
      onClick={onClick}
      className="glass"
      style={{
        padding: "var(--space-md)",
        cursor: "pointer",
        border: isSelected ? `2px solid ${color}` : "1px solid var(--glass-border)",
        borderRadius: "var(--radius-md)",
        transition: "var(--transition-fast)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <Icon size={18} style={{ color, flexShrink: 0 }} />
        <span style={{ fontWeight: 600, fontSize: 15 }}>{canonical_name}</span>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
        {TYPE_LABELS[entity_type] || entity_type}
        {person.linked_agent_id ? ` · ${person.linked_agent_id}` : ""}
      </div>
      {aliases.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {aliases.slice(0, 5).map((a, i) => {
            const name = typeof a === "string" ? a : a.name || "";
            const scope = typeof a === "object" ? a.scope : "household";
            return (
              <span key={i} style={{
                fontSize: 11, padding: "1px 6px",
                background: scope === "game_world" ? "rgba(147,51,234,0.1)" : "var(--primary-light)",
                color: scope === "game_world" ? "#9333ea" : "var(--primary)",
                borderRadius: "var(--radius-sm)",
              }}>
                {name}
                {scope !== "household" && (
                  <span style={{ fontSize: 9, marginLeft: 2, opacity: 0.7 }}>
                    {SCOPE_LABELS[scope] || scope}
                  </span>
                )}
              </span>
            );
          })}
          {aliases.length > 5 && (
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>+{aliases.length - 5}</span>
          )}
        </div>
      )}
    </div>
  );
}

function DetailPanel({ person, onEdit, onDelete }) {
  const { entity_type, canonical_name, aliases = [], note, linked_agent_id, person_id, created_at } = person;
  const color = TYPE_COLORS[entity_type] || TYPE_COLORS.other;

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: "var(--space-md)" }}>
        <div>
          <h3 style={{ fontSize: 18, fontWeight: 700, margin: "0 0 4px" }}>{canonical_name}</h3>
          <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
            <span style={{
              display: "inline-block", padding: "1px 8px", borderRadius: 20,
              background: color + "22", color, fontSize: 11, fontWeight: 600, marginRight: 8,
            }}>
              {TYPE_LABELS[entity_type]}
            </span>
            ID: {person_id}
            {linked_agent_id ? ` · Agent: ${linked_agent_id}` : ""}
          </div>
        </div>
        <div style={{ display: "flex", gap: "var(--space-xs)" }}>
          <button className="btn btn-ghost" onClick={onEdit} style={{ padding: "6px 12px", fontSize: 12 }}>
            编辑
          </button>
          <button
            onClick={onDelete}
            style={{
              background: "none", border: "1px solid #ef444444", borderRadius: "var(--radius-sm)",
              padding: "6px 12px", fontSize: 12, color: "#ef4444", cursor: "pointer",
              display: "flex", alignItems: "center", gap: 4,
            }}
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>

      {note && (
        <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: "var(--space-md)", lineHeight: 1.6 }}>
          {note}
        </p>
      )}

      {aliases.length > 0 && (
        <div style={{ marginBottom: "var(--space-md)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 6 }}>别名</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {aliases.map((a, i) => {
              const name = typeof a === "string" ? a : a.name || "";
              const scope = typeof a === "object" ? a.scope : "household";
              return (
                <span key={i} style={{
                  fontSize: 13, padding: "3px 10px",
                  background: scope === "game_world" ? "rgba(147,51,234,0.1)" : "var(--primary-light)",
                  color: scope === "game_world" ? "#9333ea" : "var(--primary)",
                  borderRadius: "var(--radius-md)", fontWeight: 500,
                }}>
                  {name}
                  {scope !== "household" && (
                    <span style={{ fontSize: 10, marginLeft: 4, opacity: 0.7 }}>
                      ({SCOPE_LABELS[scope] || scope})
                    </span>
                  )}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {created_at && (
        <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
          创建于 {new Date(created_at).toLocaleDateString("zh-CN")}
        </div>
      )}
    </>
  );
}

function EditPanel({ data, onChange, onSave, onCancel, saving, isNew }) {
  const [newAlias, setNewAlias] = useState("");
  const [newScope, setNewScope] = useState("household");

  const set = (field, val) => onChange({ ...data, [field]: val });

  const addAlias = () => {
    if (!newAlias.trim()) return;
    const aliases = [...(data.aliases || []), { name: newAlias.trim(), scope: newScope }];
    set("aliases", aliases);
    setNewAlias("");
  };

  const removeAlias = (idx) => {
    const aliases = [...(data.aliases || [])];
    aliases.splice(idx, 1);
    set("aliases", aliases);
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-md)" }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, margin: 0 }}>
          {isNew ? "添加人物" : "编辑人物"}
        </h3>
        <button onClick={onCancel} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)" }}>
          <X size={16} />
        </button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)", marginBottom: "var(--space-sm)" }}>
        <Field label="ID（英文）" value={data.person_id}
          onChange={(v) => set("person_id", v.toLowerCase().replace(/[^a-z0-9_]/g, ""))}
          disabled={!isNew} />
        <Field label="名字" value={data.canonical_name} onChange={(v) => set("canonical_name", v)} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)", marginBottom: "var(--space-sm)" }}>
        <div>
          <label style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 2, display: "block" }}>类型</label>
          <select value={data.entity_type} onChange={(e) => set("entity_type", e.target.value)}
            style={{
              width: "100%", padding: "8px 10px",
              background: "var(--bg-input)", border: "1px solid var(--glass-border)",
              borderRadius: "var(--radius-sm)", fontSize: 13, color: "var(--text-primary)",
              outline: "none",
            }}>
            <option value="user">用户</option>
            <option value="ai">AI</option>
            <option value="other">其他人物</option>
          </select>
        </div>
        <Field label="关联 Agent ID" value={data.linked_agent_id || ""}
          onChange={(v) => set("linked_agent_id", v)}
          placeholder="如 claude, lucien" />
      </div>

      <Field label="备注" value={data.note || ""} onChange={(v) => set("note", v)}
        placeholder="简短描述" />

      {/* Aliases editor */}
      <div style={{ marginTop: "var(--space-md)" }}>
        <label style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 6, display: "block" }}>
          别名
        </label>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }}>
          {(data.aliases || []).map((a, i) => {
            const name = typeof a === "string" ? a : a.name || "";
            const scope = typeof a === "object" ? a.scope : "household";
            return (
              <span key={i} style={{
                fontSize: 12, padding: "2px 8px",
                background: scope === "game_world" ? "rgba(147,51,234,0.1)" : "var(--primary-light)",
                color: scope === "game_world" ? "#9333ea" : "var(--primary)",
                borderRadius: "var(--radius-md)",
                display: "flex", alignItems: "center", gap: 4,
              }}>
                {name}
                {scope !== "household" && (
                  <span style={{ fontSize: 9, opacity: 0.7 }}>({SCOPE_LABELS[scope] || scope})</span>
                )}
                <button onClick={() => removeAlias(i)} style={{
                  background: "none", border: "none", cursor: "pointer",
                  padding: 0, color: "inherit", opacity: 0.6, fontSize: 14, lineHeight: 1,
                }}>×</button>
              </span>
            );
          })}
        </div>
        <div style={{ display: "flex", gap: "var(--space-xs)" }}>
          <input value={newAlias} onChange={(e) => setNewAlias(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addAlias()}
            placeholder="输入别名..."
            style={{
              flex: 1, padding: "6px 10px",
              background: "var(--bg-input)", border: "1px solid var(--glass-border)",
              borderRadius: "var(--radius-sm)", fontSize: 12, color: "var(--text-primary)",
              outline: "none",
            }} />
          <select value={newScope} onChange={(e) => setNewScope(e.target.value)}
            style={{
              padding: "6px 8px",
              background: "var(--bg-input)", border: "1px solid var(--glass-border)",
              borderRadius: "var(--radius-sm)", fontSize: 11, color: "var(--text-primary)",
              outline: "none",
            }}>
            <option value="household">家</option>
            <option value="game_world">游戏</option>
          </select>
          <button onClick={addAlias} className="btn btn-ghost"
            style={{ padding: "6px 10px", fontSize: 12 }}>
            <Plus size={12} />
          </button>
        </div>
      </div>

      {/* Save */}
      <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-md)" }}>
        <button className="btn btn-primary" onClick={onSave}
          disabled={!data.person_id || !data.canonical_name || saving}
          style={{ padding: "8px 20px", fontSize: 13 }}>
          <Save size={13} style={{ marginRight: 4 }} />
          {saving ? "保存中..." : "保存"}
        </button>
        <button className="btn btn-ghost" onClick={onCancel}
          style={{ padding: "8px 16px", fontSize: 13 }}>
          取消
        </button>
      </div>
    </div>
  );
}

function Field({ label, value, onChange, type = "text", placeholder, disabled }) {
  return (
    <div>
      <label style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 2, display: "block" }}>{label}</label>
      <input value={value} onChange={(e) => onChange(e.target.value)} type={type}
        placeholder={placeholder} disabled={disabled}
        style={{
          width: "100%", padding: "8px 10px",
          background: disabled ? "var(--bg-hover)" : "var(--bg-input)",
          border: "1px solid var(--glass-border)",
          borderRadius: "var(--radius-sm)", fontSize: 13, color: "var(--text-primary)",
          outline: "none", boxSizing: "border-box",
          opacity: disabled ? 0.6 : 1,
        }} />
    </div>
  );
}
