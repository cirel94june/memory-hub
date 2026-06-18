import { useEffect, useState, useMemo } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { Search, Trash2, Calendar, ChevronLeft, ChevronRight, ArrowLeft, User } from "lucide-react";
import Markdown from "react-markdown";
import MemoryDetailModal from "../components/MemoryDetailModal";

const ROOMS = ["", "psychology", "personality", "health", "career", "relationships", "relationship",
  "game_room", "living_room", "preferences", "infra", "infra_changelog", "diary", "work_tasks", "social"];
const ROOM_LABELS = { "": "全部", psychology: "心理", personality: "性格", health: "健康",
  career: "职业", relationships: "关系", relationship: "亲密", game_room: "游戏室",
  living_room: "客厅", preferences: "偏好", infra: "基建", infra_changelog: "更新日志",
  diary: "日记", work_tasks: "工作", social: "社交" };

const AI_LABELS = { claude: "小克", lucien: "Lucien", jasper: "Jasper", import: "导入" };
const AI_EMOJI = { claude: "🐱", lucien: "🦊", jasper: "🦜", import: "📥" };

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

function getLevel(count, max) {
  if (!count) return 0;
  if (max <= 0) return 1;
  const ratio = count / max;
  if (ratio <= 0.25) return 1;
  if (ratio <= 0.5) return 2;
  if (ratio <= 0.75) return 3;
  return 4;
}

const LEVEL_COLORS = [
  "var(--bg-hover)",
  "hsl(var(--primary-h), 50%, 80%)",
  "hsl(var(--primary-h), 55%, 65%)",
  "hsl(var(--primary-h), 60%, 50%)",
  "hsl(var(--primary-h), 65%, 38%)",
];

function CalendarHeatmap({ onSelectDay }) {
  const [calData, setCalData] = useState({});
  const [selectedDay, setSelectedDay] = useState(null);
  const [monthOffset, setMonthOffset] = useState(0);

  useEffect(() => {
    fetch("/api/memory/calendar?months=6", {
      headers: { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` },
    })
      .then((r) => r.json())
      .then((d) => setCalData(d.counts || {}))
      .catch(() => {});
  }, []);

  const maxCount = useMemo(() => Math.max(1, ...Object.values(calData)), [calData]);

  const today = new Date();
  const viewMonth = new Date(today.getFullYear(), today.getMonth() + monthOffset, 1);
  const year = viewMonth.getFullYear();
  const month = viewMonth.getMonth();
  const monthName = viewMonth.toLocaleDateString("zh-CN", { year: "numeric", month: "long" });

  const firstDow = (new Date(year, month, 1).getDay() + 6) % 7;
  const daysInMonth = new Date(year, month + 1, 0).getDate();

  const cells = [];
  for (let i = 0; i < firstDow; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) {
    const dateStr = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    cells.push({ day: d, date: dateStr, count: calData[dateStr] || 0 });
  }

  const handleClick = (cell) => {
    if (!cell) return;
    setSelectedDay(cell.date);
    onSelectDay(cell.date, cell.count);
  };

  const isToday = (dateStr) => today.toISOString().slice(0, 10) === dateStr;

  return (
    <div className="glass" style={{ padding: "10px 12px", marginBottom: "var(--space-md)" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
        <button className="btn btn-ghost" onClick={() => setMonthOffset(monthOffset - 1)}
          style={{ padding: "2px 6px" }}><ChevronLeft size={14} /></button>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{monthName}</span>
        <button className="btn btn-ghost" onClick={() => setMonthOffset(Math.min(0, monthOffset + 1))}
          disabled={monthOffset >= 0} style={{ padding: "2px 6px" }}><ChevronRight size={14} /></button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 2, textAlign: "center", maxWidth: 280, margin: "0 auto" }}>
        {WEEKDAYS.map((wd) => (
          <div key={wd} style={{ fontSize: 9, color: "var(--text-muted)", padding: "1px 0" }}>{wd}</div>
        ))}
        {cells.map((cell, i) => (
          <div key={i} onClick={() => handleClick(cell)} style={{
            width: 28, height: 28, borderRadius: 6, display: "flex",
            alignItems: "center", justifyContent: "center", fontSize: 10,
            cursor: cell ? "pointer" : "default", margin: "0 auto",
            background: cell ? LEVEL_COLORS[getLevel(cell.count, maxCount)] : "transparent",
            color: cell && getLevel(cell.count, maxCount) >= 3 ? "white" : "var(--text-secondary)",
            border: cell && selectedDay === cell.date ? "2px solid var(--primary)" :
              cell && isToday(cell.date) ? "1px solid var(--primary)" : "1px solid transparent",
            transition: "var(--transition-fast)",
            fontWeight: cell && isToday(cell.date) ? 700 : 400,
          }}>
            {cell ? cell.day : ""}
          </div>
        ))}
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 3, marginTop: 4 }}>
        <span style={{ fontSize: 9, color: "var(--text-muted)" }}>少</span>
        {LEVEL_COLORS.map((c, i) => (
          <div key={i} style={{ width: 8, height: 8, borderRadius: 2, background: c }} />
        ))}
        <span style={{ fontSize: 9, color: "var(--text-muted)" }}>多</span>
      </div>
      {detailId && (
        <MemoryDetailModal
          memoryId={detailId}
          onClose={() => setDetailId(null)}
          onNavigate={(target) => {
            if (target.startsWith("memory:")) {
              setDetailId(target.slice(7));
            } else {
              setDetailId(null);
              navigate(target);
            }
          }}
        />
      )}
    </div>
  );
}

function AiRoomView({ aiId, authHeaders, onSelectRoom, onBack }) {
  const [aiStats, setAiStats] = useState(null);

  useEffect(() => {
    fetch(`/api/stats/ai/${encodeURIComponent(aiId)}`, { headers: authHeaders })
      .then((r) => r.json())
      .then(setAiStats)
      .catch(() => {});
  }, [aiId]);

  const label = AI_LABELS[aiId] || aiId;
  const emoji = AI_EMOJI[aiId] || "🤖";
  const maxCount = aiStats ? Math.max(1, ...aiStats.rooms.map((r) => r.count)) : 1;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", marginBottom: "var(--space-md)" }}>
        <button className="btn btn-ghost" onClick={onBack} style={{ padding: "4px 8px" }}>
          <ArrowLeft size={16} />
        </button>
        <span style={{ fontSize: 24 }}>{emoji}</span>
        <div>
          <h3 style={{ fontSize: 16, fontWeight: 700, color: "var(--text-primary)", margin: 0 }}>{label}</h3>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {aiStats ? `共 ${aiStats.total} 条记忆` : "加载中..."}
          </span>
        </div>
      </div>

      {aiStats && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)" }}>
          {aiStats.rooms.map((r) => (
            <div key={r.room} className="glass" style={{
              padding: "var(--space-md)", cursor: "pointer",
              transition: "transform var(--transition-fast)",
            }}
              onClick={() => onSelectRoom(r.room)}
              onMouseEnter={(e) => e.currentTarget.style.transform = "translateY(-2px)"}
              onMouseLeave={(e) => e.currentTarget.style.transform = "none"}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                  {ROOM_LABELS[r.room] || r.room}
                </span>
                <span style={{
                  fontSize: 11, padding: "2px 8px", borderRadius: "var(--radius-sm)",
                  background: "var(--primary-light)", color: "var(--primary-dark)", fontWeight: 600,
                }}>{r.count}</span>
              </div>
              <div style={{ height: 4, background: "var(--bg-hover)", borderRadius: 2, overflow: "hidden" }}>
                <div style={{
                  height: "100%", borderRadius: 2, background: "var(--primary)",
                  width: `${(r.count / maxCount) * 100}%`, transition: "width 0.3s ease",
                }} />
              </div>
            </div>
          ))}
        </div>
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
              navigate(target);
            }
          }}
        />
      )}
    </div>
  );
}

export default function MemoriesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const aiFilter = searchParams.get("ai") || "";
  const roomFromUrl = searchParams.get("room") || "";

  const [memories, setMemories] = useState([]);
  const [total, setTotal] = useState(0);
  const [room, setRoom] = useState(roomFromUrl);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [expanded, setExpanded] = useState(null);
  const [showCal, setShowCal] = useState(!aiFilter);
  const [dayFilter, setDayFilter] = useState(null);
  const [dayMemories, setDayMemories] = useState(null);
  const [showAiRooms, setShowAiRooms] = useState(!!aiFilter && !roomFromUrl);
  const [detailId, setDetailId] = useState(null);

  const authHeaders = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const load = () => {
    setDayFilter(null);
    setDayMemories(null);
    const params = new URLSearchParams({ page, per_page: 20 });
    if (room) params.set("room", room);
    if (aiFilter) params.set("source_ai", aiFilter);
    fetch(`/api/memory/list?${params}`, { headers: authHeaders })
      .then((r) => r.json())
      .then((d) => { setMemories(d.items || []); setTotal(d.total || 0); })
      .catch(() => {});
  };

  const doSearch = () => {
    if (!search.trim()) { load(); return; }
    setDayFilter(null);
    setDayMemories(null);
    fetch(`/api/memory/recall?q=${encodeURIComponent(search)}&top_k=20`, { headers: authHeaders })
      .then((r) => r.json())
      .then((d) => { setMemories(d.results || []); setTotal(d.results?.length || 0); })
      .catch(() => {});
  };

  const loadDay = (dateStr) => {
    fetch(`/api/memory/by-date?date=${encodeURIComponent(dateStr)}`, { headers: authHeaders })
      .then((r) => r.json())
      .then((d) => setDayMemories(d.items || []))
      .catch(() => {});
  };

  useEffect(() => {
    if (aiFilter && !roomFromUrl) {
      setShowAiRooms(true);
    } else {
      setShowAiRooms(false);
      if (!dayFilter) load();
    }
  }, [room, page, aiFilter, roomFromUrl]);

  const onSelectDay = (dateStr, count) => {
    if (!count) {
      setDayFilter(dateStr);
      setDayMemories([]);
      return;
    }
    setDayFilter(dateStr);
    loadDay(dateStr);
  };

  const deleteMem = async (id) => {
    if (!confirm("确定删除这条记忆？")) return;
    await fetch(`/api/memory/${id}`, { method: "DELETE", headers: authHeaders });
    if (dayFilter) loadDay(dayFilter);
    else load();
  };

  const handleAiRoomSelect = (selectedRoom) => {
    setSearchParams({ ai: aiFilter, room: selectedRoom });
    setRoom(selectedRoom);
    setShowAiRooms(false);
    setPage(1);
  };

  const handleBackFromAiRoom = () => {
    setSearchParams({});
    setRoom("");
    setShowAiRooms(false);
    setPage(1);
  };

  const handleBackFromRoomList = () => {
    setSearchParams({ ai: aiFilter });
    setRoom("");
    setShowAiRooms(true);
    setPage(1);
  };

  const displayMemories = dayFilter !== null ? (dayMemories || []) : memories;
  const displayLabel = dayFilter ? `${dayFilter} 的记忆` : null;
  const aiLabel = AI_LABELS[aiFilter] || aiFilter;

  const renderCard = (m) => (
    <div key={m.id} className="glass" style={{ padding: "var(--space-md)", cursor: "pointer" }}
      onClick={() => setDetailId(m.id)}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: "flex", gap: "var(--space-xs)", marginBottom: "var(--space-xs)", flexWrap: "wrap" }}>
            <span style={{
              fontSize: 11, padding: "2px 8px", borderRadius: "var(--radius-sm)",
              background: "var(--primary-light)", color: "var(--primary-dark)",
            }}>{ROOM_LABELS[m.room] || m.room}</span>
            {!aiFilter && m.source_ai && (
              <span style={{
                fontSize: 11, padding: "2px 8px", borderRadius: "var(--radius-sm)",
                background: "var(--bg-hover)", color: "var(--text-secondary)", cursor: "pointer",
              }} onClick={(e) => { e.stopPropagation(); navigate(`/memories?ai=${encodeURIComponent(m.source_ai)}`); }}>
                {AI_EMOJI[m.source_ai] || "🤖"} {m.source_ai}
              </span>
            )}
            {m.importance >= 0.8 && <span style={{ fontSize: 11 }}>⭐</span>}
          </div>
          <div style={{
            fontSize: 13, lineHeight: 1.5, color: "var(--text-primary)",
            display: expanded === m.id ? "block" : "-webkit-box",
            WebkitLineClamp: expanded === m.id ? "unset" : 2,
            WebkitBoxOrient: "vertical", overflow: "hidden",
          }}>
            <Markdown components={{
              p: ({ children }) => <p style={{ margin: "0 0 4px" }}>{children}</p>,
              strong: ({ children }) => <strong style={{ color: "var(--primary-dark)" }}>{children}</strong>,
              code: ({ children }) => <code style={{ background: "var(--bg-hover)", padding: "1px 4px", borderRadius: 3, fontSize: 12 }}>{children}</code>,
            }}>{m.content}</Markdown>
          </div>
        </div>
        <button className="btn-ghost" onClick={(e) => { e.stopPropagation(); deleteMem(m.id); }}
          style={{ padding: 4, color: "var(--text-muted)", background: "none", border: "none", cursor: "pointer" }}>
          <Trash2 size={14} />
        </button>
      </div>
      {expanded === m.id && (
        <div style={{ marginTop: "var(--space-sm)", fontSize: 11, color: "var(--text-muted)" }}>
          ID: {m.id} · 重要度: {m.importance} · 来源: {m.source_ai || "unknown"}
        </div>
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
              navigate(target);
            }
          }}
        />
      )}
    </div>
  );

  if (showAiRooms && aiFilter) {
    return (
      <div style={{ maxWidth: 720, margin: "0 auto" }}>
        <AiRoomView
          aiId={aiFilter}
          authHeaders={authHeaders}
          onSelectRoom={handleAiRoomSelect}
          onBack={handleBackFromAiRoom}
        />
      </div>
    );
  }

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
          {aiFilter && (
            <button className="btn btn-ghost" onClick={handleBackFromRoomList} style={{ padding: "4px 8px" }}>
              <ArrowLeft size={16} />
            </button>
          )}
          <h2 style={{ fontSize: 20, fontWeight: 700 }}>
            {aiFilter ? (
              <>{AI_EMOJI[aiFilter] || "🤖"} {aiLabel} · {ROOM_LABELS[room] || room}</>
            ) : (
              <>记忆库</>
            )}
            {" "}<span style={{ fontSize: 14, fontWeight: 400, color: "var(--text-muted)" }}>{total}条</span>
          </h2>
        </div>
        {!aiFilter && (
          <button className={`btn ${showCal ? "btn-primary" : "btn-ghost"}`}
            onClick={() => setShowCal(!showCal)} style={{ padding: "6px 10px", fontSize: 12 }}>
            <Calendar size={14} style={{ marginRight: 4 }} /> 日历
          </button>
        )}
      </div>

      {showCal && !aiFilter && <CalendarHeatmap onSelectDay={onSelectDay} />}

      {dayFilter && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          marginBottom: "var(--space-md)", padding: "var(--space-sm) var(--space-md)",
          background: "var(--primary-light)", borderRadius: "var(--radius-sm)",
        }}>
          <span style={{ fontSize: 13, color: "var(--primary-dark)", fontWeight: 600 }}>
            {displayLabel} ({displayMemories.length}条)
          </span>
          <button className="btn btn-ghost" onClick={() => { setDayFilter(null); setDayMemories(null); load(); }}
            style={{ padding: "2px 8px", fontSize: 12 }}>返回全部</button>
        </div>
      )}

      {!dayFilter && !aiFilter && (
        <>
          <div className="glass" style={{
            display: "flex", gap: "var(--space-sm)", padding: "var(--space-sm)",
            marginBottom: "var(--space-md)",
          }}>
            <input value={search} onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && doSearch()}
              placeholder="搜索记忆..."
              style={{
                flex: 1, padding: "8px 12px", border: "none", outline: "none",
                background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                fontSize: 14, color: "var(--text-primary)",
              }} />
            <button className="btn btn-primary" onClick={doSearch} style={{ padding: "8px 12px" }}>
              <Search size={16} />
            </button>
          </div>

          <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap", marginBottom: "var(--space-md)" }}>
            {ROOMS.map((r) => (
              <button key={r} className={`btn ${room === r ? "btn-primary" : "btn-ghost"}`}
                onClick={() => { setRoom(r); setPage(1); }}
                style={{ padding: "4px 10px", fontSize: 12 }}>
                {ROOM_LABELS[r] || r}
              </button>
            ))}
          </div>
        </>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
        {displayMemories.map(renderCard)}
        {dayFilter && displayMemories.length === 0 && (
          <div style={{ textAlign: "center", padding: "var(--space-xl)", color: "var(--text-muted)", fontSize: 13 }}>
            这天没有记忆
          </div>
        )}
      </div>

      {!dayFilter && total > 20 && (
        <div style={{ display: "flex", justifyContent: "center", gap: "var(--space-sm)", marginTop: "var(--space-lg)" }}>
          <button className="btn btn-ghost" onClick={() => setPage(Math.max(1, page - 1))} disabled={page <= 1}>上一页</button>
          <span style={{ padding: "var(--space-sm)", color: "var(--text-secondary)", fontSize: 13 }}>
            {page} / {Math.ceil(total / 20)}
          </span>
          <button className="btn btn-ghost" onClick={() => setPage(page + 1)} disabled={page * 20 >= total}>下一页</button>
        </div>
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
              navigate(target);
            }
          }}
        />
      )}
    </div>
  );
}
