import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Archive,
  Eye,
  Gauge,
  CheckCircle,
  Pencil,
  Sparkles,
  RefreshCw,
  Shield,
  Timer,
  TrendingDown,
} from "lucide-react";
import MemoriesHubPage from "./MemoriesHubPage";
import MemoryDetailModal from "../components/MemoryDetailModal";
import { useAI } from "../contexts/AIContext";

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


function DreamDiagnostics({ dream, onRun, running }) {
  const diagnostics = dream?.diagnostics || {};
  const entries = Object.entries(diagnostics);
  const reasonLabel = {
    already_dreamed: "今天已经做过梦",
    too_few_materials: "材料不足",
    llm_failed_or_too_short: "小模型失败或输出太短",
    ok: "已生成",
  };
  return (
    <section className="glass" style={{ padding: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, marginBottom: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, color: "var(--text-primary)", fontWeight: 700 }}>
          <Sparkles size={16} /> 梦境诊断
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          <button className="btn btn-ghost" onClick={() => onRun(false)} disabled={running}>
            <RefreshCw size={14} /> {running ? "触发中" : "单独补跑"}
          </button>
          <button className="btn btn-ghost" onClick={() => onRun(true)} disabled={running} title="忽略今天已做梦限制，重新生成当天梦境">
            <Sparkles size={14} /> 强制重做
          </button>
        </div>
      </div>
      <div style={{ display: "grid", gap: 6, marginBottom: 12, fontSize: 12, color: "var(--text-muted)" }}>
        <div>状态：<b style={{ color: "var(--text-primary)" }}>{dream?.status || "unknown"}</b></div>
        <div>更新：{dream?.updated_at || "还没有记录"}</div>
        {dream?.local_day && <div>本地日期：{dream.local_day}</div>}
        <div>只显示夜梦；旧逻辑生成的截断梦需要点“强制重做”重新生成。</div>
      </div>
      {entries.length ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {entries.map(([ai, info]) => (
            <div key={ai} style={{ border: "1px solid var(--glass-border)", borderRadius: "var(--radius-sm)", padding: "9px 10px", background: "var(--bg-card)" }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
                <span style={{ fontSize: 12, fontWeight: 650, color: "var(--text-primary)" }}>{ai}</span>
                <StatusPill tone={info.status === "dreamed" ? "good" : "warn"}>{reasonLabel[info.reason] || info.reason || info.status}</StatusPill>
              </div>
              <div style={{ marginTop: 5, fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5 }}>
                摘要 {info.digest_count ?? 0} 条 · 近期记忆 {info.memory_residue_count ?? 0} 条
                {info.required ? ` · ${info.required}` : ""}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ color: "var(--text-muted)", fontSize: 13 }}>还没有梦境诊断记录。可以点“单独补跑”测试一次。</div>
      )}
      {(dream?.recent_dreams || []).length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)", marginBottom: 8 }}>最近梦境</div>
          <div style={{ display: "grid", gap: 8 }}>
            {dream.recent_dreams.slice(0, 6).map((d) => (
              <div key={d.id || `${d.source_ai}-${d.created_at}`} style={{ border: "1px solid var(--glass-border)", borderRadius: "var(--radius-sm)", padding: "10px 11px", background: "var(--bg-card)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginBottom: 6, fontSize: 11, color: "var(--text-muted)" }}>
                  <span>{d.source_ai || d.owner_ai || "AI"}</span>
                  <span>{(d.created_at || "").slice(0, 16).replace("T", " ")}</span>
                </div>
                <div style={{ fontSize: 13, color: "var(--text-primary)", lineHeight: 1.65, whiteSpace: "pre-wrap" }}>
                  {d.content || "（梦境内容为空）"}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
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
          {item.lane_reason && (
            <div style={{ marginTop: 7, fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5 }}>
              {item.lane_reason}
            </div>
          )}
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


function WakePreview({ auth }) {
  const { profiles } = useAI();
  const socialProfiles = profiles.filter((p) => ["cloudy", "lucien", "jasper", "claude"].includes(p.ai_id) || p.platform === "telegram");
  const [aiId, setAiId] = useState(socialProfiles[0]?.ai_id || "cloudy");
  const [surface, setSurface] = useState("private");
  const [message, setMessage] = useState("我今天想让你回忆一下最近我们聊过什么");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [chatThreads, setChatThreads] = useState([]);
  const [selectedChatId, setSelectedChatId] = useState("");

  const surfaceMap = {
    private: { label: "私聊", chatType: "private", chatId: `preview:private:${aiId}` },
    social: { label: "朋友圈/论坛", chatType: "private_group", chatId: `social:${aiId}` },
    group: { label: "群聊", chatType: "private_group", chatId: selectedChatId || "preview:group" },
    mcp: { label: "MCP 唤醒", chatType: "private", chatId: `mcp:${aiId}` },
  };

  const preview = async () => {
    setLoading(true);
    try {
      const cfg = surfaceMap[surface] || surfaceMap.private;
      const res = await fetch("/api/gateway/context", {
        method: "POST",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({
          ai_id: aiId,
          user_message: message,
          chat_id: cfg.chatId,
          chat_type: cfg.chatType,
          compact: false,
          max_memories: 5,
          force_corridor: true,
        }),
      });
      setResult(res.ok ? await res.json() : { error: await res.text() });
    } catch (e) {
      setResult({ error: String(e) });
    }
    setLoading(false);
  };

  useEffect(() => {
    fetch("/api/chat-digests/threads", { headers: auth })
      .then((r) => r.ok ? r.json() : { threads: [] })
      .then((data) => {
        const threads = data.threads || [];
        setChatThreads(threads);
        if (!selectedChatId && threads.length) setSelectedChatId(threads[0].chat_id);
      })
      .catch(() => setChatThreads([]));
  }, []);

  useEffect(() => { preview(); }, [aiId, surface, selectedChatId]);

  return (
    <div className="glass" style={{ padding: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 10, color: "var(--text-primary)", fontWeight: 700 }}>
        <Eye size={16} /> 醒来预览
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10, marginBottom: 10 }}>
        <select className="input" value={aiId} onChange={(e) => { setAiId(e.target.value); setResult(null); }}>
          {socialProfiles.map((p) => <option key={p.ai_id} value={p.ai_id}>{p.name || p.ai_id}</option>)}
        </select>
        <select className="input" value={surface} onChange={(e) => { setSurface(e.target.value); setResult(null); }}>
          {Object.entries(surfaceMap).map(([key, cfg]) => <option key={key} value={key}>{cfg.label}</option>)}
        </select>
        {surface === "group" && (
          <select className="input" value={selectedChatId} onChange={(e) => { setSelectedChatId(e.target.value); setResult(null); }}>
            {chatThreads.length === 0 && <option value="">没有最近群聊摘要</option>}
            {chatThreads.map((t) => (
              <option key={`${t.chat_id}:${t.chat_type}`} value={t.chat_id}>{t.chat_id} · {t.digest_count || 0}条摘要</option>
            ))}
          </select>
        )}
        <button className="btn btn-primary" onClick={preview} disabled={loading}>
          <RefreshCw size={14} /> {loading ? "读取中" : "预览"}
        </button>
      </div>
      <textarea
        className="input"
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        rows={3}
        style={{ width: "100%", resize: "vertical", marginBottom: 10 }}
      />
      {result?.error && <div style={{ color: "#b91c1c", fontSize: 13, marginBottom: 8 }}>{result.error}</div>}
      {result && !result.error && (
        <>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
            <StatusPill>{result.requested_ai_id || aiId} → {result.ai_id || aiId}</StatusPill>
            <StatusPill>{result.chat_type || surfaceMap[surface]?.chatType}</StatusPill>
            <StatusPill>{result.chat_id || surfaceMap[surface]?.chatId}</StatusPill>
            {result.corridor_forced && <StatusPill tone="warn">强制刷新走廊</StatusPill>}
            {surface === "group" && <StatusPill>{result.group_activity_count || 0} 条群内AI摘要</StatusPill>}
            <StatusPill>{result.memory_count || 0} 条相关记忆</StatusPill>
            <StatusPill>{result.estimated_tokens || 0} token 估算</StatusPill>
            {result.detail_mode && <StatusPill tone="warn">细节模式</StatusPill>}
            {(result.recalled_ids || []).slice(0, 5).map((id) => <StatusPill key={id}>{id}</StatusPill>)}
          </div>
          <pre style={{
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxHeight: 520,
            overflow: "auto",
            margin: 0,
            padding: 12,
            borderRadius: "var(--radius-sm)",
            border: "1px solid var(--glass-border)",
            background: "var(--bg-card)",
            color: "var(--text-primary)",
            fontSize: 12,
            lineHeight: 1.6,
          }}>{result.inject_text || "这一轮没有注入记忆。"}</pre>
        </>
      )}
    </div>
  );
}

function LivingRoomRefresh({ auth }) {
  const [suggestions, setSuggestions] = useState([]);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [writing, setWriting] = useState(false);

  const refresh = async (dryRun = true) => {
    if (dryRun) setLoading(true);
    else setWriting(true);
    setResult(null);
    try {
      const res = await fetch("/api/memory/living-room/refresh", {
        method: "POST",
        headers: { ...auth, "Content-Type": "application/json" },
        body: JSON.stringify({ dry_run: dryRun, source_ai: "observatory" }),
      });
      const data = res.ok ? await res.json() : { error: await res.text() };
      if (dryRun) setSuggestions(data.actions || []);
      else {
        setResult(data);
        setSuggestions([]);
      }
    } catch (e) {
      setResult({ error: String(e) });
    }
    setLoading(false);
    setWriting(false);
  };

  return (
    <section className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, color: "var(--text-primary)", fontWeight: 700 }}>
          <Sparkles size={16} /> 客厅画像
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="btn btn-ghost" onClick={() => { window.location.href = "/app/memories?room=living_room"; }}>
            <Pencil size={14} /> 编辑用户画像
          </button>
          <button className="btn btn-ghost" onClick={() => { window.location.href = "/app/memories?room=relationships"; }}>
            <Pencil size={14} /> 编辑人物关系
          </button>
          <button className="btn btn-primary" onClick={() => refresh(true)} disabled={loading || writing}>
            <RefreshCw size={14} /> {loading ? "生成中" : "生成建议"}
          </button>
          <button className="btn btn-ghost" onClick={() => refresh(false)} disabled={loading || writing || !suggestions.length}>
            <CheckCircle size={14} /> {writing ? "写入中" : "写入建议"}
          </button>
        </div>
      </div>

      {result?.error && <div style={{ color: "#b91c1c", fontSize: 13 }}>{result.error}</div>}
      {result && !result.error && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
          <StatusPill tone="good">已写入 {result.count || 0} 条</StatusPill>
          <StatusPill>走廊已刷新</StatusPill>
        </div>
      )}
      {suggestions.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {suggestions.map((item, index) => (
            <div key={`${item.content}-${index}`} style={{
              border: "1px solid var(--glass-border)",
              borderRadius: "var(--radius-sm)",
              padding: "9px 10px",
              background: "var(--bg-card)",
            }}>
              <div style={{ fontSize: 13, lineHeight: 1.5, color: "var(--text-primary)" }}>{item.content}</div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 7 }}>
                <StatusPill>{item.category || "profile"}</StatusPill>
                <StatusPill>{Math.round((item.importance || 0) * 100)}% 重要度</StatusPill>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ color: "var(--text-muted)", fontSize: 13, lineHeight: 1.5 }}>
          用后台模型从近期稳定记忆里整理用户画像、重要人物和当前状态；写入前会先在这里显示建议。
        </div>
      )}
    </section>
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
  const [dream, setDream] = useState(null);
  const [decay, setDecay] = useState(null);
  const [loading, setLoading] = useState(false);
  const [dreamRunning, setDreamRunning] = useState(false);
  const [tab, setTab] = useState("overview");
  const [detailId, setDetailId] = useState(null);
  const secret = localStorage.getItem("mh-secret") || "";
  const auth = { Authorization: `Bearer ${secret}` };

  const load = async () => {
    setLoading(true);
    try {
      const [daemonRes, dreamRes, decayRes] = await Promise.all([
        fetch("/api/daemon/status", { headers: auth }),
        fetch("/api/dream/status", { headers: auth }),
        fetch("/api/memory/decay-scores", { headers: auth }),
      ]);
      if (daemonRes.ok) setDaemon(await daemonRes.json());
      if (dreamRes.ok) setDream(await dreamRes.json());
      if (decayRes.ok) setDecay(await decayRes.json());
    } catch {
      // keep last visible state
    }
    setLoading(false);
  };

  useEffect(() => { load(); }, []);

  const runDream = async (force = false) => {
    setDreamRunning(true);
    try {
      const suffix = force ? "?force=true" : "";
      await fetch(`/api/dream/run${suffix}`, { method: "POST", headers: auth });
      setTimeout(load, 1200);
    } finally {
      setDreamRunning(false);
    }
  };

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
          <div style={{ marginTop: 4, fontSize: 12, color: "var(--text-muted)" }}>后台整理、衰减分层、醒来预览和记忆库</div>
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
          ["wake", "醒来预览"],
          ["memories", "记忆库"],
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

      {tab === "wake" && <WakePreview auth={auth} />}
      {tab === "memories" && <MemoriesHubPage />}
      {tab === "overview" && (
        <>
          <LivingRoomRefresh auth={auth} />
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

            <DreamDiagnostics dream={dream} onRun={runDream} running={dreamRunning} />

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


