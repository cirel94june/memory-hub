import { useTheme, PRESETS } from "../contexts/ThemeContext";
import { Sun, Moon, RotateCcw, Image, Check } from "lucide-react";
import { useRef } from "react";

export default function ThemePage() {
  const { settings, update, toggleMode, resetTheme, applyPreset } = useTheme();
  const fileRef = useRef(null);
  const isDark = settings.mode === "dark";

  const handleBgImage = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => update({ bgImage: ev.target.result });
    reader.readAsDataURL(file);
  };

  return (
    <div style={{ maxWidth: 560, margin: "0 auto" }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: "var(--space-lg)" }}>主题定制</h2>

      {/* 深色模式切换 */}
      <Section title="模式">
        <div style={{ display: "flex", gap: "var(--space-sm)" }}>
          <button className={`btn ${!isDark ? "btn-primary" : "btn-ghost"}`}
            onClick={() => update({ mode: "light" })}>
            <Sun size={16} /> 浅色
          </button>
          <button className={`btn ${isDark ? "btn-primary" : "btn-ghost"}`}
            onClick={() => update({ mode: "dark" })}>
            <Moon size={16} /> 深色
          </button>
        </div>
      </Section>

      {/* 主题预设 */}
      <Section title="预设主题">
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-sm)" }}>
          {Object.entries(PRESETS).map(([key, preset]) => {
            const active = settings.preset === key;
            const colors = isDark ? preset.previewDark : preset.preview;
            return (
              <button key={key} onClick={() => applyPreset(key)}
                style={{
                  position: "relative",
                  padding: 0, border: "2px solid",
                  borderColor: active ? "var(--primary)" : "var(--glass-border)",
                  borderRadius: "var(--radius-md)",
                  cursor: "pointer",
                  overflow: "hidden",
                  background: colors.bg,
                  transition: "border-color var(--transition-fast), transform var(--transition-fast)",
                  transform: active ? "scale(1.02)" : "none",
                }}>
                {/* Mini preview */}
                <div style={{ padding: "12px 10px 8px" }}>
                  {/* Fake sidebar + content */}
                  <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                    <div style={{
                      width: 8, borderRadius: 3,
                      background: colors.accent, opacity: 0.6,
                    }} />
                    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 4 }}>
                      <div style={{
                        height: 16, borderRadius: 4,
                        background: colors.card,
                        border: `1px solid ${colors.accent}22`,
                      }} />
                      <div style={{
                        height: 10, borderRadius: 3,
                        background: colors.card,
                        width: "75%",
                        border: `1px solid ${colors.accent}11`,
                      }} />
                    </div>
                  </div>
                  {/* Accent bar */}
                  <div style={{
                    height: 3, borderRadius: 2,
                    background: colors.accent,
                    width: "60%",
                  }} />
                </div>
                {/* Label */}
                <div style={{
                  padding: "6px 10px",
                  background: colors.card,
                  borderTop: `1px solid ${colors.accent}15`,
                  textAlign: "left",
                }}>
                  <div style={{ fontSize: 12, fontWeight: 600, color: colors.accent }}>
                    {preset.name}
                  </div>
                  <div style={{ fontSize: 10, color: colors.accent, opacity: 0.6, marginTop: 1 }}>
                    {preset.desc}
                  </div>
                </div>
                {/* Active check */}
                {active && (
                  <div style={{
                    position: "absolute", top: 6, right: 6,
                    width: 18, height: 18, borderRadius: "50%",
                    background: "var(--primary)", display: "flex",
                    alignItems: "center", justifyContent: "center",
                  }}>
                    <Check size={11} color="#fff" strokeWidth={3} />
                  </div>
                )}
              </button>
            );
          })}
        </div>
      </Section>

      {/* 自定义色调 */}
      <Section title="自定义色调">
        <div style={{ display: "flex", gap: "var(--space-md)", alignItems: "center", flexWrap: "wrap" }}>
          {[
            { h: 260, s: 60, l: 65, name: "薰衣草" },
            { h: 340, s: 50, l: 62, name: "玫瑰" },
            { h: 25, s: 70, l: 60, name: "蜜桃" },
            { h: 210, s: 40, l: 58, name: "雾蓝" },
            { h: 160, s: 45, l: 50, name: "薄荷" },
            { h: 0, s: 0, l: 50, name: "灰调" },
          ].map(({ h, s, l, name }) => (
            <button key={name} onClick={() => update({ primaryH: h, primaryS: s, primaryL: l })}
              title={name}
              style={{
                width: 32, height: 32, borderRadius: "50%", border: "2px solid",
                borderColor: settings.primaryH === h && settings.primaryS === s ? "var(--text-primary)" : "transparent",
                background: `hsl(${h}, ${s}%, ${l}%)`, cursor: "pointer",
                transition: "border-color var(--transition-fast)",
              }} />
          ))}
        </div>
        <SliderRow label="色相" value={settings.primaryH} min={0} max={360}
          onChange={(v) => update({ primaryH: v })}
          preview={`hsl(${settings.primaryH}, ${settings.primaryS}%, ${settings.primaryL}%)`} />
        <SliderRow label="饱和度" value={settings.primaryS} min={0} max={100}
          onChange={(v) => update({ primaryS: v })} />
        <SliderRow label="亮度" value={settings.primaryL} min={20} max={85}
          onChange={(v) => update({ primaryL: v })} />
      </Section>

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
            <Image size={16} /> 选择图片
          </button>
          {settings.bgImage && (
            <button className="btn btn-ghost" onClick={() => update({ bgImage: "" })}>
              清除
            </button>
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
          <RotateCcw size={14} /> 恢复默认主题
        </button>
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="glass" style={{ padding: "var(--space-lg)", marginBottom: "var(--space-md)" }}>
      <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: "var(--space-md)", color: "var(--text-secondary)" }}>
        {title}
      </h3>
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}>
        {children}
      </div>
    </div>
  );
}

function SliderRow({ label, value, min, max, unit = "", onChange, preview }) {
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4, fontSize: 12, color: "var(--text-secondary)" }}>
        <span>{label}</span>
        <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
          {preview && <span style={{ width: 14, height: 14, borderRadius: 3, background: preview, display: "inline-block" }} />}
          {value}{unit}
        </span>
      </div>
      <input type="range" min={min} max={max} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: "100%", accentColor: "var(--primary)" }} />
    </div>
  );
}
