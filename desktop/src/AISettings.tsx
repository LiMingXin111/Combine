import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  KeyRound,
  Loader2,
  Plus,
  ShieldCheck,
  Sparkles,
  Trash2,
  Zap,
} from "lucide-react";
import { rpc } from "./api";
import { ModelField, type ModelOption } from "./ModelSelection";
import LocalAIStatus from "./LocalAIStatus";
type Any = Record<string, any>;
const presets = [
  {
    name: "DeepSeek",
    base_url: "https://api.deepseek.com",
    model: "",
  },
  { name: "OpenAI", base_url: "https://api.openai.com/v1", model: "" },
  { name: "OpenRouter", base_url: "https://openrouter.ai/api/v1", model: "" },
  {
    name: "Qwen / DashScope",
    base_url: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    model: "",
  },
  { name: "Custom compatible API", base_url: "", model: "" },
];
export default function AISettings({
  ai,
  usage,
  refresh,
  onLocalResources,
}: {
  ai: Any;
  usage: Any[];
  refresh: () => Promise<unknown>;
  onLocalResources: () => void;
}) {
  const [form, setForm] = useState<Any>({ ...presets[0], api_key: "" }),
    [editing, setEditing] = useState(false),
    [busy, setBusy] = useState(""),
    [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [models, setModels] = useState<ModelOption[]>([]),
    [modelsLoading, setModelsLoading] = useState(false);
  const modelRequest = useRef(0);
  const savedConnections = useRef<HTMLDetailsElement>(null);
  const isLocal = ai.mode === "local";
  const [webEnabled, setWebEnabled] = useState(ai.web?.enabled !== false);
  useEffect(() => setWebEnabled(ai.web?.enabled !== false), [ai.web?.enabled]);
  async function fetchModels(params: Any) {
    const token = ++modelRequest.current;
    setModelsLoading(true);
    setError("");
    try {
      const result = await rpc<ModelOption[]>("provider.models", params);
      if (token === modelRequest.current) {
        setModels(result);
        if (params.id && !("base_url" in params)) await refresh();
      }
    } catch (e) {
      if (token === modelRequest.current) setError((e as Error).message);
    } finally {
      if (token === modelRequest.current) setModelsLoading(false);
    }
  }
  function openForm(provider?: Any) {
    if (savedConnections.current) savedConnections.current.open = true;
    modelRequest.current++;
    setForm({
      ...(provider || presets[0]),
      api_key: "",
      make_default: !isLocal && (!provider || provider.id === ai.provider_id),
    });
    setModels(provider?.model_catalog || []);
    setModelsLoading(false);
    setError("");
    setMessage("");
    setEditing(true);
    if (provider?.has_key && !isLocal) void fetchModels({ id: provider.id });
  }
  function closeForm() {
    modelRequest.current++;
    setModelsLoading(false);
    setEditing(false);
    setForm({ ...presets[0], api_key: "" });
    setModels([]);
  }
  function changeCredentials(values: Any) {
    modelRequest.current++;
    setModelsLoading(false);
    setModels([]);
    setError("");
    setForm({ ...form, ...values });
  }
  async function act(label: string, fn: () => Promise<any>) {
    setBusy(label);
    setError("");
    setMessage("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="ai-settings" id="ai-connections">
      <div className="settings-section-heading">
        <span className="settings-symbol">
          <Sparkles size={22} />
        </span>
        <div>
          <h2>AI connections</h2>
          <p>Bring a capable model. Make it your everyday collaborator.</p>
        </div>
        <button
          className="button primary"
          disabled={!!busy}
          onClick={() => openForm()}
        >
          <Plus size={15} />
          Add connection
        </button>
      </div>
      <div className="ai-mode-selector">
        <button
          className={ai.mode !== "local" ? "selected" : ""}
          aria-pressed={!isLocal}
          disabled={!!busy}
          onClick={() =>
            act("Switching mode", () =>
              rpc("ai.settings", { values: { mode: "api" } }),
            )
          }
        >
          <Zap size={19} />
          <div>
            <strong>API AI</strong>
            <small>Recommended · your choice of model</small>
          </div>
          {ai.mode !== "local" && <Check size={16} />}
        </button>
        <button
          className={ai.mode === "local" ? "selected" : ""}
          aria-pressed={isLocal}
          disabled={!!busy}
          onClick={() =>
            act("Switching mode", () =>
              rpc("ai.settings", { values: { mode: "local" } }),
            )
          }
        >
          <ShieldCheck size={19} />
          <div>
            <strong>Local AI</strong>
            <small>Optional · private and offline work</small>
          </div>
          {ai.mode === "local" && <Check size={16} />}
        </button>
      </div>
      {isLocal && <LocalAIStatus ai={ai} onManage={onLocalResources} />}
      <details
        className="saved-api-connections"
        ref={savedConnections}
        open={!isLocal}
      >
        <summary>
          {isLocal ? "Saved API connections · inactive" : "API connections"}
        </summary>
        {isLocal && (
          <p className="field-hint">
            Saved for API mode. These connections do not power the local
            assistant.
          </p>
        )}
        {!ai.providers?.length && !editing && (
          <div className="connect-ai-empty">
            <KeyRound size={25} />
            <h3>Connect the intelligence you prefer.</h3>
            <p>
              Add your API key to start a real conversation and let AI build on
              your canvas. There are no simulated AI answers.
            </p>
            <button className="button primary" onClick={() => openForm()}>
              Connect an AI model
            </button>
          </div>
        )}
        {ai.providers?.map((provider: Any) => (
          <article className="provider-row" key={provider.id}>
            <span className="provider-logo">
              <Sparkles size={19} />
            </span>
            <div>
              <strong>
                {provider.name}
                {ai.provider_id === provider.id && (
                  <span className="default-tag">
                    {isLocal ? "API default · inactive" : "Default"}
                  </span>
                )}
              </strong>
              <button
                type="button"
                className="provider-model-current"
                aria-label={`Choose model for ${provider.name}`}
                disabled={!!busy}
                onClick={() => openForm(provider)}
              >
                {provider.model_catalog?.find(
                  (m: ModelOption) => m.id === provider.model,
                )?.name || provider.model}
                <ChevronDown size={12} />
              </button>
              <small>{new URL(provider.base_url).hostname}</small>
              <span className="key-status">
                <ShieldCheck size={11} />
                {provider.has_key
                  ? "Key encrypted on this computer"
                  : "API key required on this device"}
              </span>
            </div>
            <div className="provider-actions">
              <button
                className="button secondary small"
                disabled={!!busy || !provider.has_key || isLocal}
                title={
                  isLocal
                    ? "Switch to API AI to test this connection"
                    : "Test this API model"
                }
                onClick={() =>
                  act("Testing actual model connection", async () => {
                    const result = await rpc("provider.test", {
                      id: provider.id,
                    });
                    setMessage(
                      `Connected to ${result.model}. Actual model response: ${result.reply}`,
                    );
                  })
                }
              >
                {busy === "Testing actual model connection" ? (
                  <Loader2 size={13} className="spin" />
                ) : (
                  <Zap size={13} />
                )}
                Test
              </button>
              <button
                className="text-button"
                disabled={!!busy}
                onClick={() => openForm(provider)}
              >
                Edit
              </button>
              {(isLocal || ai.provider_id !== provider.id) && (
                <button
                  className="text-button"
                  onClick={() =>
                    act("Selecting model", () =>
                      rpc("ai.settings", {
                        values: { provider_id: provider.id, mode: "api" },
                      }),
                    )
                  }
                >
                  {isLocal ? "Use API AI" : "Use"}
                </button>
              )}
              <button
                className="icon-button"
                aria-label={`Delete ${provider.name} connection`}
                onClick={() => {
                  if (
                    window.confirm(
                      `Delete ${provider.name} and its encrypted key? Existing outputs are preserved.`,
                    )
                  )
                    act("Removing connection", () =>
                      rpc("provider.delete", { id: provider.id }),
                    );
                }}
              >
                <Trash2 size={15} />
              </button>
            </div>
          </article>
        ))}
        {editing && (
          <form
            className="provider-form"
            onSubmit={(e) => {
              e.preventDefault();
              act("Saving encrypted connection", async () => {
                const saved = await rpc("provider.save", form);
                let discoveryFailed = false;
                if (models.length && (!isLocal || form.make_default)) {
                  try {
                    await rpc("provider.models", { id: saved.id });
                  } catch {
                    discoveryFailed = true;
                  }
                }
                closeForm();
                setMessage(
                  discoveryFailed
                    ? "Connection saved. Refresh the model list when the API is available."
                    : isLocal && !form.make_default
                      ? "API connection saved. Local AI remains selected."
                      : "Connection saved. Your selected model is ready; the key is encrypted on this computer.",
                );
              });
            }}
          >
            <header>
              <h3>{form.id ? "Edit connection" : "New connection"}</h3>
              <button
                type="button"
                className="text-button"
                disabled={!!busy}
                onClick={closeForm}
              >
                Cancel
              </button>
            </header>
            {!form.id && (
              <label>
                Provider
                <select
                  aria-label="AI provider preset"
                  value={
                    presets.some((p) => p.name === form.name)
                      ? form.name
                      : "Custom compatible API"
                  }
                  onChange={(e) => {
                    modelRequest.current++;
                    setModels([]);
                    setModelsLoading(false);
                    setError("");
                    setForm({
                      ...presets.find((p) => p.name === e.target.value),
                      api_key: "",
                      make_default: true,
                    });
                  }}
                >
                  {presets.map((p) => (
                    <option key={p.name}>{p.name}</option>
                  ))}
                </select>
              </label>
            )}
            <div className="provider-form-grid">
              <label>
                Connection name
                <input
                  aria-label="Connection name"
                  required
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </label>
              <label>
                API base URL
                <input
                  aria-label="API base URL"
                  required
                  type="url"
                  placeholder="https://api.example.com/v1"
                  value={form.base_url}
                  onChange={(e) =>
                    changeCredentials({ base_url: e.target.value, model: "" })
                  }
                />
              </label>
            </div>
            <label>
              API key
              <input
                aria-label="API key"
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                required={!form.id}
                value={form.api_key}
                placeholder={
                  form.id
                    ? "Leave blank to keep the existing key"
                    : "Stored encrypted on this computer"
                }
                onChange={(e) => changeCredentials({ api_key: e.target.value })}
              />
            </label>
            <ModelField
              key={form.id || form.base_url || "new-connection"}
              value={form.model || ""}
              models={models}
              busy={modelsLoading}
              disabled={
                isLocal ||
                !!busy ||
                !form.base_url ||
                (!form.id && !form.api_key)
              }
              onChange={(model) => setForm({ ...form, model })}
              onFetch={() =>
                fetchModels({
                  id: form.id,
                  base_url: form.base_url,
                  api_key: form.api_key,
                })
              }
            />
            <label className="default-model-check">
              <input
                type="checkbox"
                checked={!!form.make_default}
                onChange={(e) =>
                  setForm({ ...form, make_default: e.target.checked })
                }
              />
              {isLocal
                ? "Switch to API AI and use this connection by default"
                : "Use this connection and model by default"}
            </label>
            <p className="field-hint">
              Use an OpenAI-compatible Chat Completions endpoint. Changing the
              endpoint requires entering its key again.
            </p>
            <button
              className="button primary"
              disabled={!!busy || modelsLoading || !form.model?.trim()}
              type="submit"
            >
              {busy ? (
                <Loader2 size={15} className="spin" />
              ) : (
                <ShieldCheck size={15} />
              )}
              Save connection
            </button>
          </form>
        )}
      </details>
      {busy && (
        <div className="settings-feedback">
          <Loader2 size={14} className="spin" />
          {busy}…
        </div>
      )}
      {message && (
        <p className="settings-success" role="status">
          <Check size={15} />
          {message}
        </p>
      )}
      {error && (
        <p className="chat-error" role="alert">
          {error}
        </p>
      )}
      <div className="ai-boundary-note">
        <ShieldCheck size={18} />
        <p>
          {isLocal
            ? "Local AI conversation and private material stay on this computer. Missing local resources do not trigger an API request."
            : "Conversation text is sent to your selected API. Imported files, private notes and their derived results stay on this computer. Private document understanding and classification use optional Local AI."}
        </p>
      </div>
      {!isLocal && (
        <div className="optional-local-row">
          <div>
            <strong>Optional on-device models</strong>
            <p>
              For offline workflows and private material. No download is
              required for API conversations.
            </p>
          </div>
          <button className="button secondary" onClick={onLocalResources}>
            Manage local models
            <ChevronDown size={13} />
          </button>
        </div>
      )}
      <section className="web-research-settings" aria-label="Web research">
        <h3>Web research</h3>
        {isLocal ? (
          <p>
            Unavailable in Local AI mode. Switch to API AI to use public web
            research. Your saved web configuration is preserved.
          </p>
        ) : (
          <>
            <p>
              Search and read public pages with DeepSeek Search &amp; Fetch.
              Uses a saved DeepSeek key; works with either Flash or Pro as your
              AI model.
            </p>
            <label className="default-model-check">
              <input
                type="checkbox"
                checked={webEnabled}
                disabled={!!busy}
                onChange={(e) => {
                  const enabled = e.target.checked;
                  setWebEnabled(enabled);
                  void act("Updating web access", async () => {
                    try {
                      return await rpc("ai.settings", {
                        values: { web_enabled: enabled },
                      });
                    } catch (error) {
                      setWebEnabled(!enabled);
                      throw error;
                    }
                  });
                }}
              />
              Enable public web search and fetching
            </label>
            <label>
              DeepSeek web connection
              <select
                aria-label="DeepSeek web connection"
                value={ai.web_provider_id || ""}
                disabled={!!busy}
                onChange={(e) =>
                  act("Selecting web connection", () =>
                    rpc("ai.settings", {
                      values: { web_provider_id: e.target.value },
                    }),
                  )
                }
              >
                <option value="">
                  Automatic · default or only DeepSeek connection
                </option>
                {ai.web?.providers?.map((p: Any) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <p className="field-hint" role="status">
              {ai.mode === "local"
                ? "Unavailable in Local AI mode."
                : ai.web?.available
                  ? "Ready · AI can search and fetch public sources. Web nodes require internet."
                  : "Add or select an official DeepSeek connection to use web research."}
            </p>
            <p className="field-hint">
              Private files and derived content cannot be sent as search queries
              or URLs. Web content may be incomplete or unavailable on
              restricted pages.
            </p>
            <small>
              Estimated Search / Fetch price: $
              {ai.web?.price_usd_per_request?.toFixed(5) || "0.00075"} per
              successful request. Checked{" "}
              {ai.web?.pricing_checked_at || "2026-10-20"}.{" "}
              <a
                href="https://api-docs.deepseek.com/guides/search/"
                target="_blank"
                rel="noreferrer noopener"
              >
                DeepSeek pricing
              </a>
              . No task budget cutoff.
            </small>
          </>
        )}
      </section>
      <section className="api-usage-settings" aria-label="API usage">
        <div className="usage-heading">
          <h3>Actual API usage</h3>
          <span>{usage.length} recorded requests</span>
        </div>
        {isLocal && (
          <p className="field-hint">
            Historical API usage only. The local assistant does not use your API
            connection.
          </p>
        )}
        {usage.length ? (
          <div className="usage-table">
            <table>
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Purpose</th>
                  <th>Tokens</th>
                  <th>Time</th>
                  <th>Estimated cost</th>
                </tr>
              </thead>
              <tbody>
                {usage.slice(0, 12).map((item) => (
                  <tr key={item.id}>
                    <td>{item.model}</td>
                    <td>{item.purpose.replaceAll("_", " ")}</td>
                    <td>
                      {item.request_count
                        ? `${item.request_count} web request`
                        : (item.total_tokens ?? "Not reported")}
                    </td>
                    <td>{(item.duration_ms / 1000).toFixed(1)}s</td>
                    <td>
                      {typeof item.estimated_cost === "number"
                        ? `${item.currency || "USD"} ${item.estimated_cost.toFixed(5)}`
                        : "Unavailable"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <small>
              Usage comes from actual provider responses. Unavailable pricing is
              not treated as free. There is no task budget cutoff.
            </small>
          </div>
        ) : (
          <p className="field-hint">
            Actual usage appears after a connection test, conversation or
            workflow model call.
          </p>
        )}
      </section>
    </div>
  );
}
