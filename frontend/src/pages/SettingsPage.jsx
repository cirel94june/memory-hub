import { useState, useEffect } from "react";
import { Save, Wifi, RefreshCw, FlaskConical, Bot, Key, Database, Activity } from "lucide-react";

export default function SettingsPage() {
  const [secret, setSecret] = useState(() => localStorage.getItem("mh-secret") || "");
  const [saved, setSaved] = useState(false);
  const [llm, setLlm] = useState(null);
  const [llmModel, setLlmModel] = useState("");
  const [llmBaseUrl, setLlmBaseUrl] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmLoading, setLlmLoading] = useState(false);
  const [embInfo, setEmbInfo] = useState(null);
  const [daemonStatus, setDaemonStatus] = useState(null);

  useEffect(() => {
    loadLLM();
    loadEmbedding();
    loadDaemonStatus();
  }, []);

  const loadLLM = () => {
    fetch("/api/settings/llm", { headers: { Authorization: "Bearer " + secret } }).then(r => r.json()).then(d => {
      setLlm(d);
      setLlmModel(d.current?.llm_model || "");
      setLlmBaseUrl(d.current?.llm_base_url || "");
    }).catch(() => {});
  };

  const loadEmbedding = () => {
    fetch("/api/daemon/test-llm", { headers: { Authorization: "Bearer " + secret } })
      .then(r => r.json())
      .then(setEmbInfo)
      .catch(() => {});
  };

  const loadDaemonStatus = () => {
    fetch("/api/daemon/status", { headers: { Authorization: "Bearer " + secret } })
      .then(r => r.json())
      .then(setDaemonStatus)
      .catch(() => {});
  };

  const save = () => {
    localStorage.setItem("mh-secret", secret);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    loadLLM();
    loadEmbedding();
    loadDaemonStatus();
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

  const saveLLMSettings = async () => {
    if (!llmModel.trim() && !llmBaseUrl.trim() && !llmApiKey.trim()) return;
    setLlmLoading(true);
    try {
      const res = await fetch("/api/settings/llm", {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + secret },
        body: JSON.stringify({
          llm_base_url: llmBaseUrl,
          llm_model: llmModel,
          llm_api_key: llmApiKey,
        }),
      });
      const d = await res.json();
      alert(res.ok ? ("✅ 已保存小模型设置") : ("❌ " + (d.detail || "failed")));
      if (res.ok) setLlmApiKey("");
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
        alert("✅ 小模型正常！\n模型: " + d.model + "\n耗时: " + d.duration_ms + "ms\n回复: " + (d.response || "").slice(0, 100));
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
            <InfoRow label="当前模型" value={llm.current?.llm_model || "未设置"} />
            <InfoRow label="Base URL" value={llm.current?.llm_base_url || "未设置"} />
            <InfoRow label="API Key" value={llm.current?.llm_api_key_set ? "✅ 已设置" : "❌ 未设置"} />
            <InfoRow label="来源" value={llm.is_overridden ? "运行中覆盖" : ".env 默认"} />
            <div style={{ marginTop: "var(--space-md)" }}>
              <Field label="Base URL" value={llmBaseUrl} onChange={setLlmBaseUrl} placeholder="留空则使用 .env 默认值" />
              <Field label="API Key（留空不覆盖）" value={llmApiKey} onChange={setLlmApiKey} type="password" placeholder="sk-..." />
              <label style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4, display: "block" }}>模型名称</label>
              <div style={{ display: "flex", gap: "var(--space-sm)" }}>
                <input value={llmModel} onChange={(e) => setLlmModel(e.target.value)}
                  style={{
                    flex: 1, padding: "8px 12px", background: "var(--bg-input)",
                    border: "1px solid var(--glass-border)", borderRadius: "var(--radius-sm)",
                    fontSize: 13, color: "var(--text-primary)", outline: "none", fontFamily: "monospace",
                  }} />
                <button className="btn btn-primary" onClick={saveLLMSettings} disabled={llmLoading}
                  style={{ whiteSpace: "nowrap" }}>
                  <RefreshCw size={14} /> 保存
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
            <InfoRow label="模型" value={embInfo.embedding_model || "未知"} />
            <InfoRow label="状态" value={embInfo.embedding_ok ? "✅ 正常" : "❌ 异常"} />
            <InfoRow label="维度" value={embInfo.embedding_dim || 0} />
          </>
        ) : (
          <p style={{ color: "var(--text-muted)", fontSize: 13 }}>加载中...</p>
        )}
      </Section>

      <Section icon={<Activity size={16} />} title="后台整理状态">
        <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap" }}>
          <button className="btn btn-ghost" onClick={loadDaemonStatus}>
            <RefreshCw size={14} /> 刷新状态
          </button>
          <button className="btn btn-primary" onClick={async () => {
            await fetch("/api/daemon/maintain", { method: "POST", headers: { Authorization: "Bearer " + secret } });
            setTimeout(loadDaemonStatus, 800);
          }}>
            <Activity size={14} /> 手动整理
          </button>
        </div>
        {daemonStatus ? (
          <>
            <InfoRow label="状态" value={daemonStatus.status || "unknown"} />
            <InfoRow label="更新时间" value={daemonStatus.updated_at || "还没有"} />
            {daemonStatus.started_at && <InfoRow label="开始时间" value={daemonStatus.started_at} />}
            {daemonStatus.finished_at && <InfoRow label="结束时间" value={daemonStatus.finished_at} />}
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: "var(--space-sm)" }}>
              {(daemonStatus.steps || []).slice(-8).map((step) => (
                <div key={step.key} style={{
                  padding: "8px 10px",
                  border: "1px solid var(--glass-border)",
                  borderRadius: "var(--radius-sm)",
                  background: "var(--bg-card)",
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 12 }}>
                    <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{step.label || step.key}</span>
                    <span style={{ color: step.status === "error" ? "#b91c1c" : "var(--text-muted)", fontFamily: "monospace" }}>
                      {step.status} · {step.duration_ms || 0}ms
                    </span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4, wordBreak: "break-word" }}>
                    {step.error ? step.error.message : summarizeStep(step.result)}
                  </div>
                </div>
              ))}
            </div>
          </>
        ) : (
          <p style={{ color: "var(--text-muted)", fontSize: 13 }}>还没有状态记录</p>
        )}
      </Section>

      <div style={{ textAlign: "center", marginTop: "var(--space-lg)" }}>
        <a href="/static/legacy.html" target="_blank" rel="noopener"
          style={{ fontSize: 12, color: "var(--text-muted)" }}>
          打开后台日志台（小模型活动 / 手动整理） ↗
        </a>
      </div>
    </div>
  );
}

function summarizeStep(result) {
  if (result === null || result === undefined) return "";
  if (typeof result === "string") return result;
  if (typeof result === "number" || typeof result === "boolean") return String(result);
  if (Array.isArray(result)) return `${result.length} 项`;
  if (typeof result === "object") {
    const parts = Object.entries(result)
      .slice(0, 5)
      .map(([key, value]) => {
        if (typeof value === "object" && value !== null) return `${key}: ${Array.isArray(value) ? value.length + "项" : "..."}`;
        return `${key}: ${String(value).slice(0, 40)}`;
      });
    return parts.join(" · ");
  }
  return String(result);
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
