import { useEffect, useState } from "react";
import { ClipboardList, Check, X, RefreshCw, ChevronDown, ChevronUp } from "lucide-react";

const CLAIM_LABELS = { fact: "事实", observation: "观察", hypothesis: "推测" };
const SPEECH_LABELS = { literal: "直述", playful: "玩梗", hypothetical: "假设", fictional: "虚构", uncertain: "不确定" };
const STATUS_TABS = [
  { key: "pending", label: "待审核" },
  { key: "auto_approved", label: "自动通过" },
  { key: "approved", label: "已批准" },
  { key: "rejected", label: "已拒绝" },
  { key: "promotion_failed", label: "晋升失败" },
];

export default function ProposalsPage() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("pending");
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState({});
  const [expanded, setExpanded] = useState({});
  const [retriageResult, setRetriageResult] = useState(null);

  const auth = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = (s = status, p = page) => {
    setLoading(true);
    fetch(`/api/proposals?status=${s}&page=${p}&per_page=30`, { headers: auth })
      .then((r) => r.json())
      .then((d) => {
        setItems(d.items || []);
        setTotal(d.total || 0);
      })
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(status, 1); setPage(1); }, [status]);

  const review = async (id, action, reason = "") => {
    setActing((a) => ({ ...a, [id]: action }));
    try {
      const resp = await fetch(`/api/proposals/${id}/review`, {
        method: "POST",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({ action, reviewed_by: "user", reject_reason: reason }),
      });
      const result = await resp.json();
      if (result.error) {
        alert(result.error);
      } else {
        setItems((prev) => prev.filter((p) => p.id !== id));
        setTotal((t) => Math.max(0, t - 1));
      }
    } catch {
      alert("操作失败");
    }
    setActing((a) => ({ ...a, [id]: null }));
  };

  const retriage = async () => {
    setRetriageResult(null);
    setLoading(true);
    try {
      const resp = await fetch("/api/proposals/retriage", { method: "POST", headers: auth });
      const result = await resp.json();
      setRetriageResult(result);
      load(status, page);
    } catch {
      alert("Retriage 失败");
      setLoading(false);
    }
  };

  const approveAll = async () => {
    if (!confirm(`批量通过当前 ${items.length} 条？`)) return;
    setLoading(true);
    let ok = 0;
    for (const item of items) {
      try {
        await fetch(`/api/proposals/${item.id}/review`, {
          method: "POST",
          headers: { ...auth, "Content-Type": "application/json" },
          body: JSON.stringify({ action: "approve", reviewed_by: "user" }),
        });
        ok++;
      } catch {}
    }
    alert(`已通过 ${ok} 条`);
    load(status, page);
  };

  const toggle = (id) => setExpanded((e) => ({ ...e, [id]: !e[id] }));

  const confBar = (conf) => {
    const pct = Math.round(conf * 100);
    const color = conf >= 0.7 ? "#22c55e" : conf >= 0.5 ? "#eab308" : "#ef4444";
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <div style={{ width: 48, height: 6, borderRadius: 3, background: "var(--bg-tertiary)" }}>
          <div style={{ width: `${pct}%`, height: "100%", borderRadius: 3, background: color }} />
        </div>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{pct}%</span>
      </div>
    );
  };

  return (
    <div style={{ maxWidth: 800, margin: "0 auto", padding: "20px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
        <ClipboardList size={22} />
        <h2 style={{ margin: 0, fontSize: 20 }}>记忆候选区</h2>
        <span style={{ fontSize: 13, color: "var(--text-muted)" }}>({total})</span>
      </div>

      {/* Status tabs */}
      <div style={{ display: "flex", gap: 6, marginBottom: 12, flexWrap: "wrap" }}>
        {STATUS_TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setStatus(t.key)}
            style={{
              padding: "4px 12px", borderRadius: 14, border: "1px solid var(--border)",
              background: status === t.key ? "var(--accent)" : "var(--bg-secondary)",
              color: status === t.key ? "#fff" : "var(--text-primary)",
              cursor: "pointer", fontSize: 13,
            }}
          >{t.label}</button>
        ))}
      </div>

      {/* Actions */}
      {status === "pending" && (
        <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
          <button onClick={retriage} disabled={loading} style={btnStyle}>
            <RefreshCw size={14} /> Retriage
          </button>
          {items.length > 0 && (
            <button onClick={approveAll} disabled={loading} style={{ ...btnStyle, background: "#22c55e", color: "#fff" }}>
              <Check size={14} /> 全部通过 ({items.length})
            </button>
          )}
          {retriageResult && (
            <span style={{ fontSize: 13, color: "var(--text-muted)", alignSelf: "center" }}>
              通过 {retriageResult.approved} / 失败 {retriageResult.failed} / 仍待审 {retriageResult.still_pending}
            </span>
          )}
        </div>
      )}

      {loading && <div style={{ padding: 20, textAlign: "center", color: "var(--text-muted)" }}>加载中...</div>}

      {/* List */}
      {!loading && items.length === 0 && (
        <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>暂无{STATUS_TABS.find(t => t.key === status)?.label || ""}记录</div>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {items.map((p) => (
          <div key={p.id} style={cardStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14, lineHeight: 1.5, wordBreak: "break-word" }}>
                  {p.content?.length > 120 && !expanded[p.id]
                    ? p.content.slice(0, 120) + "..."
                    : p.content}
                  {p.content?.length > 120 && (
                    <button onClick={() => toggle(p.id)} style={linkBtn}>
                      {expanded[p.id] ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                    </button>
                  )}
                </div>
                <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap", alignItems: "center" }}>
                  <Tag color="#6e9fff">{CLAIM_LABELS[p.claim_type] || p.claim_type}</Tag>
                  <Tag color="#a78bfa">{SPEECH_LABELS[p.speech_mode] || p.speech_mode}</Tag>
                  <Tag color="#888">{p.proposed_room || p.room || "?"}</Tag>
                  {p.proposer_ai_id && <Tag color="#f59e0b">{p.proposer_ai_id}</Tag>}
                  {confBar(p.confidence || 0)}
                </div>
                {p.triage_reason && (
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                    分流: {p.triage_reason} · {p.created_at?.slice(0, 16)}
                  </div>
                )}
                {p.reject_reason && (
                  <div style={{ fontSize: 12, color: "#ef4444", marginTop: 4 }}>拒绝原因: {p.reject_reason}</div>
                )}
              </div>

              {status === "pending" && (
                <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                  <button
                    onClick={() => review(p.id, "approve")}
                    disabled={!!acting[p.id]}
                    style={{ ...actionBtn, background: "#22c55e", color: "#fff" }}
                    title="通过"
                  ><Check size={16} /></button>
                  <button
                    onClick={() => {
                      const reason = prompt("拒绝原因（可选）：");
                      if (reason !== null) review(p.id, "reject", reason);
                    }}
                    disabled={!!acting[p.id]}
                    style={{ ...actionBtn, background: "#ef4444", color: "#fff" }}
                    title="拒绝"
                  ><X size={16} /></button>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Pagination */}
      {total > 30 && (
        <div style={{ display: "flex", justifyContent: "center", gap: 8, marginTop: 16 }}>
          <button disabled={page <= 1} onClick={() => { setPage(page - 1); load(status, page - 1); }} style={btnStyle}>上一页</button>
          <span style={{ fontSize: 13, alignSelf: "center", color: "var(--text-muted)" }}>第 {page} 页</span>
          <button disabled={items.length < 30} onClick={() => { setPage(page + 1); load(status, page + 1); }} style={btnStyle}>下一页</button>
        </div>
      )}
    </div>
  );
}

function Tag({ color, children }) {
  return (
    <span style={{
      display: "inline-block", padding: "1px 8px", borderRadius: 10,
      fontSize: 11, background: `${color}22`, color, border: `1px solid ${color}44`,
    }}>{children}</span>
  );
}

const cardStyle = {
  padding: "12px 14px", borderRadius: 8, border: "1px solid var(--border)",
  background: "var(--bg-secondary)",
};
const btnStyle = {
  display: "inline-flex", alignItems: "center", gap: 4,
  padding: "4px 12px", borderRadius: 6, border: "1px solid var(--border)",
  background: "var(--bg-secondary)", color: "var(--text-primary)",
  cursor: "pointer", fontSize: 13,
};
const actionBtn = {
  display: "flex", alignItems: "center", justifyContent: "center",
  width: 32, height: 32, borderRadius: 6, border: "none", cursor: "pointer",
};
const linkBtn = {
  background: "none", border: "none", color: "var(--text-muted)",
  cursor: "pointer", padding: 0, marginLeft: 4, verticalAlign: "middle",
};
