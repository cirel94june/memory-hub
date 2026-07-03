import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Archive,
  Gauge,
  RefreshCw,
  Shield,
  Timer,
  TrendingDown,
} from "lucide-react";
import MemoriesHubPage from "./MemoriesHubPage";
import MemoryDetailModal from "../components/MemoryDetailModal";

const LANE_LABELS = {
  protected: "保护中",
  long_term: "长期",
  short_term: "短期池",
  watch: "观察中",
};

const HEALTH_LABELS = {
  healthy: "稳定",
  decaying: "衰减中",
  critical: "临近归档",
};

const REASON_LABELS = {
  anchored: "锚点",
  living_room: "客厅",
  high_importance: "高重要度",
  often_recalled: "常被想起",
  emotionally_strong: "强情绪",
  low_importance: "低重要度",
  never_recalled: "未被想起",
  fast_decay_room: "快衰房间",
  auto_capture_unrecalled: "自动捕获未召回",
  old: "较久远",
};

function label(map, key) {
  return map[key] || key || "";
}

function StatusPill({ children, tone = "neutral" }) {
  const styles = {
    good: ["rgba(34,197,94,0.12)", "#15803d"],
    warn: ["rgba(245,158,11,0.14)", "#b45309"],
    bad: ["rgba(239,68,68,0.12)", "#b91c1c"],
    neutral: ["var(--bg-hover)", "var(--text-secondary)"],
  }[tone];

  return (
    <span style={{
      display: "inline-flex",
      alignItems: "center",
      height: 22,
      padding: "0 8px",
      borderRadius: 999,
      fontSize: 11,
      fontWeight: 600,
      background: styles[0],
      color: styles[1],
    }}>
      {children}
    </span>
  );
}

function Metric({ icon: Icon, label, value, tone = "neutral" }) {
  const color = tone === "bad" ? "#b91c1c" : tone === "warn" ? "#b45309" : tone === "good" ? "#15803d" : "var(--primary)";
  return (
    <div className="glass" style={{ padding: "14px 16px", minHeight: 78 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--text-muted)", fontSize: 12 }}>
        <Icon size={15} style={{ color }} /> {label}
      </div>
      <div style={{ marginTop: 8, fontSize: 24, fontWeight: 750, color: "var(--text-primary)" }}>
        {value}
      </div>
    </div>
  );
}

function StepSummary({ steps = [] }) {
  const recent = steps.slice(-8).reverse();
  if (!recent.length) {
    return <div style={{ color: "var(--text-muted)", fontSize: 13 }}>还没有后台整理记录</div>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {recent.map((step) => (
        <div key={`${step.key}-${step.started_at || step.duration_ms}`} style={{
          border: "1px solid var(--glass-border)",
          borderRadius: "var(--radius-sm)",
          padding: "9px 10px",
          background: "var(--bg-card)",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
            <span style={{ fontSize: 12, fontWeight: 650, color: "var(--text-primary)" }}>{step.label || step.key}</span>
            <StatusPill tone={step.status === "error" ? "bad" : step.status === "success" ? "good" : "neutral"}>
              {step.status || "unknown"} · {step.duration_ms || 0}ms
            </StatusPill>
          </div>
          {step.error && (
            <div style={{ marginTop: 5, fontSize: 11, color: "#b91c1c", wordBreak: "break-word" }}>
              {step.error.message || String(step.error)}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function MemoryRow({ item, onOpen }) {
  const laneTone = item.lane === "protected" || item.lane === "long_term" ? "good" : item.lane === "short_term" ? "warn" : "neutral";
  const healthTone = item.health === "critical" ? "bad" : item.health === "decaying" ? "warn" : "good";
  return (
    <div
      onClick={() => onOpen?.(item.id)}
      style={{
        border: "1px solid var(--glass-border)",
        borderRadius: "var(--radius-sm)",
        padding: "10px 12px",
        background: "var(--bg-card)",
        cursor: onOpen ? "pointer" : "default",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, color: "var(--text-primary)", lineHeight: 1.45 }}>{item.content}</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 7 }}>
            <StatusPill tone={laneTone}>{label(LANE_LABELS, item.lane)}</StatusPill>
            <StatusPill tone={healthTone}>{label(HEALTH_LABELS, item.health)}</StatusPill>
            <StatusPill>{item.room || "未分房间"}</StatusPill>
            {item.days_to_archive !== null && item.days_to_archive !== undefined && (
              <StatusPill tone={item.days_to_archive <= 3 ? "bad" : "neutral"}>{item.days_to_archive} 天到线</StatusPill>
            )}
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
            {(item.protections || []).map((p) => <StatusPill key={`p-${p}`} tone="good">{label(REASON_LABELS, p)}</StatusPill>)}
            {(item.pressures || []).map((p) => <StatusPill key={`d-${p}`} tone="warn">{label(REASON_LABELS, p)}</StatusPill>)}
          </div>
        </div>
        <div style={{ width: 72, flexShrink: 0, textAlign: "right" }}>
          <div style={{ fontSize: 18, fontWeight: 750, color: "var(--text-primary)" }}>{Math.round((item.decay_score || 0) * 100)}%</div>
          <div style={{ marginTop: 5, height: 5, borderRadius: 999, background: "var(--bg-hover)", overflow: "hidden" }}>
            <div style={{
              width: `${Math.max(2, Math.round((item.decay_score || 0) * 100))}%`,
              height: "100%",
              background: item.health === "critical" ? "#ef4444" : item.health === "decaying" ? "#f59e0b" : "#22c55e",
            }} />
          </div>
        </div>
      </div>
    </div>
  );
}

function PanelList({ icon: Icon, title, items, empty, onOpen }) {
  return (
    <div className="glass" style={{ padding: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 10, color: "var(--text-primary)", fontWeight: 700 }}>
        <Icon size={16} /> {title}
      </div>
      {items.length ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {items.map((item) => <MemoryRow key={item.id} item={item} onOpen={onOpen} />)}
        </div>
      ) : (
        <div style={{ color: "var(--text-muted)", fontSize: 13, padding: "10px 0" }}>{empty}</div>
      )}
    </div>
  );
}

export default function ObservatoryPage() {
  const [daemon, setDaemon] = useState(null);
  const [decay, setDecay] = useState(null);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState("overview");
  const [detailId, setDetailId] = useState(null);
  const secret = localStorage.getItem("mh-secret") || "";
  const auth = { Authorization: `Bearer ${secret}` };

  const load = async () => {
    setLoading(true);
    try {
      const [daemonRes, decayRes] = await Promise.all([
        fetch("/api/daemon/status", { headers: auth }),
        fetch("/api/memory/decay-scores", { headers: auth }),
      ]);
      if (daemonRes.ok) setDaemon(await daemonRes.json());
      if (decayRes.ok) setDecay(await decayRes.json());
    } catch {
      // keep last visible state
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const memories = decay?.memories || [];
  const protectedMemories = useMemo(() => memories.filter((m) => m.lane === "protected").slice(0, 10), [memories]);
  const critical = useMemo(() => memories.filter((m) => m.health === "critical").slice(0, 8), [memories]);
  const shortTerm = useMemo(() => memories.filter((m) => m.lane === "short_term").slice(0, 8), [memories]);
  const watch = useMemo(() => memories.filter((m) => m.lane === "watch").slice(0, 8), [memories]);
  const summary = decay?.summary || {};

  return (
    <div style={{ maxWidth: 980, margin: "0 auto", paddingBottom: 24 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginBottom: "var(--space-md)" }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 21, fontWeight: 760, color: "var(--text-primary)" }}>观测台</h2>
          <div style={{ marginTop: 4, fontSize: 12, color: "var(--text-muted)" }}>后台整理、衰减分层、时间线、热力图和记忆编辑</div>
        </div>
        <button className="btn btn-primary" onClick={load} disabled={loading} style={{ flexShrink: 0 }}>
          <RefreshCw size={14} /> {loading ? "刷新中" : "刷新"}
        </button>
      </div>

      <div className="glass" style={{
        display: "grid",
        gridTemplateColumns: "repeat(3, 1fr)",
        gap: 4,
        padding: 4,
        marginBottom: "var(--space-md)",
      }}>
        {[
          ["overview", "总览"],
          ["timeline", "时间线"],
          ["edit", "记忆编辑"],
        ].map(([key, title]) => (
          <button
            key={key}
            className={`btn ${tab === key ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setTab(key)}
            style={{ justifyContent: "center" }}
          >
            {title}
          </button>
        ))}
      </div>

      {tab === "timeline" && <MemoriesHubPage initialView="timeline" />}
      {tab === "edit" && <MemoriesHubPage />}
      {tab === "overview" && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
            <Metric icon={Gauge} label="活跃记忆" value={summary.total ?? "—"} />
            <Metric icon={Shield} label="保护中" value={summary.protected ?? 0} tone="good" />
            <Metric icon={Timer} label="短期池" value={summary.short_term ?? 0} tone="warn" />
            <Metric icon={AlertTriangle} label="临近归档" value={summary.critical ?? 0} tone="bad" />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "var(--space-md)", alignItems: "start" }}>
            <section className="glass" style={{ padding: "var(--space-md)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 10, color: "var(--text-primary)", fontWeight: 700 }}>
                <Activity size={16} /> 后台整理
              </div>
              <div style={{ display: "grid", gap: 6, marginBottom: 12, fontSize: 12, color: "var(--text-muted)" }}>
                <div>状态：<b style={{ color: "var(--text-primary)" }}>{daemon?.status || "unknown"}</b></div>
                <div>更新：{daemon?.updated_at || "还没有记录"}</div>
                {daemon?.finished_at && <div>完成：{daemon.finished_at}</div>}
              </div>
              <StepSummary steps={daemon?.steps || []} />
            </section>

            <section className="glass" style={{ padding: "var(--space-md)" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 10, color: "var(--text-primary)", fontWeight: 700 }}>
                <TrendingDown size={16} /> 衰减分层
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                {[
                  ["long_term", "长期", summary.long_term || 0, "good"],
                  ["watch", "观察中", summary.watch || 0, "neutral"],
                  ["short_term", "短期池", summary.short_term || 0, "warn"],
                  ["critical", "临近归档", summary.critical || 0, "bad"],
                ].map(([key, title, count, tone]) => (
                  <div key={key} style={{ padding: 10, borderRadius: "var(--radius-sm)", background: "var(--bg-card)", border: "1px solid var(--glass-border)" }}>
                    <StatusPill tone={tone}>{title}</StatusPill>
                    <div style={{ marginTop: 8, fontSize: 22, fontWeight: 750, color: "var(--text-primary)" }}>{count}</div>
                  </div>
                ))}
              </div>
            </section>
          </div>

          <section style={{ marginTop: "var(--space-md)" }}>
            <PanelList icon={Shield} title="保护中" items={protectedMemories} empty="暂时没有被保护的记忆" onOpen={setDetailId} />
          </section>
          <section style={{ marginTop: "var(--space-md)" }}>
            <PanelList icon={AlertTriangle} title="临近归档" items={critical} empty="暂时没有临近归档的记忆" onOpen={setDetailId} />
          </section>
          <section style={{ marginTop: "var(--space-md)", display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "var(--space-md)" }}>
            <PanelList icon={Archive} title="短期池" items={shortTerm} empty="短期池暂时为空" onOpen={setDetailId} />
            <PanelList icon={Gauge} title="观察中" items={watch} empty="没有需要观察的记忆" onOpen={setDetailId} />
          </section>
        </>
      )}

      {detailId && (
        <MemoryDetailModal
          memoryId={detailId}
          onClose={() => setDetailId(null)}
          onNavigate={(target) => {
            if (target.startsWith("memory:")) {
              setDetailId(target.slice(7));
            } else {
              setDetailId(null);
            }
          }}
        />
      )}
    </div>
  );
}
