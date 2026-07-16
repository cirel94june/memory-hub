import { useEffect, useState } from "react";
import { Brain, MessageCircle, Heart, TrendingUp, Sparkles, Zap, HeartPulse } from "lucide-react";
import { Link } from "react-router-dom";
import { useAI } from "../contexts/AIContext";

const ROOM_LABELS = {
  psychology: "心理", personality: "性格", health: "健康", career: "职业",
  relationships: "关系", relationship: "亲密", game_room: "游戏室",
  living_room: "客厅", preferences: "偏好", infra: "基建",
  infra_changelog: "更新日志", diary: "日记", work_tasks: "工作", social: "社交",
};

export default function HomePage() {
  const { profiles, getAI } = useAI();
  const [stats, setStats] = useState(null);
  const [personas, setPersonas] = useState({});
  const [whispers, setWhispers] = useState([]);

  const authHeaders = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  useEffect(() => {
    fetch("/api/stats", { headers: authHeaders })
      .then((r) => r.json())
      .then(setStats)
      .catch(() => {});

    profiles.forEach((p) => {
      fetch(`/api/persona/${p.ai_id}`, { headers: authHeaders })
        .then((r) => r.json())
        .then((data) => setPersonas((prev) => ({ ...prev, [p.ai_id]: data })))
        .catch(() => {});
    });

    fetch("/api/whispers?limit=5", { headers: authHeaders })
      .then((r) => r.json())
      .then((d) => setWhispers(d.whispers || []))
      .catch(() => {});
  }, [profiles]);

  const startDate = new Date("2026-02-23");
  const today = new Date();
  const daysTogether = Math.floor((today - startDate) / 86400000);

  return (
    <div style={{ maxWidth: 640, margin: "0 auto" }}>
      {/* Hero card */}
      <div className="glass" style={{ padding: "var(--space-xl)", textAlign: "center", marginBottom: "var(--space-lg)" }}>
        <div style={{ fontSize: 64, marginBottom: "var(--space-md)" }}>🐱</div>
        <h2 style={{ fontSize: 22, marginBottom: "var(--space-sm)" }}>小猫 & AI 们</h2>
        <p style={{ color: "var(--text-secondary)", fontSize: 14, marginBottom: "var(--space-lg)" }}>
          在一起的第 <strong style={{ color: "var(--primary)", fontSize: 20 }}>{daysTogether}</strong> 天
        </p>
        <div style={{ display: "flex", gap: "var(--space-md)", justifyContent: "center", flexWrap: "wrap" }}>
          <StatBadge icon={<Brain size={18} />} value={stats?.total ?? "..."} label="记忆" />
          <StatBadge icon={<TrendingUp size={18} />} value={stats?.this_week ?? "..."} label="本周新增" />
          <StatBadge icon={<Heart size={18} />} value="∞" label="想你" />
        </div>
      </div>

      {/* All AI Status Cards — dynamic from profiles */}
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
        {profiles.map((ai) => {
          const p = personas[ai.ai_id];
          if (!p) return null;
          return (
            <Link key={ai.ai_id} to="/chat" onClick={() => {
              localStorage.setItem("mh-chat-ai", ai.ai_id);
              localStorage.setItem("mh-ai-id", ai.ai_id);
            }} style={{ textDecoration: "none", color: "inherit" }}>
              <div className="glass" style={{
                padding: "var(--space-md)", cursor: "pointer",
                transition: "transform var(--transition-fast)",
                borderLeft: `3px solid ${ai.color}`,
              }}
                onMouseEnter={(e) => e.currentTarget.style.transform = "translateY(-1px)"}
                onMouseLeave={(e) => e.currentTarget.style.transform = "none"}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
                  <span style={{ fontSize: 28 }}>{ai.emoji}</span>
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{ai.name}</span>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
                      <span style={{ fontSize: 16 }}>{p.mood_emoji}</span>
                      <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                        {p.mood_text} · 精力 {Math.round(p.energy * 100)}%
                      </span>
                      {p.last_topics?.length > 0 && (
                        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                          · 最近聊了 {p.last_topics[0]}
                        </span>
                      )}
                    </div>
                    <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                      <MiniBar value={p.mood_valence} color={ai.color} label="心情" />
                      <MiniBar value={p.energy} color="hsl(140, 50%, 55%)" label="精力" />
                    </div>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--primary)", fontWeight: 500 }}>聊天 →</div>
                </div>
              </div>
            </Link>
          );
        })}
      </div>

      {/* Top rooms */}
      {stats?.top_rooms?.length > 0 && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: "var(--space-sm)", color: "var(--text-primary)" }}>
            <Sparkles size={14} style={{ verticalAlign: -2, marginRight: 4 }} />
            记忆分布
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {stats.top_rooms.slice(0, 5).map((r) => (
              <div key={r.room} style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
                <span style={{ fontSize: 11, width: 48, textAlign: "right", color: "var(--text-muted)", flexShrink: 0 }}>
                  {ROOM_LABELS[r.room] || r.room}
                </span>
                <div style={{ flex: 1, height: 12, background: "var(--bg-hover)", borderRadius: 6, overflow: "hidden" }}>
                  <div style={{
                    height: "100%", borderRadius: 6,
                    background: "var(--primary)",
                    width: `${Math.min(100, (r.count / stats.total) * 100 * 3)}%`,
                    transition: "width 0.5s ease",
                  }} />
                </div>
                <span style={{ fontSize: 10, color: "var(--text-muted)", width: 24 }}>{r.count}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* AI sources */}
      {stats?.ai_sources?.length > 0 && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: "var(--space-sm)", color: "var(--text-primary)" }}>
            <Zap size={14} style={{ verticalAlign: -2, marginRight: 4 }} />
            谁在记忆
          </div>
          <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap" }}>
            {stats.ai_sources.map((s) => {
              const ai = getAI(s.ai);
              return (
                <Link key={s.ai} to={`/memories?ai=${encodeURIComponent(s.ai)}`} style={{ textDecoration: "none" }}>
                  <div style={{
                    padding: "4px 10px", borderRadius: "var(--radius-sm)",
                    background: "var(--primary-light)", fontSize: 12,
                    color: "var(--primary-dark)", cursor: "pointer",
                    transition: "var(--transition-fast)",
                  }}
                    onMouseEnter={(e) => e.currentTarget.style.opacity = "0.8"}
                    onMouseLeave={(e) => e.currentTarget.style.opacity = "1"}
                  >
                    {ai.emoji} {ai.name} <span style={{ fontWeight: 600 }}>{s.count}</span>
                  </div>
                </Link>
              );
            })}
          </div>
        </div>
      )}

      {/* Heart Whispers */}
      {whispers.length > 0 && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: "var(--space-sm)", color: "var(--text-primary)" }}>
            <HeartPulse size={14} style={{ verticalAlign: -2, marginRight: 4 }} />
            心语
            <span style={{ fontSize: 11, fontWeight: 400, color: "var(--text-muted)", marginLeft: 6 }}>AI 的内心独白</span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {whispers.map((w) => {
              const ai = getAI(w.ai_id);
              const time = w.created_at ? new Date(w.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" }) : "";
              return (
                <div key={w.id} style={{
                  padding: "8px 12px", borderRadius: "var(--radius-sm)",
                  background: "var(--bg-hover)", fontSize: 12,
                }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2 }}>
                    <span>{ai.emoji}</span>
                    <span style={{ color: "var(--text-muted)", fontSize: 10 }}>{ai.name} · {time}</span>
                  </div>
                  <div style={{ color: "var(--text-secondary)", fontStyle: "italic", lineHeight: 1.5 }}>
                    {w.content}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Quick actions */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-md)" }}>
        <QuickCard to="/chat" icon={<MessageCircle />} title="聊天" desc="和 AI 们对话" />
        <QuickCard to="/memories" icon={<Brain />} title="记忆库" desc={`${stats?.total ?? "..."}条记忆`} />
      </div>
    </div>
  );
}

function StatBadge({ icon, value, label }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center", gap: 2,
      padding: "var(--space-sm) var(--space-md)",
      background: "var(--bg-hover)", borderRadius: "var(--radius-md)",
      minWidth: 72,
    }}>
      <div style={{ color: "var(--primary)", display: "flex", alignItems: "center", gap: 4 }}>
        {icon} <span style={{ fontSize: 18, fontWeight: 700 }}>{value}</span>
      </div>
      <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{label}</span>
    </div>
  );
}

function MiniBar({ value, color }) {
  return (
    <div style={{ flex: 1, maxWidth: 100 }}>
      <div style={{ height: 4, background: "var(--bg-hover)", borderRadius: 2, overflow: "hidden" }}>
        <div style={{
          height: "100%", borderRadius: 2, background: color,
          width: `${value * 100}%`, transition: "width 0.5s ease",
        }} />
      </div>
    </div>
  );
}

function QuickCard({ to, icon, title, desc }) {
  return (
    <Link to={to} style={{ textDecoration: "none", color: "inherit" }}>
      <div className="glass" style={{
        padding: "var(--space-lg)", cursor: "pointer",
        transition: "transform var(--transition-fast)",
      }}
        onMouseEnter={(e) => e.currentTarget.style.transform = "translateY(-2px)"}
        onMouseLeave={(e) => e.currentTarget.style.transform = "none"}
      >
        <div style={{ color: "var(--primary)", marginBottom: "var(--space-sm)" }}>{icon}</div>
        <div style={{ fontWeight: 600, marginBottom: 2 }}>{title}</div>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{desc}</div>
      </div>
    </Link>
  );
}
