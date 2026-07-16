import { useEffect, useState, useMemo, useRef } from "react";
import { Clock, Filter, ChevronDown, ChevronUp, Calendar, Minus } from "lucide-react";
import Markdown from "react-markdown";
import MemoryDetailModal from "../components/MemoryDetailModal";
import { useAI } from "../contexts/AIContext";

const ROOM_LABELS = {
  psychology: "心理", personality: "性格", health: "健康", career: "职业",
  relationships: "关系", relationship: "亲密", game_room: "游戏室",
  living_room: "客厅", preferences: "偏好", infra: "基建",
  infra_changelog: "更新日志", diary: "日记", work_tasks: "工作",
  social: "社交", dreams: "梦境", learning: "学习",
};

const ROOM_EMOJI = {
  living_room: "🏠", career: "💼", psychology: "🧠", health: "❤️",
  learning: "📚", relationships: "👥", relationship: "💕",
  preferences: "✨", work_tasks: "📋", infra: "🏗️",
  infra_changelog: "📝", diary: "📔", dreams: "🌙",
  personality: "🪞", game_room: "🎮", social: "💬",
};

// AI display resolved dynamically via useAI context

const WEEKDAY_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

function relativeDate(dateStr) {
  const today = new Date();
  const d = new Date(dateStr + "T00:00:00");
  const diff = Math.floor((today - d) / 86400000);
  if (diff === 0) return "今天";
  if (diff === 1) return "昨天";
  if (diff === 2) return "前天";
  if (diff < 7) return `${diff}天前`;
  return null;
}

function formatTime(isoStr) {
  if (!isoStr || isoStr.length < 16) return "";
  const d = new Date(isoStr);
  if (isNaN(d)) return "";
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
}

function HeatDot({ heat, size = 10 }) {
  const opacity = 0.2 + heat * 0.8;
  const scale = 0.6 + heat * 0.4;
  return (
    <div style={{
      width: size, height: size, borderRadius: "50%",
      background: `var(--accent)`, opacity,
      transform: `scale(${scale})`,
      transition: "all 0.3s ease",
      flexShrink: 0,
    }} />
  );
}

function MiniNav({ timeline, activeDate, onJump }) {
  const ref = useRef(null);
  const months = useMemo(() => {
    const map = {};
    for (const day of timeline) {
      const m = day.date.slice(0, 7);
      if (!map[m]) map[m] = [];
      map[m].push(day);
    }
    return Object.entries(map);
  }, [timeline]);

  return (
    <div ref={ref} className="glass" style={{
      padding: "12px 14px", marginBottom: "var(--space-md)",
      maxHeight: 160, overflowY: "auto",
    }}>
      {months.map(([month, days]) => (
        <div key={month} style={{ marginBottom: 8 }}>
          <div style={{
            fontSize: 11, fontWeight: 700, color: "var(--text-secondary)",
            marginBottom: 4, fontFamily: "var(--serif)", fontStyle: "italic",
          }}>
            {month}
          </div>
          <div style={{ display: "flex", gap: 3, flexWrap: "wrap" }}>
            {days.map((d) => (
              <div key={d.date} onClick={() => onJump(d.date)}
                title={`${d.date} · ${d.count}条`}
                style={{
                  width: 14, height: 14, borderRadius: 3, cursor: "pointer",
                  background: d.date === activeDate
                    ? "var(--accent)"
                    : `rgba(var(--accent-rgb, 110,79,154), ${0.1 + d.heat * 0.5})`,
                  border: d.date === activeDate ? "none" : "1px solid var(--glass-border)",
                  transition: "all 0.15s ease",
                }}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function DayCard({ day, isExpanded, onToggle, onSelectMemory, isToday }) {
  const { getAI } = useAI();
  const dateObj = new Date(day.date + "T00:00:00");
  const weekday = WEEKDAY_NAMES[dateObj.getDay()];
  const rel = relativeDate(day.date);
  const topRooms = Object.entries(day.rooms || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4);

  return (
    <div id={`day-${day.date}`} className="glass" style={{
      padding: 0, overflow: "hidden",
      borderLeft: isToday ? `3px solid var(--accent)` : "3px solid transparent",
      animation: "popIn 0.22s var(--ease-pop) both",
    }}>
      {/* Day header */}
      <div onClick={onToggle} style={{
        display: "flex", alignItems: "center", gap: "var(--space-sm)",
        padding: "12px 16px", cursor: "pointer",
        background: isExpanded ? "var(--bg-hover)" : "transparent",
        transition: "background var(--transition-fast)",
      }}>
        <HeatDot heat={day.heat} size={12} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
            <span style={{
              fontSize: 15, fontWeight: 700, color: "var(--text-primary)",
              fontFamily: "var(--serif)",
            }}>
              {day.date.slice(5)}
            </span>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{weekday}</span>
            {rel && (
              <span style={{
                fontSize: 11, padding: "1px 6px", borderRadius: "var(--radius-sm)",
                background: isToday ? "var(--primary-light)" : "var(--bg-hover)",
                color: isToday ? "var(--accent)" : "var(--text-muted)",
                fontWeight: 500,
              }}>{rel}</span>
            )}
          </div>
          <div style={{
            display: "flex", gap: 4, marginTop: 3, flexWrap: "wrap",
          }}>
            {topRooms.map(([r, cnt]) => (
              <span key={r} style={{
                fontSize: 10, color: "var(--text-muted)",
              }}>{ROOM_EMOJI[r] || "📦"}{cnt}</span>
            ))}
          </div>
        </div>
        <span style={{
          fontSize: 13, fontWeight: 600, color: "var(--accent)",
          padding: "2px 8px", borderRadius: "var(--radius-sm)",
          background: "var(--primary-light)", whiteSpace: "nowrap",
        }}>{day.count}条</span>
        {isExpanded
          ? <ChevronUp size={16} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
          : <ChevronDown size={16} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
        }
      </div>

      {/* Expanded memory list */}
      {isExpanded && (
        <div style={{ padding: "0 16px 12px" }}>
          <div style={{
            borderLeft: "2px solid var(--glass-border)",
            marginLeft: 5, paddingLeft: 16,
          }}>
            {day.memories.map((m, i) => (
              <div key={m.id} onClick={() => onSelectMemory(m.id)}
                style={{
                  position: "relative", padding: "8px 0",
                  borderBottom: i < day.memories.length - 1 ? "1px solid var(--glass-border)" : "none",
                  cursor: "pointer",
                  transition: "background var(--transition-fast)",
                }}>
                {/* Timeline dot */}
                <div style={{
                  position: "absolute", left: -22, top: 14,
                  width: 8, height: 8, borderRadius: "50%",
                  background: getAI(m.source_ai)?.color || "var(--text-muted)",
                  border: "2px solid var(--bg-card)",
                  boxShadow: `0 0 0 1px ${getAI(m.source_ai)?.color || "var(--glass-border)"}`,
                }} />

                <div style={{
                  display: "flex", alignItems: "center", gap: 6, marginBottom: 3,
                }}>
                  <span style={{
                    fontSize: 11, color: "var(--text-muted)",
                    fontVariantNumeric: "tabular-nums", minWidth: 36,
                  }}>{formatTime(m.created_at)}</span>
                  <span style={{
                    fontSize: 10, padding: "1px 5px", borderRadius: 4,
                    background: "var(--primary-light)", color: "var(--primary-dark)",
                  }}>{ROOM_EMOJI[m.room] || ""} {ROOM_LABELS[m.room] || m.room}</span>
                  {m.source_ai && (
                    <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                      {getAI(m.source_ai)?.emoji || "🤖"}
                    </span>
                  )}
                  {m.importance >= 0.8 && <span style={{ fontSize: 10 }}>⭐</span>}
                </div>
                <div style={{
                  fontSize: 13, lineHeight: 1.55, color: "var(--text-primary)",
                  display: "-webkit-box", WebkitLineClamp: 2,
                  WebkitBoxOrient: "vertical", overflow: "hidden",
                }}>
                  <Markdown components={{
                    p: ({ children }) => <span>{children}</span>,
                    strong: ({ children }) => <strong style={{ color: "var(--primary-dark)" }}>{children}</strong>,
                  }}>{m.content}</Markdown>
                </div>
                {Array.isArray(m.tags) && m.tags.length > 0 && (
                  <div style={{ display: "flex", gap: 4, marginTop: 3, flexWrap: "wrap" }}>
                    {m.tags.slice(0, 3).map((t, i) => (
                      <span key={i} className="chip" style={{ fontSize: 9, padding: "0 4px" }}>{t}</span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function GapIndicator({ count }) {
  if (count <= 0) return null;
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "4px 0", margin: "0 16px",
    }}>
      <div style={{ flex: 1, height: 1, background: "var(--glass-border)" }} />
      <span style={{ fontSize: 11, color: "var(--text-muted)", whiteSpace: "nowrap" }}>
        <Minus size={10} style={{ verticalAlign: "middle" }} /> {count}天无记忆
      </span>
      <div style={{ flex: 1, height: 1, background: "var(--glass-border)" }} />
    </div>
  );
}

function MonthDivider({ month }) {
  const [y, m] = month.split("-");
  return (
    <div style={{
      padding: "var(--space-md) 0 var(--space-sm)",
      display: "flex", alignItems: "center", gap: "var(--space-sm)",
    }}>
      <span style={{
        fontSize: 18, fontWeight: 700, fontFamily: "var(--serif)",
        fontStyle: "italic", color: "var(--text-primary)",
      }}>{parseInt(m)}月</span>
      <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{y}</span>
      <div style={{ flex: 1, height: 1, background: "var(--glass-border)", marginLeft: 8 }} />
    </div>
  );
}

const ROOMS_ALL = ["", "living_room", "psychology", "personality", "health", "career",
  "relationships", "relationship", "preferences", "work_tasks", "diary", "dreams",
  "learning", "infra", "game_room"];
const ROOMS_FILTER_LABELS = { "": "全部", ...ROOM_LABELS };

export default function TimelinePage() {
  const { profiles, getAI } = useAI();
  const [timeline, setTimeline] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(new Set());
  const [detailId, setDetailId] = useState(null);
  const [days, setDays] = useState(90);
  const [showFilters, setShowFilters] = useState(false);
  const [roomFilter, setRoomFilter] = useState("");
  const [aiFilter, setAiFilter] = useState("");

  const authHeaders = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = () => {
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ days });
    if (roomFilter) params.set("room", roomFilter);
    if (aiFilter) params.set("source_ai", aiFilter);
    fetch(`/api/memory/timeline?${params}`, { headers: authHeaders })
      .then((r) => {
        if (!r.ok) throw new Error(`API ${r.status}`);
        return r.json();
      })
      .then((d) => {
        setTimeline(d.timeline || []);
        if (d.timeline && d.timeline.length > 0) {
          setExpanded(new Set([d.timeline[0].date]));
        }
      })
      .catch((e) => setError(e.message || "加载失败"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [days, roomFilter, aiFilter]);

  const todayStr = new Date().toISOString().slice(0, 10);

  const toggleDay = (date) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(date)) next.delete(date);
      else next.add(date);
      return next;
    });
  };

  const jumpToDay = (date) => {
    setExpanded((prev) => new Set(prev).add(date));
    setTimeout(() => {
      const el = document.getElementById(`day-${date}`);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 50);
  };

  const totalMemories = useMemo(
    () => timeline.reduce((s, d) => s + d.count, 0),
    [timeline],
  );

  const renderItems = useMemo(() => {
    const items = [];
    let lastMonth = null;
    let lastDate = null;

    for (const day of timeline) {
      const month = day.date.slice(0, 7);
      if (month !== lastMonth) {
        items.push({ type: "month", month, key: `m-${month}` });
        lastMonth = month;
      }

      if (lastDate) {
        const prev = new Date(lastDate + "T00:00:00");
        const curr = new Date(day.date + "T00:00:00");
        const gap = Math.floor((prev - curr) / 86400000) - 1;
        if (gap > 0) {
          items.push({ type: "gap", count: gap, key: `g-${day.date}` });
        }
      }

      items.push({ type: "day", day, key: `d-${day.date}` });
      lastDate = day.date;
    }
    return items;
  }, [timeline]);

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      {/* Header */}
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        marginBottom: "var(--space-md)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
          <Clock size={22} style={{ color: "var(--accent)" }} />
          <h2 style={{ fontSize: 20, fontWeight: 700 }}>时间线</h2>
          <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
            {timeline.length}天 · {totalMemories}条
          </span>
        </div>
        <div style={{ display: "flex", gap: "var(--space-xs)" }}>
          <button
            className={`btn ${showFilters ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setShowFilters(!showFilters)}
            style={{ padding: "6px 10px", fontSize: 12 }}
          >
            <Filter size={14} /> 筛选
          </button>
        </div>
      </div>

      {/* Filters */}
      {showFilters && (
        <div className="glass anim-pop" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <div style={{ marginBottom: "var(--space-sm)" }}>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>时间范围</div>
            <div style={{ display: "flex", gap: "var(--space-xs)" }}>
              {[30, 60, 90, 180, 365].map((d) => (
                <button key={d}
                  className={`btn ${days === d ? "btn-primary" : "btn-ghost"}`}
                  onClick={() => setDays(d)}
                  style={{ padding: "4px 10px", fontSize: 12 }}
                >{d <= 90 ? `${d}天` : d === 180 ? "半年" : "一年"}</button>
              ))}
            </div>
          </div>
          <div style={{ marginBottom: "var(--space-sm)" }}>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>房间</div>
            <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap" }}>
              {ROOMS_ALL.map((r) => (
                <button key={r}
                  className={`btn ${roomFilter === r ? "btn-primary" : "btn-ghost"}`}
                  onClick={() => setRoomFilter(r)}
                  style={{ padding: "4px 8px", fontSize: 11 }}
                >{ROOM_EMOJI[r] || ""} {ROOMS_FILTER_LABELS[r] || r}</button>
              ))}
            </div>
          </div>
          <div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4 }}>AI</div>
            <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap" }}>
              <button className={`btn ${aiFilter === "" ? "btn-primary" : "btn-ghost"}`}
                onClick={() => setAiFilter("")} style={{ padding: "4px 10px", fontSize: 12 }}>全部</button>
              {profiles.map((p) => (
                <button key={p.ai_id}
                  className={`btn ${aiFilter === p.ai_id ? "btn-primary" : "btn-ghost"}`}
                  onClick={() => setAiFilter(p.ai_id)}
                  style={{ padding: "4px 10px", fontSize: 12 }}
                >{p.emoji} {p.name}</button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Mini navigation */}
      {timeline.length > 5 && (
        <MiniNav timeline={timeline} activeDate={todayStr} onJump={jumpToDay} />
      )}

      {/* Loading */}
      {loading && (
        <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)" }}>
          <div style={{ animation: "pulse 1.5s ease infinite" }}>加载时间线...</div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="glass" style={{
          padding: "var(--space-md)", marginBottom: "var(--space-md)",
          color: "var(--text-primary)", fontSize: 13,
          borderLeft: "3px solid #d4756a",
        }}>
          加载出错：{error}
          <button className="btn btn-ghost" onClick={load}
            style={{ marginLeft: 12, fontSize: 12 }}>重试</button>
        </div>
      )}

      {/* Timeline */}
      {!loading && (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
          {renderItems.map((item) => {
            if (item.type === "month") {
              return <MonthDivider key={item.key} month={item.month} />;
            }
            if (item.type === "gap") {
              return <GapIndicator key={item.key} count={item.count} />;
            }
            return (
              <DayCard
                key={item.key}
                day={item.day}
                isExpanded={expanded.has(item.day.date)}
                onToggle={() => toggleDay(item.day.date)}
                onSelectMemory={setDetailId}
                isToday={item.day.date === todayStr}
              />
            );
          })}

          {timeline.length === 0 && (
            <div style={{
              textAlign: "center", padding: "var(--space-xl)",
              color: "var(--text-muted)", fontSize: 14,
            }}>
              <Calendar size={32} style={{ marginBottom: 8, opacity: 0.4 }} />
              <div>这段时间没有记忆</div>
            </div>
          )}
        </div>
      )}

      {/* Memory Detail Modal */}
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
