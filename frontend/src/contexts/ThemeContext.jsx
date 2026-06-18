import { createContext, useContext, useState, useEffect, useCallback } from "react";

const ThemeContext = createContext(null);

const STORAGE_KEY = "mh-theme";

const defaultSettings = {
  mode: "light",          // light | dark
  primaryH: 260,          // hue 0-360
  primaryS: 60,           // saturation %
  primaryL: 65,           // lightness %
  glassBlur: 16,          // px
  bgImage: "",            // url or data-uri
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

  // Apply CSS variables whenever settings change
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", settings.mode);
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
    setSettings((prev) => ({ ...prev, ...patch }));
  }, []);

  const toggleMode = useCallback(() => {
    setSettings((prev) => ({ ...prev, mode: prev.mode === "light" ? "dark" : "light" }));
  }, []);

  const resetTheme = useCallback(() => {
    setSettings(defaultSettings);
  }, []);

  return (
    <ThemeContext.Provider value={{ settings, update, toggleMode, resetTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeContext);
