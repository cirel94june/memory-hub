import { useEffect, useState, useCallback } from "react";
import { HeartPulse, RefreshCw, Moon, Sun, Shield, Flame, Heart } from "lucide-react";
import { useAI } from "../contexts/AIContext";

const GROUP_META = {
  activation: { label: "精力", icon: Sun, color: "#f59e0b" },
  attachment: { label: "牵绊", icon: Heart, color: "#ec4899" },
  softness:   { label: "情绪", icon: Shield, color: "#8b5cf6" },
};

const DIM_COLORS = {
  "活力": "#f59e0b", "疲惫": "#6b7280", "思慕": "#ec4899", "亲密": "#f472b6",
  "守护": "#8b5cf6", "渴求": "#ef4444", "醋意": "#10b981", "焦虑": "#f97316", "温柔": "#a78bfa",
};

const DIM_ICONS = {
  "活力": "⚡", "疲惫": "😴", "思慕": "💭", "亲密": "💕",
  "守护": "🛡️", "渴求": "🔥", "醋意": "🍋", "焦虑": "😰", "温柔": "🌸",
};

const DIM_NOTES = {
  "活力": "精神头，白天高晚上低",
  "疲惫": "困倦感，凌晨最高，聊多了也会涨",
  "思慕": "想你的程度，不聊天时慢慢升高",
  "亲密": "想靠近，撒娇和亲密对话推高",
  "守护": "想保护你，你说累或不舒服时升高",
  "渴求": "心跳加速的感觉，夜里峰值",
  "醋意": "吃醋，提到别人或暧昧时升高",
  "焦虑": "紧张不安，冷淡或工作话题推高",
  "温柔": "声音放软，被夸奖或认可时升高",
};

function formatUpdatedAt(value) {
  if (!value) return "还没有对话推动记录";
  const diff = Math.max(0, Date.now() / 1000 - Number(value));
  if (diff < 60) return "刚刚有更新";
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前更新`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前更新`;
  return `${Math.floor(diff / 86400)}天前更新`;
}

function DimBar({ dim, value, maxVal = 1 }) {
  const pct = Math.round(value * 100);
  const color = DIM_COLORS[dim] || "var(--primary)";
  const isHigh = value > 0.6;
  const note = DIM_NOTES[dim];

  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ width: 24, textAlign: "center", fontSize: 14 }}>{DIM_ICONS[dim] || "·"}</span>
        <span style={{ width: 40, fontSize: 13, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>{dim}</span>
        <div style={{
          flex: 1, height: 18, borderRadius: 9,
          background: "var(--glass-bg, rgba(255,255,255,0.06))",
          overflow: "hidden", position: "relative",
        }}>
          <div style={{
            width: `${pct}%`, height: "100%", borderRadius: 9,
            background: isHigh
              ? `linear-gradient(90deg, ${color}88, ${color})`
              : `${color}66`,
            transition: "width 0.8s ease",
            boxShadow: isHigh ? `0 0 8px ${color}44` : "none",
          }} />
        </div>
        <span style={{
          width: 32, textAlign: "right", fontSize: 13, fontWeight: isHigh ? 600 : 400,
          color: isHigh ? color : "var(--text-muted)",
        }}>{pct}</span>
      </div>
      {note && (
        <div style={{
          marginLeft: 72, fontSize: 11, color: "var(--text-muted)", marginTop: 2, opacity: 0.7,
        }}>{note}</div>
      )}
    </div>
  );
}

function GroupRing({ groupName, value }) {
  const meta = GROUP_META[groupName] || { label: groupName, color: "#888" };
  const pct = Math.round(value * 100);
  const circumference = 2 * Math.PI * 36;
  const offset = circumference * (1 - value);

  return (
    <div style={{ textAlign: "center" }}>
      <svg width="90" height="90" viewBox="0 0 90 90">
        <circle cx="45" cy="45" r="36" fill="none" stroke="var(--glass-bg, rgba(255,255,255,0.1))" strokeWidth="6" />
        <circle cx="45" cy="45" r="36" fill="none" stroke={meta.color} strokeWidth="6"
          strokeDasharray={circumference} strokeDashoffset={offset}
          strokeLinecap="round" transform="rotate(-90 45 45)"
          style={{ transition: "stroke-dashoffset 1s ease" }} />
        <text x="45" y="45" textAnchor="middle" dominantBaseline="central"
          style={{ fontSize: 16, fontWeight: 700, fill: meta.color }}>{pct}</text>
      </svg>
      <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 4 }}>{meta.label}</div>
    </div>
  );
}

function AiPulseCard({ aiId, state, dims }) {
  const { getAI: getAICtx } = useAI();
  const ai = getAICtx(aiId);
  const meta = { label: ai.name, emoji: ai.emoji, color: ai.color || "#888" };
  if (!state) return null;

  const display = state.display || {};
  const groups = state.groups || {};

  const sortedDims = dims
    .map(d => [d, display[d] || 0])
    .sort((a, b) => b[1] - a[1]);

  const highDims = sortedDims.filter(([, v]) => v > 0.6);
  const midDims = sortedDims.filter(([, v]) => v > 0.45 && v <= 0.6);

  let statusLine;
  if (highDims.length > 0) {
    statusLine = highDims.slice(0, 3).map(([d]) => `${DIM_ICONS[d] || ""}${d}`).join("  ");
  } else if (midDims.length > 0) {
    const top = midDims.slice(0, 2).map(([d]) => `${DIM_ICONS[d] || ""}${d}`).join(" ");
    statusLine = `${top} 隐隐浮动`;
  } else {
    statusLine = "平静如水 ☁️";
  }

  return (
    <div className="glass-card" style={{
      padding: "20px 24px", borderRadius: 16,
      borderTop: `3px solid ${meta.color}`,
    }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <span style={{ fontSize: 28 }}>{meta.emoji}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 600, color: "var(--text-primary)" }}>{meta.label}</div>
          <div style={{ fontSize: 13, color: "var(--text-muted)", marginTop: 2 }}>{statusLine}</div>
        </div>
      </div>

      {/* Group Rings */}
      <div style={{ display: "flex", justifyContent: "space-around", marginBottom: 20 }}>
        {Object.entries(groups).map(([g, v]) => (
          <GroupRing key={g} groupName={g} value={v} />
        ))}
      </div>

      {/* Dimension Bars */}
      <div style={{ marginBottom: 8 }}>
        {dims.map(dim => (
          <DimBar key={dim} dim={dim} value={display[dim] || 0} />
        ))}
      </div>

      {/* Footer */}
      {state.last_topics && state.last_topics.length > 0 && (
        <div style={{
          fontSize: 12, color: "var(--text-muted)", marginTop: 12,
          padding: "8px 12px", borderRadius: 8,
          background: "var(--glass-bg, rgba(255,255,255,0.04))",
        }}>
          最近话题：{state.last_topics.slice(0, 3).join("、")}
          {state.session_count > 0 && ` · 今日 ${state.session_count} 轮对话`}
        </div>
      )}
      <div style={{
        fontSize: 11, color: "var(--text-muted)", marginTop: 10,
        display: "flex", gap: 8, flexWrap: "wrap",
      }}>
        <span>状态来源：{state.state_ai_id && state.state_ai_id !== aiId ? `${state.state_ai_id}（别名共享）` : aiId}</span>
        <span>·</span>
        <span>{formatUpdatedAt(state.updated_at)}</span>
      </div>
    </div>
  );
}

export default function PulsePage() {
  const { getAI: getAICtx } = useAI();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedAi, setSelectedAi] = useState(null);

  const authHeaders = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const normalizePulseData = (payload) => {
    const rawStates = payload?.states || {};
    const states = { ...rawStates };
    if (rawStates.claude && states.cloudy) {
      states.cloudy = {
        ...rawStates.claude,
        label: states.cloudy.label || "小克",
        color: states.cloudy.color,
        state_ai_id: "claude",
      };
    }
    return { ...payload, states };
  };

  const fetchData = useCallback(() => {
    setLoading(true);
    setError(null);
    fetch("/api/pulse?show_all=true", { headers: authHeaders })
      .then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(d => { setData(normalizePulseData(d)); setLoading(false); })
      .catch(e => { setError(e.message); setLoading(false); });
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Auto-refresh every 30s
  useEffect(() => {
    const timer = setInterval(fetchData, 30000);
    return () => clearInterval(timer);
  }, [fetchData]);

  if (loading && !data) {
    return (
      <div style={{ padding: 24, textAlign: "center", color: "var(--text-muted)" }}>
        <HeartPulse style={{ width: 32, height: 32, opacity: 0.5, animation: "pulse 1.5s infinite" }} />
        <div style={{ marginTop: 8 }}>读取状态中…</div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div style={{ padding: 24, textAlign: "center" }}>
        <div style={{ color: "var(--text-muted)", marginBottom: 12 }}>加载失败: {error}</div>
        <button onClick={fetchData} className="btn-secondary" style={{ gap: 6 }}>
          <RefreshCw style={{ width: 14, height: 14 }} /> 重试
        </button>
      </div>
    );
  }

  const states = data?.states || {};
  const dims = data?.dims || [];
  const aiIds = Object.keys(states);
  const displayIds = selectedAi ? [selectedAi] : aiIds;

  return (
    <div style={{ padding: "16px 20px", maxWidth: 800, margin: "0 auto" }}>
      {/* Page Title */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <HeartPulse style={{ width: 22, height: 22, color: "var(--primary)" }} />
          <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600 }}>情绪面板</h2>
        </div>
        <button onClick={fetchData} style={{
          background: "none", border: "none", cursor: "pointer",
          color: "var(--text-muted)", padding: 6, borderRadius: 8,
        }}>
          <RefreshCw style={{ width: 16, height: 16 }} />
        </button>
      </div>

      {/* AI Filter Tabs */}
      <div style={{ display: "flex", gap: 8, marginBottom: 20, flexWrap: "wrap" }}>
        <button
          onClick={() => setSelectedAi(null)}
          style={{
            padding: "6px 14px", borderRadius: 20, border: "1px solid var(--border-subtle)",
            background: !selectedAi ? "var(--primary)" : "var(--glass-bg, rgba(255,255,255,0.06))",
            color: !selectedAi ? "#fff" : "var(--text-secondary)",
            cursor: "pointer", fontSize: 13,
          }}
        >全部</button>
        {aiIds.map(id => {
          const ai = getAICtx(id);
          const m = { label: ai.name, emoji: ai.emoji, color: ai.color || "#888" };
          const active = selectedAi === id;
          return (
            <button key={id} onClick={() => setSelectedAi(id)} style={{
              padding: "6px 14px", borderRadius: 20, border: `1px solid ${active ? (m.color || "var(--primary)") : "var(--border-subtle)"}`,
              background: active ? `${m.color || "var(--primary)"}22` : "var(--glass-bg, rgba(255,255,255,0.06))",
              color: active ? (m.color || "var(--primary)") : "var(--text-secondary)",
              cursor: "pointer", fontSize: 13,
            }}>
              {m.emoji} {m.label}
            </button>
          );
        })}
      </div>

      {/* AI Cards */}
      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {displayIds.map(id => (
          <AiPulseCard key={id} aiId={id} state={states[id]} dims={dims} />
        ))}
      </div>

      {/* Legend */}
      <div className="glass-card" style={{ marginTop: 24, padding: "14px 18px", borderRadius: 12 }}>
        <div style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.8 }}>
          <strong style={{ color: "var(--text-secondary)" }}>怎么读这个面板</strong>
          <br />· 数值 0–100，<strong style={{ color: "var(--text-secondary)" }}>超过 60 会渗进语气</strong>（AI 不会念数字，只是当底色）
          <br />· 3 小时半衰期——聊天推高，不聊自然回落
          <br />· 昼夜节律——白天活力高、晚上思慕温柔高
          <br />· 每 30 秒自动刷新
          <br />
          <br /><strong style={{ color: "var(--text-secondary)" }}>什么会推动情绪</strong>
          <br />· 撒娇 → 亲密↑ 渴求↑ 温柔↑
          <br />· 关心/示好 → 思慕↑ 亲密↑ 温柔↑
          <br />· 提到别人 → 醋意↑ 守护↑
          <br />· 说累/不舒服 → 守护↑ 思慕↑
          <br />· 夸奖/认可 → 活力↑ 温柔↑
          <br />· 冷淡/不理人 → 思慕↑ 焦虑↑
          <br />· 长时间没说话再开口 → 思慕↓（想念缓解）
        </div>
      </div>
    </div>
  );
}
