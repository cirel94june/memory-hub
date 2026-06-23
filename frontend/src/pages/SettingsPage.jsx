import { useState, useEffect } from "react";
import { Save, Wifi, RefreshCw, FlaskConical, Bot, Key, Database } from "lucide-react";

export default function SettingsPage() {
  const [secret, setSecret] = useState(() => localStorage.getItem("mh-secret") || "");
  const [saved, setSaved] = useState(false);
  const [llm, setLlm] = useState(null);
  const [llmModel, setLlmModel] = useState("");
  const [llmLoading, setLlmLoading] = useState(false);
  const [embInfo, setEmbInfo] = useState(null);

  useEffect(() => {
    loadLLM();
    fetch("/api/settings/embedding").then(r => r.json()).then(setEmbInfo).catch(() => {});
  }, []);

  const loadLLM = () => {
    fetch("/api/settings/llm").then(r => r.json()).then(d => {
      setLlm(d);
      setLlmModel(d.model || "");
    }).catch(() => {});
  };

  const save = () => {
    localStorage.setItem("mh-secret", secret);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const testConn = async () => {
    try {
      const res = await fetch("/api/memory/list?per_page=1", {
        headers: { Authorization: "Bearer " + secret },
      });
      const d = await res.json();
      alert(res.ok ? ("✅ 连接成功！共 " + d.total + " 条记忆") : ("❌ 失败: " + (d.detail || res.status)));
    } catch (e) {
      alert("❌ 连接失败: " + e.message);
    }
  };

  const switchModel = async () => {
    if (!llmModel.trim()) return;
    setLlmLoading(true);
    try {
      const res = await fetch("/api/settings/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + secret },
        body: JSON.stringify({ model: llmModel }),
      });
      const d = await res.json();
      alert(res.ok ? ("✅ 已切换到 " + d.model) : ("❌ " + (d.detail || "failed")));
      loadLLM();
    } catch (e) {
      alert("❌ " + e.message);
    }
    setLlmLoading(false);
  };

  const testLLM = async () => {
    setLlmLoading(true);
    try {
      const res = await fetch("/api/settings/llm/test", {
        method: "POST",
        headers: { Authorization: "Bearer " + secret },
      });
      const d = await res.json();
      if (res.ok) {
        alert("✅ 小模型正常！\n模型: " + d.model + "\n耗时: " + d.duration + "\n回复: " + (d.response || "").slice(0, 100));
      } else {
        alert("❌ " + (d.detail || d.error || "test failed"));
      }
    } catch (e) {
      alert("❌ " + e.message);
    }
    setLlmLoading(false);
  };

  return (
    <div style={{ maxWidth: 560, margin: "0 auto" }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: "var(--space-lg)" }}>设置</h2>

      <Section icon={<Key size={16} />} title="API 连接">
        <Field label="HUB_SECRET（记忆管理密钥）" value={secret} onChange={setSecret} type="password" placeholder="填入后才能看到记忆" />
        <div style={{ display: "flex", gap: "var(--space-sm)", marginTop: "var(--space-sm)" }}>
          <button className="btn btn-primary" onClick={save}>
            <Save size={14} /> {saved ? "✅ 已保存" : "保存"}
          </button>
          <button className="btn btn-ghost" onClick={testConn}>
            <Wifi size={14} /> 测试连接
          </button>
        </div>
        <p style={{ fontSize: 11, color: "var(--text-muted)", marginTop: "var(--space-sm)" }}>
          对话页会自动使用每个 AI 在档案页配置的模型和中转站。去 <a href="/app/ai-profiles" style={{ color: "var(--primary)" }}>AI 档案</a> 配置。
        </p>
      </Section>

      <Section icon={<Bot size={16} />} title="小模型（提取 / 分析）">
        <p style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: "var(--space-sm)" }}>
          用于记忆提取、打标、合并分析的后台模型，在 VPS .env 中配置。
        </p>
        {llm ? (
          <>
            <InfoRow label="当前模型" value={llm.model} />
            <InfoRow label="Base URL" value={llm.base_url} />
            <InfoRow label="API Key" value={llm.api_key_set ? "✅ 已设置" : "❌ 未设置"} />
            <div style={{ marginTop: "var(--space-md)" }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4, display: "block" }}>切换模型</label>
              <div style={{ display: "flex", gap: "var(--space-sm)" }}>
                <input value={llmModel} onChange={(e) => setLlmModel(e.target.value)}
                  style={{
                    flex: 1, padding: "8px 12px", background: "var(--bg-input)",
                    border: "1px solid var(--glass-border)", borderRadius: "var(--radius-sm)",
                    fontSize: 13, color: "var(--text-primary)", outline: "none", fontFamily: "monospace",
                  }} />
                <button className="btn btn-primary" onClick={switchModel} disabled={llmLoading}
                  style={{ whiteSpace: "nowrap" }}>
                  <RefreshCw size={14} /> 切换
                </button>
              </div>
              <div style={{ display: "flex", gap: "var(--space-xs)", flexWrap: "wrap", marginTop: "var(--space-sm)" }}>
                {["claude-haiku-4-5-20241022", "deepseek-chat", "gemini-2.0-flash"].map(m => (
                  <button key={m} className="btn btn-ghost" onClick={() => setLlmModel(m)}
                    style={{ padding: "2px 8px", fontSize: 11, fontFamily: "monospace" }}>{m}</button>
                ))}
              </div>
            </div>
            <button className="btn btn-ghost" onClick={testLLM} disabled={llmLoading}
              style={{ marginTop: "var(--space-md)" }}>
              <FlaskConical size={14} /> 测试小模型
            </button>
          </>
        ) : (
          <p style={{ color: "var(--text-muted)", fontSize: 13 }}>加载中...</p>
        )}
      </Section>

      <Section icon={<Database size={16} />} title="Embedding 模型">
        <p style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: "var(--space-sm)" }}>
          用于记忆向量搜索，在 VPS .env 中配置。
        </p>
        {embInfo ? (
          <>
            <InfoRow label="模型" value={embInfo.model} />
            <InfoRow label="维度" value={embInfo.dim} />
            <InfoRow label="API" value={embInfo.base_url} />
            <InfoRow label="Key" value={embInfo.api_key_set ? "✅ 已设置" : "❌ 未设置"} />
          </>
        ) : (
          <p style={{ color: "var(--text-muted)", fontSize: 13 }}>加载中...</p>
        )}
      </Section>

      <div style={{ textAlign: "center", marginTop: "var(--space-lg)" }}>
        <a href="/static/index.html" target="_blank" rel="noopener"
          style={{ fontSize: 12, color: "var(--text-muted)" }}>
          打开旧版管理页面（含完整日志） ↗
        </a>
      </div>
    </div>
  );
}

function Section({ icon, title, children }) {
  return (
    <div className="glass" style={{ padding: "var(--space-lg)", marginBottom: "var(--space-md)" }}>
      <h3 style={{ fontSize: 14, fontWeight: 600, color: "var(--text-secondary)", marginBottom: "var(--space-md)", display: "flex", alignItems: "center", gap: 6 }}>
        {icon} {title}
      </h3>
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-sm)" }}>
        {children}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, type = "text", placeholder }) {
  return (
    <div>
      <label style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4, display: "block" }}>{label}</label>
      <input value={value} onChange={(e) => onChange(e.target.value)} type={type} placeholder={placeholder}
        style={{
          width: "100%", padding: "8px 12px",
          background: "var(--bg-input)", border: "1px solid var(--glass-border)",
          borderRadius: "var(--radius-sm)", fontSize: 13, color: "var(--text-primary)", outline: "none",
        }} />
    </div>
  );
}

function InfoRow({ label, value }) {
  return (
    <div style={{
      display: "flex", justifyContent: "space-between", padding: "6px 0",
      borderBottom: "1px solid var(--glass-border)", fontSize: 13,
    }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span style={{ color: "var(--text-primary)", fontFamily: "monospace", fontSize: 12, maxWidth: "60%", textAlign: "right", wordBreak: "break-all" }}>{String(value)}</span>
    </div>
  );
}
