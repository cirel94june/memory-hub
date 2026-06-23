import { createContext, useContext, useState, useEffect, useCallback } from "react";

const AIContext = createContext(null);

const DEFAULT_PROFILES = [
  { ai_id: "cloudy", name: "小克", emoji: "🐱", color: "#6e4f9a", greeting: "", persona: "" },
  { ai_id: "lucien", name: "Lucien", emoji: "🦊", color: "hsl(30, 60%, 55%)", greeting: "", persona: "" },
  { ai_id: "jasper", name: "Jasper", emoji: "🦜", color: "hsl(160, 50%, 45%)", greeting: "", persona: "" },
];

export function AIProvider({ children }) {
  const [profiles, setProfiles] = useState(DEFAULT_PROFILES);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(() => {
    const secret = localStorage.getItem("mh-secret") || "";
    fetch("/api/ai-profiles", { headers: { Authorization: `Bearer ${secret}` } })
      .then((r) => r.json())
      .then((d) => {
        if (d.profiles?.length) setProfiles(d.profiles);
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  useEffect(() => { load(); }, [load]);

  const [aliases, setAliases] = useState({});

  const loadAliases = useCallback(() => {
    const secret = localStorage.getItem("mh-secret") || "";
    fetch("/api/ai-aliases", { headers: { Authorization: `Bearer ${secret}` } })
      .then((r) => r.json())
      .then((d) => { if (d.aliases) setAliases(d.aliases); })
      .catch(() => {});
  }, []);

  useEffect(() => { loadAliases(); }, [loadAliases]);

  const getAI = useCallback((id) => {
    if (!id) return null;
    const direct = profiles.find((p) => p.ai_id === id);
    if (direct) return direct;
    const canonical = aliases[id];
    if (canonical) {
      const aliased = profiles.find((p) => p.ai_id === canonical);
      if (aliased) return aliased;
    }
    return { ai_id: id, name: id, emoji: "🤖", color: "#888" };
  }, [profiles, aliases]);

  const aiLabel = useCallback((id) => {
    const p = getAI(id);
    return p ? `${p.emoji} ${p.name}` : id;
  }, [getAI]);

  const aiLabelMap = {};
  for (const p of profiles) {
    aiLabelMap[p.ai_id] = p.name;
  }

  return (
    <AIContext.Provider value={{ profiles, loaded, reload: load, getAI, aiLabel, aiLabelMap }}>
      {children}
    </AIContext.Provider>
  );
}

export const useAI = () => useContext(AIContext);
