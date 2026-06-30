import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { Clock, List } from "lucide-react";
import MemoriesPage from "./MemoriesPage";
import TimelinePage from "./TimelinePage";

export default function MemoriesHubPage({ initialView = "list" }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const view = searchParams.get("view") || initialView;
  const isTimeline = view === "timeline";

  useEffect(() => {
    if (initialView === "timeline" && searchParams.get("view") !== "timeline") {
      const next = new URLSearchParams(searchParams);
      next.set("view", "timeline");
      setSearchParams(next, { replace: true });
    }
  }, [initialView, searchParams, setSearchParams]);

  const setView = (nextView) => {
    const next = new URLSearchParams(searchParams);
    if (nextView === "timeline") next.set("view", "timeline");
    else next.delete("view");
    setSearchParams(next);
  };

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      <div className="glass" style={{
        display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4,
        padding: 4, marginBottom: "var(--space-md)",
      }}>
        <button
          className={`btn ${!isTimeline ? "btn-primary" : "btn-ghost"}`}
          onClick={() => setView("list")}
          style={{ justifyContent: "center" }}
        >
          <List size={14} /> 列表/编辑
        </button>
        <button
          className={`btn ${isTimeline ? "btn-primary" : "btn-ghost"}`}
          onClick={() => setView("timeline")}
          style={{ justifyContent: "center" }}
        >
          <Clock size={14} /> 时间线
        </button>
      </div>

      {isTimeline ? <TimelinePage embedded /> : <MemoriesPage />}
    </div>
  );
}
