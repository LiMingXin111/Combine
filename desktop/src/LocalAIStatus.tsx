import { Check, HardDrive, ShieldCheck } from "lucide-react";
type Any = Record<string, any>;
const labels: Record<string, string> = {
  ready: "Ready",
  missing: "Not installed",
  downloading: "Downloading",
  needs_verification: "Verification required",
  error: "Needs attention",
};
export default function LocalAIStatus({
  ai,
  onManage,
}: {
  ai: Any;
  onManage: () => void;
}) {
  const local = ai.local;
  return (
    <section className="local-ai-status" aria-label="Current Local AI">
      <header>
        <HardDrive size={20} />
        <div>
          <small>ON-DEVICE MODEL</small>
          <h3>{local?.model || "Local model"}</h3>
        </div>
        <span
          className={`local-model-readiness ${local?.ready ? "ready" : "missing"}`}
        >
          {local?.ready && <Check size={12} />}
          {local?.ready
            ? "Ready on this computer"
            : local
              ? "Preparation required"
              : "Checking resources"}
        </span>
      </header>
      <p>
        Local AI is selected. Conversation requests stay on this computer. Saved
        API connections are inactive for the assistant.
      </p>
      <div className="local-ai-requirements">
        {local?.requirements?.map((r: Any) => (
          <div key={r.id}>
            <span>{r.title}</span>
            <small>{labels[r.status] || "Needs attention"}</small>
          </div>
        ))}
      </div>
      <footer>
        <span>
          <ShieldCheck size={13} />
          No automatic API fallback
        </span>
        <button
          type="button"
          className="button secondary small"
          onClick={onManage}
        >
          Manage local models
        </button>
      </footer>
    </section>
  );
}
