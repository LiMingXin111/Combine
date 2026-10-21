import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Loader2,
  RefreshCw,
  Settings,
  Sparkles,
  X,
} from "lucide-react";
import { rpc } from "./api";
import LocalAIStatus from "./LocalAIStatus";

type Any = Record<string, any>;
export type ModelOption = { id: string; name?: string };
const modelLabel = (model: ModelOption) =>
  model.name && model.name !== model.id
    ? `${model.name} (${model.id})`
    : model.id;

export function ModelField({
  value,
  models,
  busy,
  disabled = false,
  onChange,
  onFetch,
}: {
  value: string;
  models: ModelOption[];
  busy: boolean;
  disabled?: boolean;
  onChange: (value: string) => void;
  onFetch: () => void;
}) {
  const [manual, setManual] = useState(false);
  return (
    <div className="model-field">
      <div className="model-input-row">
        <label>
          {manual ? "Model ID" : "Model"}
          {manual ? (
            <input
              aria-label="Model ID"
              required
              value={value}
              placeholder="Exact model ID from your provider"
              spellCheck={false}
              onChange={(e) => onChange(e.target.value)}
            />
          ) : (
            <select
              aria-label="Model"
              required
              value={value}
              onChange={(e) => onChange(e.target.value)}
            >
              <option value="">Choose a model</option>
              {value && !models.some((m) => m.id === value) && (
                <option value={value}>{value} · Current / custom</option>
              )}
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {modelLabel(m)}
                </option>
              ))}
            </select>
          )}
        </label>
        <button
          type="button"
          className="button secondary small"
          disabled={busy || disabled}
          onClick={onFetch}
        >
          {busy ? (
            <Loader2 size={14} className="spin" />
          ) : (
            <RefreshCw size={14} />
          )}
          {models.length ? "Refresh models" : "Fetch models"}
        </button>
      </div>
      {value && (
        <small className="selected-model-id" title={value}>
          {value}
        </small>
      )}
      <div className="model-field-footnote">
        <small>
          {models.length
            ? `${models.length} models returned by this API`
            : "Fetch models with your API key, or enter an exact model ID."}
        </small>
        <button
          type="button"
          className="text-button"
          onClick={() => setManual(!manual)}
        >
          {manual ? "Choose from list" : "Enter model ID manually"}
        </button>
      </div>
    </div>
  );
}

export default function ModelPicker({
  ai,
  disabled,
  onConfigure,
  onLocalResources,
  refresh,
}: {
  ai: Any;
  disabled: boolean;
  onConfigure: () => void;
  onLocalResources?: () => void;
  refresh: () => Promise<unknown>;
}) {
  const active = ai.providers?.find((p: Any) => p.id === ai.provider_id);
  const isLocal = ai.mode === "local";
  const [open, setOpen] = useState(false),
    [providerId, setProviderId] = useState(""),
    [model, setModel] = useState(""),
    [models, setModels] = useState<ModelOption[]>([]),
    [loading, setLoading] = useState(false),
    [saving, setSaving] = useState(false),
    [error, setError] = useState("");
  const request = useRef(0),
    container = useRef<HTMLDivElement>(null);
  const chosen = ai.providers?.find((p: Any) => p.id === providerId);
  async function fetchModels(id: string) {
    const token = ++request.current;
    setLoading(true);
    setError("");
    try {
      const result = await rpc<ModelOption[]>("provider.models", { id });
      if (token === request.current) setModels(result);
    } catch (e) {
      if (token === request.current) setError((e as Error).message);
    } finally {
      if (token === request.current) setLoading(false);
    }
  }
  useEffect(() => {
    if (!open || isLocal) return;
    const provider = ai.providers?.find((p: Any) => p.id === providerId);
    setModel(provider?.model || "");
    setModels(provider?.model_catalog || []);
    setLoading(false);
    setError("");
    if (provider?.has_key) void fetchModels(providerId);
    return () => {
      request.current++;
    };
  }, [open, providerId, isLocal]);
  useEffect(() => {
    request.current++;
    setOpen(false);
    setLoading(false);
  }, [isLocal]);
  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (!container.current?.contains(e.target as Node)) setOpen(false);
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div className="model-picker-container" ref={container}>
      <button
        className="model-picker"
        type="button"
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={
          isLocal
            ? `${ai.local?.model || "Local model"} · on this computer`
            : active?.model
        }
        onClick={() => {
          if (!isLocal && !ai.providers?.length) {
            onConfigure();
            return;
          }
          if (!isLocal) setProviderId(active?.id || ai.providers[0].id);
          setError("");
          setOpen(!open);
        }}
      >
        <Sparkles size={13} />
        <span>
          {ai.mode === "local"
            ? "Local AI"
            : active?.model_catalog?.find(
                (m: ModelOption) => m.id === active.model,
              )?.name ||
              active?.model ||
              "Connect your AI"}
        </span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div
          className="model-picker-popover"
          role="dialog"
          aria-label={isLocal ? "Local AI model" : "Choose AI model"}
        >
          <header>
            <strong>{isLocal ? "Local AI" : "Choose AI model"}</strong>
            <button
              type="button"
              className="icon-button"
              aria-label="Close model picker"
              onClick={() => setOpen(false)}
            >
              <X size={15} />
            </button>
          </header>
          {isLocal ? (
            <LocalAIStatus
              ai={ai}
              onManage={() => {
                setOpen(false);
                (onLocalResources || onConfigure)();
              }}
            />
          ) : (
            <>
              <label>
                Connection
                <select
                  aria-label="AI connection"
                  value={providerId}
                  disabled={saving}
                  onChange={(e) => setProviderId(e.target.value)}
                >
                  {ai.providers.map((p: Any) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <ModelField
                key={providerId}
                value={model}
                models={models}
                busy={loading}
                disabled={!chosen?.has_key || saving}
                onChange={setModel}
                onFetch={() => fetchModels(providerId)}
              />
              {error && (
                <p className="chat-error" role="alert">
                  {error}
                </p>
              )}
              <p className="field-hint">
                Used for new AI requests and nodes using this connection.
                Existing messages and outputs are preserved.
              </p>
              <footer>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    setOpen(false);
                    onConfigure();
                  }}
                >
                  <Settings size={13} /> Manage connections
                </button>
                <button
                  type="button"
                  className="button primary small"
                  disabled={saving || !model.trim() || !chosen?.has_key}
                  onClick={async () => {
                    setSaving(true);
                    setError("");
                    try {
                      await rpc("provider.select_model", {
                        id: providerId,
                        model,
                        make_default: true,
                      });
                      await refresh();
                      setOpen(false);
                    } catch (e) {
                      setError((e as Error).message);
                    } finally {
                      setSaving(false);
                    }
                  }}
                >
                  {saving ? (
                    <Loader2 size={13} className="spin" />
                  ) : (
                    <Check size={13} />
                  )}
                  Use model
                </button>
              </footer>
            </>
          )}
        </div>
      )}
    </div>
  );
}
