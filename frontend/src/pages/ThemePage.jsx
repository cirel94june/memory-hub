import { useTheme } from "../contexts/ThemeContext";
import { Sun, Moon, RotateCcw, Image } from "lucide-react";
import { useRef } from "react";

export default function ThemePage() {
  const { settings, update, toggleMode, resetTheme } = useTheme();
  const fileRef = useRef(null);

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
          <button className={`btn ${settings.mode === "light" ? "btn-primary" : "btn-ghost"}`}
            onClick={() => update({ mode: "light" })}>
            <Sun size={16} /> 浅色
          </button>
          <button className={`btn ${settings.mode === "dark" ? "btn-primary" : "btn-ghost"}`}
            onClick={() => update({ mode: "dark" })}>
            <Moon size={16} /> 深色
          </button>
        </div>
      </Section>

      {/* 主色调 */}
      <Section title="主色调">
        <div style={{ display: "flex", gap: "var(--space-md)", alignItems: "center", flexWrap: "wrap" }}>
          {/* 预设色 */}
          {[
            { h: 260, s: 60, l: 65, name: "奶油紫" },
            { h: 340, s: 55, l: 65, name: "樱花粉" },
            { h: 200, s: 60, l: 55, name: "天空蓝" },
            { h: 160, s: 45, l: 50, name: "薄荷绿" },
            { h: 25, s: 70, l: 60, name: "暖橘" },
            { h: 0, s: 0, l: 50, name: "灰调" },
          ].map(({ h, s, l, name }) => (
            <button key={name} onClick={() => update({ primaryH: h, primaryS: s, primaryL: l })}
              title={name}
              style={{
                width: 36, height: 36, borderRadius: "50%", border: "2px solid",
                borderColor: settings.primaryH === h ? "var(--text-primary)" : "transparent",
                background: `hsl(${h}, ${s}%, ${l}%)`, cursor: "pointer",
                transition: "border-color var(--transition-fast)",
              }} />
          ))}
        </div>
        <SliderRow label="色相 (Hue)" value={settings.primaryH} min={0} max={360}
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
