import { useTheme, PRESETS } from "../contexts/ThemeContext";
import { Sun, Moon, RotateCcw, Image, Check } from "lucide-react";
import { useRef, useState } from "react";

const COLOR_ROWS = [
  ["accent", "强调色", "按钮 / 链接 / 重要标记"],
  ["rose",   "情感色", "feel / 温度感 / 情绪"],
  ["gold",   "点缀色", "高亮 / 重要度"],
  ["bg",     "页面底色", "页面背景"],
  ["paper",  "卡片色", "卡片 / 模态框纸面"],
  ["ink",    "文本色", "主文字深色"],
];

export default function ThemePage() {
  const { settings, update, toggleMode, resetTheme, applyPreset, applyCustomColors } = useTheme();
  const fileRef = useRef(null);
  const isDark = settings.mode === "dark";
  const [customOpen, setCustomOpen] = useState(false);
  const [draft, setDraft] = useState(null);

  const handleBgImage = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => update({ bgImage: ev.target.result });
    reader.readAsDataURL(file);
  };

  const openCustom = () => {
    const current = settings.preset === "custom" && settings.custom
      ? settings.custom
      : (PRESETS[settings.preset] || PRESETS.moonlight);
    setDraft({ ...current });
    setCustomOpen(true);
  };

  const applyDraft = () => {
    if (draft) applyCustomColors(draft);
    setCustomOpen(false);
  };

  return (
    <div style={{ maxWidth: 560, margin: "0 auto" }} className="anim-pop">
      <h2 style={{ fontSize: 22, fontWeight: 600, marginBottom: "var(--space-lg)", fontStyle: "italic" }}>
        主题定制
      </h2>

      {/* 模式切换 */}
      <Section title="模式">
        <div style={{ display: "flex", gap: "var(--space-sm)" }}>
          <button className={`btn ${!isDark ? "btn-primary" : "btn-ghost"}`}
            onClick={() => update({ mode: "light" })}>
            <Sun size={15} /> 浅色
          </button>
          <button className={`btn ${isDark ? "btn-primary" : "btn-ghost"}`}
            onClick={() => update({ mode: "dark" })}>
            <Moon size={15} /> 深色
          </button>
        </div>
      </Section>

      {/* 预设主题 */}
      <Section title="预设主题">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          {Object.entries(PRESETS).map(([key, preset]) => {
            const active = settings.preset === key;
            return (
              <PresetCard key={key} name={key} preset={preset}
                active={active} isDark={isDark}
                onClick={() => applyPreset(key)} />
            );
          })}
        </div>
        {/* 自定义入口 */}
        <button className="btn btn-ghost" onClick={openCustom}
          style={{ marginTop: "var(--space-sm)", width: "100%", justifyContent: "center", fontSize: 12 }}>
          <span style={{
            width: 14, height: 14, borderRadius: "50%",
            background: "conic-gradient(from 0deg, #6e4f9a, #d291b3, #d4a85f, #6e4f9a)",
          }} />
          自定义配色
        </button>
      </Section>

      {/* 自定义色板抽屉 */}
      {customOpen && draft && (
        <Section title="自定义配色">
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8 }}>
            调整 6 种颜色 · 实时预览
          </div>
          {COLOR_ROWS.map(([key, label, hint]) => (
            <div key={key} style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "6px 0", borderBottom: "0.5px solid var(--glass-border)",
            }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>{label}</div>
                <div style={{ fontSize: 10, color: "var(--text-muted)" }}>{hint}</div>
              </div>
              <input type="color" value={draft[key] || "#888888"}
                onChange={(e) => {
                  const next = { ...draft, [key]: e.target.value };
                  setDraft(next);
                  applyCustomColors(next);
                }}
                style={{
                  width: 28, height: 28, border: "none", borderRadius: 6,
                  cursor: "pointer", background: "transparent",
                }} />
              <span style={{ fontSize: 10, fontFamily: "var(--mono)", color: "var(--text-muted)", width: 58 }}>
                {(draft[key] || "").toUpperCase()}
              </span>
            </div>
          ))}
          <div style={{ display: "flex", gap: 8, marginTop: "var(--space-md)", justifyContent: "flex-end" }}>
            <button className="btn btn-ghost" onClick={() => { setCustomOpen(false); applyPreset(settings.preset !== "custom" ? settings.preset : "moonlight"); }}>
              取消
            </button>
            <button className="btn btn-primary" onClick={applyDraft}>应用</button>
          </div>
        </Section>
      )}

      {/* 玻璃模糊 */}
      <Section title="玻璃效果">
        <SliderRow label="模糊度" value={settings.glassBlur} min={0} max={40} unit="px"
          onChange={(v) => update({ glassBlur: v })} />
      </Section>

      {/* 背景图 */}
      <Section title="背景图片">
        <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center" }}>
          <input ref={fileRef} type="file" accept="image/*" onChange={handleBgImage}
            style={{ display: "none" }} />
          <button className="btn btn-ghost" onClick={() => fileRef.current?.click()}>
            <Image size={15} /> 选择图片
          </button>
          {settings.bgImage && (
            <button className="btn btn-ghost" onClick={() => update({ bgImage: "" })}>清除</button>
          )}
        </div>
        {settings.bgImage && (
          <SliderRow label="透明度" value={Math.round(settings.bgImageOpacity * 100)}
            min={5} max={80} unit="%"
            onChange={(v) => update({ bgImageOpacity: v / 100 })} />
        )}
      </Section>

      {/* 重置 */}
      <div style={{ marginTop: "var(--space-xl)", textAlign: "center" }}>
        <button className="btn btn-ghost" onClick={resetTheme}>
          <RotateCcw size={13} /> 恢复默认
        </button>
      </div>
    </div>
  );
}

function PresetCard({ name, preset, active, isDark, onClick }) {
  const c = preset;
  return (
    <button onClick={onClick} style={{
      position: "relative",
      padding: 0, border: active ? "1.5px solid var(--accent)" : "0.5px solid var(--glass-border)",
      borderRadius: "var(--radius-md)",
      cursor: "pointer",
      overflow: "hidden",
      background: isDark ? "#1d1c27" : c.bg,
      transition: "all 0.22s var(--ease-pop)",
      transform: active ? "scale(1.02)" : "none",
    }}>
      {/* 6-color swatch strip */}
      <div style={{ display: "flex", height: 6 }}>
        {[c.accent, c.rose, c.gold, c.bg, c.paper, c.ink].map((color, i) => (
          <div key={i} style={{ flex: 1, background: color }} />
        ))}
      </div>
      {/* Mini preview */}
      <div style={{ padding: "10px 10px 6px", display: "flex", gap: 5 }}>
        <div style={{ width: 6, borderRadius: 3, background: c.accent, opacity: 0.5 }} />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 3 }}>
          <div style={{ height: 14, borderRadius: 4, background: c.paper, border: `0.5px solid ${c.accent}22` }} />
          <div style={{ height: 8, borderRadius: 3, background: c.paper, width: "65%", border: `0.5px solid ${c.accent}11` }} />
          <div style={{ height: 2, borderRadius: 1, background: c.accent, width: "50%", marginTop: 2 }} />
        </div>
      </div>
      {/* Label */}
      <div style={{
        padding: "5px 10px 7px",
        background: isDark ? "#14131c" : c.paper,
        borderTop: `0.5px solid ${c.accent}10`,
        textAlign: "left",
      }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: isDark ? "#ece9f2" : c.ink, fontFamily: "var(--serif)", fontStyle: "italic" }}>
          {c.name}
        </div>
        <div style={{ fontSize: 9, color: isDark ? "#a78bd0" : c.accent, opacity: 0.7, marginTop: 1 }}>
          {c.desc}
        </div>
      </div>
      {active && (
        <div style={{
          position: "absolute", top: 10, right: 6,
          width: 16, height: 16, borderRadius: "50%",
          background: "var(--accent)", display: "flex",
          alignItems: "center", justifyContent: "center",
          boxShadow: "0 0 0 1.5px var(--bg-card), 0 0 0 2.5px var(--accent)",
        }}>
          <Check size={9} color="#fff" strokeWidth={3} />
        </div>
      )}
    </button>
  );
}

function Section({ title, children }) {
  return (
    <div className="glass" style={{ padding: "var(--space-lg)", marginBottom: 10 }}>
      <h3 style={{
        fontSize: 13, fontWeight: 600, marginBottom: "var(--space-md)",
        color: "var(--text-secondary)", fontFamily: "var(--sans)", fontStyle: "normal",
        letterSpacing: "0.02em",
      }}>
        {title}
      </h3>
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
        {children}
      </div>
    </div>
  );
}

function SliderRow({ label, value, min, max, unit = "", onChange }) {
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3, fontSize: 11, color: "var(--text-secondary)" }}>
        <span>{label}</span>
        <span style={{ fontFamily: "var(--mono)", fontSize: 10 }}>{value}{unit}</span>
      </div>
      <input type="range" min={min} max={max} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: "100%", accentColor: "var(--accent)" }} />
    </div>
  );
}
