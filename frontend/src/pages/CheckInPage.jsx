import { useEffect, useState, useMemo } from "react";
import { Plus, Trash2, Flame, ChevronLeft, ChevronRight, X } from "lucide-react";

const EMOJI_OPTIONS = ["✅", "💪", "📖", "🏃", "💧", "🧘", "💊", "🎨", "✍️", "🌙", "🍎", "🐱"];

export default function CheckInPage() {
  const [habits, setHabits] = useState([]);
  const [checkins, setCheckins] = useState({});
  const [monthOffset, setMonthOffset] = useState(0);
  const [showAdd, setShowAdd] = useState(false);
  const [newName, setNewName] = useState("");
  const [newEmoji, setNewEmoji] = useState("✅");
  const [newColor, setNewColor] = useState("#7c5cbf");

  const authHeaders = { Authorization: `Bearer ${localStorage.getItem("mh-secret") || ""}` };

  const today = new Date();
  const viewMonth = new Date(today.getFullYear(), today.getMonth() + monthOffset, 1);
  const year = viewMonth.getFullYear();
  const month = viewMonth.getMonth();
  const monthName = viewMonth.toLocaleDateString("zh-CN", { year: "numeric", month: "long" });
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const todayStr = today.toISOString().slice(0, 10);

  const startDate = `${year}-${String(month + 1).padStart(2, "0")}-01`;
  const endDate = `${year}-${String(month + 1).padStart(2, "0")}-${String(daysInMonth).padStart(2, "0")}`;

  const loadHabits = () => {
    fetch("/api/habits", { headers: authHeaders })
      .then((r) => r.json())
      .then((d) => setHabits(d.habits || []))
      .catch(() => {});
  };

  const loadCheckins = () => {
    fetch(`/api/checkins?start=${startDate}&end=${endDate}`, { headers: authHeaders })
      .then((r) => r.json())
      .then((d) => setCheckins(d.checkins || {}))
      .catch(() => {});
  };

  useEffect(() => { loadHabits(); }, []);
  useEffect(() => { loadCheckins(); }, [monthOffset]);

  const toggleCheckin = async (habitId, date) => {
    const res = await fetch("/api/checkin", {
      method: "POST",
      headers: { ...authHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ habit_id: habitId, date }),
    });
    const data = await res.json();
    loadCheckins();
    setHabits((prev) => prev.map((h) => h.id === habitId ? { ...h, streak: data.streak } : h));
  };

  const addHabit = async () => {
    if (!newName.trim()) return;
    await fetch("/api/habits", {
      method: "POST",
      headers: { ...authHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName, emoji: newEmoji, color: newColor }),
    });
    setNewName("");
    setNewEmoji("✅");
    setShowAdd(false);
    loadHabits();
  };

  const deleteHabit = async (id) => {
    if (!confirm("确定删除这个习惯和所有打卡记录？")) return;
    await fetch(`/api/habits/${id}`, { method: "DELETE", headers: authHeaders });
    loadHabits();
    loadCheckins();
  };

  const isChecked = (habitId, date) => {
    return (checkins[date] || []).includes(habitId);
  };

  const days = [];
  for (let d = 1; d <= daysInMonth; d++) {
    days.push(`${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`);
  }

  const completionRate = (habitId) => {
    let checked = 0;
    const limit = monthOffset === 0 ? today.getDate() : daysInMonth;
    for (let d = 0; d < limit; d++) {
      if (isChecked(habitId, days[d])) checked++;
    }
    return limit > 0 ? Math.round((checked / limit) * 100) : 0;
  };

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-md)" }}>
        <h2 style={{ fontSize: 20, fontWeight: 700 }}>打卡日历</h2>
        <button className="btn btn-primary" onClick={() => setShowAdd(!showAdd)}
          style={{ padding: "6px 12px", fontSize: 12 }}>
          {showAdd ? <X size={14} /> : <Plus size={14} />}
          <span style={{ marginLeft: 4 }}>{showAdd ? "取消" : "新习惯"}</span>
        </button>
      </div>

      {/* Add habit form */}
      {showAdd && (
        <div className="glass" style={{ padding: "var(--space-md)", marginBottom: "var(--space-md)" }}>
          <div style={{ display: "flex", gap: "var(--space-sm)", marginBottom: "var(--space-sm)", alignItems: "center" }}>
            <input value={newName} onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addHabit()}
              placeholder="习惯名称..."
              style={{
                flex: 1, padding: "8px 12px", border: "none", outline: "none",
                background: "var(--bg-input)", borderRadius: "var(--radius-sm)",
                fontSize: 14, color: "var(--text-primary)",
              }} />
            <input type="color" value={newColor} onChange={(e) => setNewColor(e.target.value)}
              style={{ width: 32, height: 32, border: "none", borderRadius: "var(--radius-sm)", cursor: "pointer" }} />
            <button className="btn btn-primary" onClick={addHabit} style={{ padding: "8px 16px", fontSize: 13 }}>
              添加
            </button>
          </div>
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            {EMOJI_OPTIONS.map((e) => (
              <button key={e} onClick={() => setNewEmoji(e)}
                style={{
                  width: 32, height: 32, fontSize: 16, display: "flex", alignItems: "center",
                  justifyContent: "center", borderRadius: "var(--radius-sm)", cursor: "pointer",
                  border: newEmoji === e ? "2px solid var(--primary)" : "1px solid transparent",
                  background: newEmoji === e ? "var(--primary-light)" : "var(--bg-hover)",
                }}>
                {e}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Month nav */}
      <div className="glass" style={{ padding: "10px 12px", marginBottom: "var(--space-md)" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "var(--space-sm)" }}>
          <button className="btn btn-ghost" onClick={() => setMonthOffset(monthOffset - 1)}
            style={{ padding: "2px 6px" }}><ChevronLeft size={14} /></button>
          <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>{monthName}</span>
          <button className="btn btn-ghost" onClick={() => setMonthOffset(Math.min(0, monthOffset + 1))}
            disabled={monthOffset >= 0} style={{ padding: "2px 6px" }}><ChevronRight size={14} /></button>
        </div>

        {/* Habit grid */}
        {habits.length === 0 ? (
          <div style={{ textAlign: "center", padding: "var(--space-lg)", color: "var(--text-muted)", fontSize: 13 }}>
            还没有习惯，点击"新习惯"开始吧
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: habits.length > 3 ? 500 : "auto" }}>
              <thead>
                <tr>
                  <th style={{ position: "sticky", left: 0, background: "var(--bg-card)", zIndex: 1, padding: "4px 8px", textAlign: "left", fontSize: 11, color: "var(--text-muted)" }}>
                    日期
                  </th>
                  {habits.map((h) => (
                    <th key={h.id} style={{ padding: "4px 2px", textAlign: "center", fontSize: 11 }}>
                      <span title={h.name}>{h.emoji}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {days.map((date, di) => {
                  const dayNum = di + 1;
                  const isFuture = monthOffset === 0 && dayNum > today.getDate();
                  const isToday = date === todayStr;
                  return (
                    <tr key={date} style={{
                      opacity: isFuture ? 0.3 : 1,
                      background: isToday ? "var(--primary-light)" : "transparent",
                    }}>
                      <td style={{
                        position: "sticky", left: 0, background: isToday ? "var(--primary-light)" : "var(--bg-card)",
                        zIndex: 1, padding: "3px 8px", fontSize: 11, color: "var(--text-secondary)",
                        fontWeight: isToday ? 700 : 400, whiteSpace: "nowrap",
                      }}>
                        {dayNum}日
                      </td>
                      {habits.map((h) => (
                        <td key={h.id} style={{ textAlign: "center", padding: "2px" }}>
                          <button onClick={() => !isFuture && toggleCheckin(h.id, date)}
                            disabled={isFuture}
                            style={{
                              width: 24, height: 24, borderRadius: 4, border: "none",
                              cursor: isFuture ? "default" : "pointer",
                              background: isChecked(h.id, date) ? h.color : "var(--bg-hover)",
                              color: isChecked(h.id, date) ? "white" : "transparent",
                              fontSize: 11, display: "flex", alignItems: "center", justifyContent: "center",
                              transition: "var(--transition-fast)", margin: "0 auto",
                            }}>
                            {isChecked(h.id, date) ? "✓" : ""}
                          </button>
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Habit summary cards */}
      {habits.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)" }}>
          {habits.map((h) => (
            <div key={h.id} className="glass" style={{ padding: "var(--space-md)" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)" }}>
                  <span style={{ fontSize: 20 }}>{h.emoji}</span>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>{h.name}</div>
                    <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 2 }}>
                      <Flame size={12} style={{ color: h.streak > 0 ? "#f59e0b" : "var(--text-muted)" }} />
                      <span style={{ fontSize: 11, color: h.streak > 0 ? "#f59e0b" : "var(--text-muted)", fontWeight: 600 }}>
                        {h.streak > 0 ? `连续 ${h.streak} 天` : "暂无连续"}
                      </span>
                    </div>
                  </div>
                </div>
                <button onClick={() => deleteHabit(h.id)}
                  style={{ padding: 4, color: "var(--text-muted)", background: "none", border: "none", cursor: "pointer" }}>
                  <Trash2 size={12} />
                </button>
              </div>
              <div style={{ marginTop: "var(--space-sm)" }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 2 }}>
                  <span style={{ fontSize: 10, color: "var(--text-muted)" }}>本月完成率</span>
                  <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{completionRate(h.id)}%</span>
                </div>
                <div style={{ height: 4, background: "var(--bg-hover)", borderRadius: 2, overflow: "hidden" }}>
                  <div style={{
                    height: "100%", borderRadius: 2, background: h.color,
                    width: `${completionRate(h.id)}%`, transition: "width 0.3s ease",
                  }} />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
