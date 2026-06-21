import { createContext, useContext, useState, useEffect, useCallback } from "react";

const ThemeContext = createContext(null);

const STORAGE_KEY = "mh-theme";

export const PRESETS = {
  moonlight: {
    name: "月光紫",
    desc: "温柔薰衣草，夜空下的安宁",
    primaryH: 260, primaryS: 60, primaryL: 65, glassBlur: 16,
    preview: { bg: "#f5f0ff", card: "#ece5ff", accent: "#9b7fd4" },
    previewDark: { bg: "#1a1625", card: "#28234a", accent: "#b49be8" },
  },
  rose: {
    name: "玫瑰金属",
    desc: "暖粉蜜色，复古优雅",
    primaryH: 340, primaryS: 50, primaryL: 62, glassBlur: 14,
    preview: { bg: "#fff5f5", card: "#ffe8ec", accent: "#c97088" },
    previewDark: { bg: "#1f1518", card: "#2d1f25", accent: "#d4879a" },
  },
  candy: {
    name: "童话糖纸",
    desc: "暖橘蜜桃，甜甜的少女心",
    primaryH: 25, primaryS: 70, primaryL: 60, glassBlur: 12,
    preview: { bg: "#fff8f0", card: "#fff0e0", accent: "#d4914a" },
    previewDark: { bg: "#1d1815", card: "#2a2220", accent: "#e0a060" },
  },
  misty: {
    name: "雾蓝纸笺",
    desc: "静谧蓝灰，信纸般的质感",
    primaryH: 210, primaryS: 40, primaryL: 58, glassBlur: 18,
    preview: { bg: "#f0f4f8", card: "#e4eaf0", accent: "#6889a8" },
    previewDark: { bg: "#151a1f", card: "#1e252d", accent: "#7a9ab8" },
  },
};

const defaultSettings = {
  mode: "light",
  preset: "moonlight",
  primaryH: 260,
  primaryS: 60,
  primaryL: 65,
  glassBlur: 16,
  bgImage: "",
  bgImageOpacity: 0.3,
};

export function ThemeProvider({ children }) {
  const [settings, setSettings] = useState(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return saved ? { ...defaultSettings, ...JSON.parse(saved) } : defaultSettings;
    } catch {
      return defaultSettings;
    }
  });

  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", settings.mode);
    root.setAttribute("data-preset", settings.preset || "custom");
    root.style.setProperty("--primary-h", settings.primaryH);
    root.style.setProperty("--primary-s", settings.primaryS + "%");
    root.style.setProperty("--primary-l", settings.primaryL + "%");
    root.style.setProperty("--glass-blur", settings.glassBlur + "px");

    if (settings.bgImage) {
      document.body.style.setProperty("--bg-image", `url(${settings.bgImage})`);
      document.body.style.setProperty("--bg-image-opacity", settings.bgImageOpacity);
    } else {
      document.body.style.setProperty("--bg-image-opacity", "0");
    }

    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  }, [settings]);

  const update = useCallback((patch) => {
    setSettings((prev) => {
      const next = { ...prev, ...patch };
      if ("primaryH" in patch || "primaryS" in patch || "primaryL" in patch || "glassBlur" in patch) {
        const matchedPreset = Object.entries(PRESETS).find(([, p]) =>
          p.primaryH === next.primaryH && p.primaryS === next.primaryS &&
          p.primaryL === next.primaryL && p.glassBlur === next.glassBlur
        );
        next.preset = matchedPreset ? matchedPreset[0] : "custom";
      }
      return next;
    });
  }, []);

  const applyPreset = useCallback((name) => {
    const p = PRESETS[name];
    if (!p) return;
    setSettings((prev) => ({
      ...prev,
      preset: name,
      primaryH: p.primaryH,
      primaryS: p.primaryS,
      primaryL: p.primaryL,
      glassBlur: p.glassBlur,
    }));
  }, []);

  const toggleMode = useCallback(() => {
    setSettings((prev) => ({ ...prev, mode: prev.mode === "light" ? "dark" : "light" }));
  }, []);

  const resetTheme = useCallback(() => {
    setSettings(defaultSettings);
  }, []);

  return (
    <ThemeContext.Provider value={{ settings, update, toggleMode, resetTheme, applyPreset }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeContext);
