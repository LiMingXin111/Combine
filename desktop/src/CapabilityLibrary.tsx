import { useState } from "react";
import {
  Plus,
  X,
  Search,
  Pencil,
  Copy,
  Trash2,
  ArrowLeft,
  Layers,
  Sparkles,
  Bot,
  GraduationCap,
} from "lucide-react";
import { rpc } from "./api";
import { CAPABILITIES, capabilityKey } from "./capabilities";
import "./capabilityLibrary.css";
import CommunityLibrary, { CapabilitySource } from "./CommunityLibrary";

type Any = Record<string, any>;
const labels: Record<string, string> = {
  skill: "Skills",
  agent: "Agents",
  expert: "Experts",
  workflow: "Workflows",
};
const icons: Record<string, typeof Layers> = {
  skill: Sparkles,
  agent: Bot,
  expert: GraduationCap,
  workflow: Layers,
};
const defaults = (kind = "skill", mode = "api"): Any => ({
  kind,
  title: "",
  description: "",
  recipe: {
    mode,
    provider_id: "",
    max_tokens: mode === "local" ? 768 : 8192,
    instruction: "",
    stages:
      kind === "agent"
        ? [
            "Plan the approach and acceptance criteria.",
            "Produce the requested deliverable.",
            "Review and return the complete corrected final answer.",
          ]
        : [
            "Complete the requested task using the instructions and supplied evidence.",
          ],
  },
});
const networkNeeded = (graph: Any): boolean =>
  graph.nodes?.some(
    (n: Any) =>
      n.type_id === "model.api" ||
      n.type_id.startsWith("network.") ||
      (n.type_id === "workflow.composite" &&
        networkNeeded(n.config?.definition || {})),
  );
export function capabilityNode(cap: Any) {
  return {
    id: "workflow.composite",
    version: "1",
    title: cap.title,
    defaults: {
      capability_kind: cap.kind || "workflow",
      ...(cap.source ? { capability_source: structuredClone(cap.source) } : {}),
      definition: {
        ...structuredClone(cap.definition),
        id: cap.id,
        version: cap.version,
        library_version: cap.version,
        kind: cap.kind || "workflow",
      },
    },
  };
}

export function SaveCapabilityForm({
  node,
  onSaved,
}: {
  node: Any;
  onSaved: () => Promise<unknown>;
}) {
  const [kind, setKind] = useState(node.config?.capability_kind || "workflow"),
    [title, setTitle] = useState(node.title || ""),
    [description, setDescription] = useState("");
  const [pending, setPending] = useState(false),
    [error, setError] = useState("");
  return (
    <form
      className="capability-editor"
      onSubmit={async (e) => {
        e.preventDefault();
        setPending(true);
        setError("");
        try {
          await rpc("capability.save", {
            kind,
            title,
            description,
            definition: node.config.definition,
            origin_id: node.config.definition?.id,
          });
          await onSaved();
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          setPending(false);
        }
      }}
    >
      <label>
        Capability kind
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          {Object.entries(labels).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Name
        <input
          required
          maxLength={150}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </label>
      <label>
        Description
        <textarea
          maxLength={1000}
          rows={3}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <p className="capability-notice">
        Saves a new, independent capability containing this internal workflow
        and its public ports. Existing nodes keep their definitions.
      </p>
      {error && (
        <p role="alert" className="capability-error">
          {error}
        </p>
      )}
      <button className="button primary" disabled={pending}>
        {pending ? "Saving…" : "Save capability"}
      </button>
    </form>
  );
}

export default function CapabilityLibrary({
  types,
  capabilities,
  providers,
  community,
  mode,
  onAdd,
  onChanged,
  onClose,
}: {
  types: Any[];
  capabilities: Any[];
  providers: Any[];
  community: Any[];
  mode: string;
  onAdd: (type: Any) => void;
  onChanged: () => Promise<unknown>;
  onClose: () => void;
}) {
  const [tab, setTab] = useState("all"),
    [search, setSearch] = useState("");
  const [draft, setDraft] = useState<Any | null>(null),
    [pending, setPending] = useState(false),
    [error, setError] = useState("");
  const matches = (item: Any) =>
    `${item.title} ${item.description || ""}`
      .toLowerCase()
      .includes(search.toLowerCase());
  const caps = capabilities.filter(
    (c) =>
      (tab === "all" || tab === c.kind || (tab === "workflow" && !c.kind)) &&
      matches(c),
  );
  const groups = (tab === "all" || tab === "tools" ? types : [])
    .filter((t) => t.id !== "workflow.composite" && matches(t))
    .reduce((result: Any, t) => {
      (result[capabilityKey(t)] ||= []).push(t);
      return result;
    }, {});
  const edit = (cap: Any, copy = false) => {
    const value = structuredClone(cap);
    if (copy || cap.builtin) {
      value.origin_id = cap.id;
      delete value.id;
      delete value.version;
      value.title += " copy";
    }
    setDraft(value);
    setError("");
  };
  const perform = async (action: () => Promise<unknown>) => {
    setPending(true);
    setError("");
    try {
      await action();
      await onChanged();
      setDraft(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(false);
    }
  };
  const patchRecipe = (values: Any) =>
    setDraft((d) => ({ ...d, recipe: { ...d?.recipe, ...values } }));
  return (
    <>
      <div className="panel-title">
        <h3>Node library</h3>
        <button
          className="icon-button"
          aria-label="Close node library"
          disabled={pending}
          onClick={onClose}
        >
          <X size={14} />
        </button>
      </div>
      {draft ? (
        <form
          className="capability-editor"
          onSubmit={(e) => {
            e.preventDefault();
            void perform(() => rpc("capability.save", draft));
          }}
        >
          <button
            type="button"
            className="button secondary"
            disabled={pending}
            onClick={() => setDraft(null)}
          >
            <ArrowLeft size={13} /> Back to library
          </button>
          <h4>{draft.id ? "Edit capability" : "Create capability"}</h4>
          <label>
            Capability kind
            <select
              aria-label="Capability kind"
              value={draft.kind}
              disabled={pending}
              onChange={(e) =>
                setDraft((d) => ({ ...d, kind: e.target.value }))
              }
            >
              {Object.entries(labels)
                .filter(([key]) => !draft.recipe || key !== "workflow")
                .map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Name
            <input
              aria-label="Capability name"
              required
              maxLength={150}
              value={draft.title}
              onChange={(e) =>
                setDraft((d) => ({ ...d, title: e.target.value }))
              }
            />
          </label>
          <label>
            Description
            <textarea
              aria-label="Capability description"
              maxLength={1000}
              rows={2}
              value={draft.description}
              onChange={(e) =>
                setDraft((d) => ({ ...d, description: e.target.value }))
              }
            />
          </label>
          {draft.recipe ? (
            <>
              <label>
                Execution
                <select
                  aria-label="Capability execution"
                  value={draft.recipe.mode}
                  onChange={(e) =>
                    patchRecipe({
                      mode: e.target.value,
                      max_tokens: e.target.value === "local" ? 768 : 8192,
                    })
                  }
                >
                  <option value="api">API AI</option>
                  <option value="local">Local AI</option>
                </select>
              </label>
              {draft.recipe.mode === "api" ? (
                <label>
                  AI connection
                  <select
                    aria-label="Capability AI connection"
                    value={draft.recipe.provider_id || ""}
                    onChange={(e) =>
                      patchRecipe({ provider_id: e.target.value })
                    }
                  >
                    <option value="">Use workspace default</option>
                    {providers
                      .filter((p) => p.enabled)
                      .map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name} · {p.model}
                        </option>
                      ))}
                  </select>
                </label>
              ) : (
                <p className="capability-notice">
                  Runs on this computer. The local model must be downloaded and
                  ready.
                </p>
              )}
              <label>
                Instructions
                <textarea
                  aria-label="Capability instructions"
                  required
                  maxLength={16000}
                  rows={6}
                  value={draft.recipe.instruction}
                  onChange={(e) => patchRecipe({ instruction: e.target.value })}
                  placeholder="Describe the procedure, agent role or expert's review criteria."
                />
              </label>
              <label>
                Execution stages (one per line, up to 6)
                <textarea
                  aria-label="Capability stages"
                  required
                  rows={5}
                  value={draft.recipe.stages.join("\n")}
                  onChange={(e) =>
                    patchRecipe({ stages: e.target.value.split("\n") })
                  }
                />
              </label>
              <label>
                Output tokens per stage
                <input
                  aria-label="Capability output tokens"
                  type="number"
                  min={128}
                  max={32768}
                  required
                  value={draft.recipe.max_tokens}
                  onChange={(e) =>
                    patchRecipe({ max_tokens: Number(e.target.value) })
                  }
                />
              </label>
              <p className="capability-notice">
                Each stage uses the selected model; cached results may be
                reused. Later stages keep the original task and previous result.
                API execution requires a saved connection; private files and
                derived content stay local.
              </p>
            </>
          ) : (
            <p className="capability-notice">
              This capability contains {draft.definition?.nodes?.length || 0}{" "}
              internal nodes. Edit its workflow on the canvas, then save a new
              capability from its inspector.
            </p>
          )}
          {draft.kind === "expert" && (
            <p className="capability-notice">
              An expert is an AI role with explicit review criteria.
            </p>
          )}
          <CapabilitySource source={draft.source} />
          <p className="capability-notice">
            Saving a new version preserves existing canvas instances.
          </p>
          {error && (
            <p role="alert" className="capability-error">
              {error}
            </p>
          )}
          <button className="button primary full" disabled={pending}>
            {pending ? "Saving…" : "Save capability"}
          </button>
        </form>
      ) : (
        <>
          <p>Reusable skills, agents, experts and tools.</p>
          <button
            className="button secondary full"
            onClick={() => {
              setDraft(
                defaults(
                  ["skill", "agent", "expert"].includes(tab) ? tab : "skill",
                  mode,
                ),
              );
              setError("");
            }}
          >
            <Plus size={14} /> Create capability
          </button>
          <div
            className="capability-tabs"
            role="tablist"
            aria-label="Capability libraries"
          >
            {[
              ["all", "All"],
              ["skill", "Skills"],
              ["agent", "Agents"],
              ["expert", "Experts"],
              ["workflow", "Workflows"],
              ["tools", "Tools"],
              ["community", "Community"],
            ].map(([key, title]) => (
              <button
                key={key}
                role="tab"
                aria-selected={tab === key}
                onClick={() => setTab(key)}
              >
                {title}
              </button>
            ))}
          </div>
          {tab === "community" ? (
            <CommunityLibrary
              entries={community}
              providers={providers}
              mode={mode}
              onAdd={(cap) => onAdd(capabilityNode(cap))}
              onEdit={(cap) => edit(cap, true)}
              onChanged={onChanged}
            />
          ) : (
            <>
              {!!community.length && tab === "all" && (
                <button
                  className="button secondary full community-shortcut"
                  onClick={() => setTab("community")}
                >
                  {community.length} open-source capabilities <Plus size={13} />
                </button>
              )}
              <label className="search-box compact">
                <Search size={14} />
                <input
                  aria-label="Find a capability"
                  placeholder="Find a node…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
              {error && (
                <p role="alert" className="capability-error">
                  {error}
                </p>
              )}
              {Object.entries(labels).map(([kind, label]) => {
                const items = caps.filter(
                    (c) => (c.kind || "workflow") === kind,
                  ),
                  Icon = icons[kind];
                return items.length ? (
                  <section
                    className="capability-group saved-capabilities"
                    key={kind}
                  >
                    <h4>{label}</h4>
                    {items.map((cap) => (
                      <article
                        key={cap.id}
                        className="saved-capability"
                        style={
                          {
                            "--cap-color": `var(--cap-${kind})`,
                          } as React.CSSProperties
                        }
                      >
                        <button
                          className="saved-capability-add"
                          aria-label={`Add ${cap.title}`}
                          title={cap.description}
                          onClick={() => onAdd(capabilityNode(cap))}
                        >
                          <Icon size={16} />
                          <span>
                            <strong>{cap.title}</strong>
                            <small>{cap.description}</small>
                          </span>
                          <Plus size={13} />
                        </button>
                        <div className="capability-meta">
                          <span>
                            {cap.builtin ? "Built-in" : `v${cap.version}`} ·{" "}
                            {networkNeeded(cap.definition)
                              ? "API / online"
                              : "On device"}
                          </span>
                          <div>
                            <button
                              className="icon-button"
                              aria-label={`Duplicate ${cap.title}`}
                              title="Save a copy"
                              disabled={pending}
                              onClick={() => edit(cap, true)}
                            >
                              <Copy size={12} />
                            </button>
                            {!cap.builtin && (
                              <>
                                <button
                                  className="icon-button"
                                  aria-label={`Edit ${cap.title}`}
                                  disabled={pending}
                                  onClick={() => edit(cap)}
                                >
                                  <Pencil size={12} />
                                </button>
                                <button
                                  className="icon-button"
                                  aria-label={`Delete ${cap.title}`}
                                  disabled={pending}
                                  onClick={() => {
                                    if (
                                      window.confirm(
                                        `Delete “${cap.title}” from the library? Existing canvas instances will keep working.`,
                                      )
                                    )
                                      void perform(() =>
                                        rpc("capability.delete", {
                                          id: cap.id,
                                          version: cap.version,
                                        }),
                                      );
                                  }}
                                >
                                  <Trash2 size={12} />
                                </button>
                              </>
                            )}
                          </div>
                        </div>
                        {cap.source && <CapabilitySource source={cap.source} />}
                      </article>
                    ))}
                  </section>
                ) : null;
              })}
              {Object.entries(groups).map(([category, nodes]) => (
                <div className="capability-group" key={category}>
                  <h4>{CAPABILITIES[category]?.label || category}</h4>
                  {(nodes as Any[]).map((type) => (
                    <button
                      key={type.id}
                      title={type.description}
                      onClick={() => onAdd(type)}
                    >
                      <span
                        className="capability-dot"
                        style={{ background: `var(--cap-${category})` }}
                      />
                      <span>{type.title}</span>
                      <Plus size={13} />
                    </button>
                  ))}
                </div>
              ))}
              {!caps.length && !Object.keys(groups).length && (
                <p>
                  No matching capabilities. Create one or change your search.
                </p>
              )}
              <div className="library-hint">
                <Layers size={17} />
                <strong>Build from nodes</strong>
                <p>
                  Combine selected nodes, then use Save to capability library in
                  the inspector. Web and file tools remain explicit, visible
                  nodes.
                </p>
              </div>
            </>
          )}
        </>
      )}
    </>
  );
}
