import { createContext, useContext, useState, useEffect, useCallback } from "react";

const ThemeContext = createContext(null);
const STORAGE_KEY = "mh-theme";

function hexToRgba(hex, alpha) {
  const m = String(hex || "").replace("#", "");
  if (m.length !== 6) return `rgba(110,79,154,${alpha})`;
  const r = parseInt(m.slice(0, 2), 16);
  const g = parseInt(m.slice(2, 4), 16);
  const b = parseInt(m.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function shiftHex(hex, delta) {
  const m = String(hex || "").replace("#", "");
  if (m.length !== 6) return hex;
  const clamp = (v) => Math.max(0, Math.min(255, v));
  const r = clamp(parseInt(m.slice(0, 2), 16) + delta);
  const g = clamp(parseInt(m.slice(2, 4), 16) + delta);
  const b = clamp(parseInt(m.slice(4, 6), 16) + delta);
  const h = (n) => n.toString(16).padStart(2, "0");
  return `#${h(r)}${h(g)}${h(b)}`;
}

function hexToHue(hex) {
  const m = String(hex || "").replace("#", "");
  if (m.length !== 6) return 270;
  const r = parseInt(m.slice(0, 2), 16) / 255;
  const g = parseInt(m.slice(2, 4), 16) / 255;
  const b = parseInt(m.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max === min) return 270;
  let h = 0;
  if (max === r) h = (g - b) / (max - min);
  else if (max === g) h = 2 + (b - r) / (max - min);
  else h = 4 + (r - g) / (max - min);
  return Math.round((h * 60 + 360) % 360);
}

export const PRESETS = {
  moonlight: {
    name: "月光紫", desc: "冷紫 · 略带粉气",
    accent: "#6e4f9a", rose: "#d291b3", gold: "#d4a85f",
    bg: "#f4f3f7", paper: "#ffffff", ink: "#1a1922",
  },
  rose: {
    name: "玫瑰金属", desc: "浅玫粉 · 玫瑰金",
    accent: "#5a3a52", rose: "#c98a85", gold: "#b87a6a",
    bg: "#f4e4e1", paper: "#faeeea", ink: "#3a2530",
  },
  candy: {
    name: "童话糖纸", desc: "奶油底 · 粉紫 · 天青",
    accent: "#c7bce6", rose: "#eec9ea", gold: "#b0e8f9",
    bg: "#fffeec", paper: "#ffffff", ink: "#4e416f",
  },
  misty: {
    name: "雾蓝纸笺", desc: "烟蓝 · 浅紫 · 深蓝",
    accent: "#8696bc", rose: "#d3bdd4", gold: "#646b9c",
    bg: "#f4f3f7", paper: "#ffffff", ink: "#3d4a6b",
  },
};

const DARK_COLORS = {
  accent: "#a78bd0", rose: "#e0a3c4", gold: "#a78bd0",
  bg: "#14131c", paper: "#1d1c27", ink: "#ece9f2",
};

function applyColors(root, c, isDark) {
  root.style.setProperty("--accent", c.accent);
  root.style.setProperty("--rose", c.rose);
  root.style.setProperty("--gold", c.gold);

  // backward compat aliases
  root.style.setProperty("--primary", c.accent);
  root.style.setProperty("--primary-h", hexToHue(c.accent));
  root.style.setProperty("--primary-light", hexToRgba(c.accent, isDark ? 0.24 : 0.18));
  root.style.setProperty("--primary-dark", shiftHex(c.accent, -40));

  // backgrounds
  root.style.setProperty("--bg-base", c.bg);
  root.style.setProperty("--bg-card", isDark ? hexToRgba(c.paper, 0.72) : hexToRgba(c.paper, 0.85));
  root.style.setProperty("--bg-sidebar", isDark ? hexToRgba(c.paper, 0.6) : hexToRgba(c.paper, 0.7));
  root.style.setProperty("--bg-input", isDark ? hexToRgba(c.paper, 0.5) : hexToRgba(c.paper, 0.8));
  root.style.setProperty("--bg-hover", isDark ? hexToRgba(c.ink, 0.10) : hexToRgba(c.ink, 0.07));

  // text hierarchy from ink
  root.style.setProperty("--text-primary", c.ink);
  root.style.setProperty("--text-secondary", isDark ? shiftHex(c.ink, -45) : shiftHex(c.ink, 55));
  root.style.setProperty("--text-muted", isDark ? shiftHex(c.ink, -95) : shiftHex(c.ink, 95));
  root.style.setProperty("--text-on-primary", "#fff");

  // glass
  root.style.setProperty("--glass-border", isDark
    ? hexToRgba(c.ink, 0.14) : hexToRgba(c.ink, 0.10));
  root.style.setProperty("--glass-shadow", isDark
    ? `0 8px 32px ${hexToRgba("#000", 0.25)}` : `0 2px 8px ${hexToRgba(c.ink, 0.04)}`);

  // accent alpha variants (for glows, highlights)
  [0.06, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40].forEach((a) => {
    const suffix = String(Math.round(a * 100)).padStart(2, "0");
    root.style.setProperty(`--accent-a${suffix}`, hexToRgba(c.accent, a));
  });

  // rose alpha variants
  [0.08, 0.12, 0.25, 0.45].forEach((a) => {
    const suffix = String(Math.round(a * 100)).padStart(2, "0");
    root.style.setProperty(`--rose-a${suffix}`, hexToRgba(c.rose, a));
  });

  // meta theme-color
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = c.bg;
}

const defaultSettings = {
  mode: "light",
  preset: "moonlight",
  glassBlur: 16,
  bgImage: "",
  bgImageOpacity: 0.3,
  custom: null,
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
    const isDark = settings.mode === "dark";
    root.setAttribute("data-theme", isDark ? "dark" : "light");
    root.setAttribute("data-preset", settings.preset || "custom");

    const colors = isDark
      ? DARK_COLORS
      : settings.preset === "custom" && settings.custom
        ? settings.custom
        : (PRESETS[settings.preset] || PRESETS.moonlight);

    applyColors(root, colors, isDark);

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
    setSettings((prev) => ({ ...prev, ...patch }));
  }, []);

  const applyPreset = useCallback((name) => {
    if (!PRESETS[name]) return;
    setSettings((prev) => ({ ...prev, preset: name }));
  }, []);

  const applyCustomColors = useCallback((colors) => {
    setSettings((prev) => ({ ...prev, preset: "custom", custom: colors }));
  }, []);

  const toggleMode = useCallback(() => {
    setSettings((prev) => ({ ...prev, mode: prev.mode === "light" ? "dark" : "light" }));
  }, []);

  const resetTheme = useCallback(() => {
    setSettings(defaultSettings);
  }, []);

  return (
    <ThemeContext.Provider value={{ settings, update, toggleMode, resetTheme, applyPreset, applyCustomColors, DARK_COLORS }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeContext);
