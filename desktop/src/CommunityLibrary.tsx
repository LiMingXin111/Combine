import { useMemo, useState } from "react";
import {
  ArrowLeft,
  Download,
  ExternalLink,
  GitBranch,
  Plus,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { rpc } from "./api";
type Any = Record<string, any>;

export function CapabilitySource({ source }: { source?: Any }) {
  if (!source) return null;
  return (
    <details className="capability-provenance">
      <summary>Open-source provenance</summary>
      <a href={source.url} target="_blank" rel="noreferrer">
        {source.repository} <ExternalLink size={11} />
      </a>
      <p>Commit {source.commit}</p>
      <p>{source.path}</p>
      <p>
        {source.license} · {source.adaptation}
      </p>
      <p>SHA-256 {source.sha256}</p>
      {source.license_text && (
        <details>
          <summary>License text</summary>
          <pre>{source.license_text}</pre>
        </details>
      )}
      {source.notices?.map((notice: Any) => (
        <details key={notice.path}>
          <summary>{notice.path}</summary>
          <pre>{notice.text}</pre>
        </details>
      ))}
    </details>
  );
}

export default function CommunityLibrary({
  entries,
  providers,
  mode: initialMode,
  onAdd,
  onEdit,
  onChanged,
}: {
  entries: Any[];
  providers: Any[];
  mode: string;
  onAdd: (cap: Any) => void;
  onEdit: (cap: Any) => void;
  onChanged: () => Promise<unknown>;
}) {
  const [mode, setMode] = useState(initialMode),
    [provider, setProvider] = useState("");
  const [search, setSearch] = useState(""),
    [category, setCategory] = useState("");
  const [importing, setImporting] = useState(false),
    [url, setUrl] = useState(""),
    [path, setPath] = useState(""),
    [ref, setRef] = useState("");
  const [preview, setPreview] = useState<Any | null>(null),
    [selected, setSelected] = useState<string[]>([]),
    [kind, setKind] = useState("");
  const [pending, setPending] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const categories = useMemo(
    () => [...new Set(entries.map((e) => e.category))].sort(),
    [entries],
  );
  const visible = entries.filter(
    (e) =>
      (!category || e.category === category) &&
      `${e.title} ${e.description} ${e.kind} ${e.repository}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const resolve = async (entry: Any, edit: boolean) => {
    setPending(entry.id);
    setError("");
    try {
      const cap = await rpc("community.resolve", {
        id: entry.id,
        mode,
        provider_id: provider,
      });
      if (edit) onEdit(cap);
      else onAdd(cap);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending("");
    }
  };
  const inspect = async () => {
    setPending("inspect");
    setError("");
    setNotice("");
    setPreview(null);
    try {
      const result = await rpc("github.inspect", { url, path, ref });
      setPreview(result);
      setSelected(
        result.entries.filter((e: Any) => e.available).map((e: Any) => e.id),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending("");
    }
  };
  const install = async () => {
    if (!preview) return;
    setPending("install");
    setError("");
    try {
      const result = await rpc("github.install", {
        preview_id: preview.id,
        ids: selected,
        mode,
        provider_id: provider,
        ...(kind ? { kind } : {}),
      });
      await onChanged();
      setNotice(
        `${result.count} guidance node${result.count === 1 ? "" : "s"} imported. Find them under Skills, Agents or Experts.`,
      );
      setSelected([]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending("");
    }
  };
  const changed = (setter: (v: string) => void, value: string) => {
    setter(value);
    setPreview(null);
    setSelected([]);
    setNotice("");
  };
  return (
    <div className="community-library">
      <h4>Open-source capabilities</h4>
      <p className="capability-notice">
        {entries.length} bundled methods from GitHub. Each runs as an AI
        guidance workflow with a pinned source and license. Tools, scripts and
        MCP actions need explicit adapters.
      </p>
      <label>
        Execution
        <select
          aria-label="Community execution"
          value={mode}
          disabled={!!pending}
          onChange={(e) => setMode(e.target.value)}
        >
          <option value="api">API AI</option>
          <option value="local">Local AI</option>
        </select>
      </label>
      {mode === "api" && (
        <label>
          AI connection
          <select
            aria-label="Community AI connection"
            value={provider}
            disabled={!!pending}
            onChange={(e) => setProvider(e.target.value)}
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
      )}
      {mode === "local" && (
        <p className="capability-notice">
          Uses the downloaded on-device model. Long methods must fit its context
          limit; missing resources are reported without a cloud fallback.
        </p>
      )}
      {!importing ? (
        <>
          <button
            className="button secondary full"
            onClick={() => {
              setImporting(true);
              setError("");
            }}
          >
            <GitBranch size={14} /> Import from GitHub
          </button>
          <label className="search-box compact">
            <Search size={14} />
            <input
              aria-label="Find open-source capability"
              placeholder="Search open source…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <label>
            Category
            <select
              aria-label="Community category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              <option value="">All categories</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          {error && (
            <p role="alert" className="capability-error">
              {error}
            </p>
          )}
          <div
            className="community-results"
            aria-label="Open-source capabilities"
          >
            {visible.map((entry) => (
              <article
                className="saved-capability community-entry"
                key={entry.id}
                style={
                  {
                    "--cap-color": `var(--cap-${entry.kind})`,
                  } as React.CSSProperties
                }
              >
                <div>
                  <strong>{entry.title}</strong>
                  <span className="community-kind">
                    {entry.kind} · {entry.category}
                  </span>
                  <p>{entry.description}</p>
                </div>
                <div className="community-source">
                  <a href={entry.url} target="_blank" rel="noreferrer">
                    {entry.repository} <ExternalLink size={10} />
                  </a>
                  <span>
                    {entry.license} · {entry.commit.slice(0, 7)}
                  </span>
                  <span>
                    {entry.source_characters.toLocaleString()} reference
                    characters
                  </span>
                </div>
                <p className="capability-notice">
                  AI guidance ·{" "}
                  {mode === "local" ? "On device" : "API / online"}
                </p>
                <div className="community-actions">
                  <button
                    className="button secondary"
                    disabled={!!pending}
                    aria-label={`Configure ${entry.title}`}
                    onClick={() => void resolve(entry, true)}
                  >
                    <SlidersHorizontal size={12} /> Copy
                  </button>
                  <button
                    className="button primary"
                    disabled={!!pending}
                    aria-label={`Add open-source ${entry.title}`}
                    onClick={() => void resolve(entry, false)}
                  >
                    <Plus size={12} />{" "}
                    {pending === entry.id ? "Preparing…" : "Add node"}
                  </button>
                </div>
              </article>
            ))}
            {!visible.length && <p>No matching open-source capabilities.</p>}
          </div>
        </>
      ) : (
        <>
          <button
            className="button secondary"
            disabled={!!pending}
            onClick={() => {
              setImporting(false);
              setError("");
            }}
          >
            <ArrowLeft size={13} /> Bundled catalog
          </button>
          <form
            className="capability-editor"
            onSubmit={(e) => {
              e.preventDefault();
              void inspect();
            }}
          >
            <label>
              Public GitHub URL
              <input
                aria-label="Public GitHub URL"
                required
                type="url"
                placeholder="https://github.com/owner/repository"
                value={url}
                disabled={!!pending}
                onChange={(e) => changed(setUrl, e.target.value)}
              />
            </label>
            <label>
              Folder or Markdown file (optional)
              <input
                aria-label="GitHub repository path"
                placeholder="skills/example/SKILL.md"
                value={path}
                disabled={!!pending}
                onChange={(e) => changed(setPath, e.target.value)}
              />
            </label>
            <label>
              Revision (optional)
              <input
                aria-label="GitHub revision"
                placeholder="Commit SHA or branch"
                value={ref}
                disabled={!!pending}
                onChange={(e) => changed(setRef, e.target.value)}
              />
            </label>
            <p className="capability-notice">
              Downloads public definitions and applicable licenses. The preview
              fixes a commit; importing creates local nodes without running
              upstream code. No GitHub token is needed.
            </p>
            <button className="button secondary full" disabled={!!pending}>
              {pending === "inspect" ? "Reading GitHub…" : "Inspect repository"}
            </button>
          </form>
          {error && (
            <p role="alert" className="capability-error">
              {error}
            </p>
          )}
          {notice && (
            <p role="status" className="capability-notice">
              {notice}
            </p>
          )}
          {preview && (
            <section
              className="github-preview"
              aria-label="GitHub import preview"
            >
              <h4>{preview.repository}</h4>
              <p className="capability-notice">
                Commit {preview.commit.slice(0, 12)} · {preview.total}{" "}
                {preview.total === 1 ? "definition" : "definitions"} found
                {preview.limited
                  ? "; first 24 shown. Narrow the folder for more."
                  : "."}
              </p>
              <label>
                Import as
                <select
                  aria-label="Imported capability kind"
                  value={kind}
                  disabled={!!pending}
                  onChange={(e) => setKind(e.target.value)}
                >
                  <option value="">Use detected types</option>
                  <option value="skill">Skills</option>
                  <option value="agent">Agents</option>
                  <option value="expert">Experts</option>
                </select>
              </label>
              {preview.entries.map((entry: Any) => (
                <label
                  className={`github-preview-entry ${entry.available ? "" : "unavailable"}`}
                  key={entry.id}
                >
                  <input
                    type="checkbox"
                    aria-label={`Import ${entry.title}`}
                    checked={selected.includes(entry.id)}
                    disabled={!!pending || !entry.available}
                    onChange={(e) =>
                      setSelected((ids) =>
                        e.target.checked
                          ? [...ids, entry.id]
                          : ids.filter((id) => id !== entry.id),
                      )
                    }
                  />
                  <span>
                    <strong>{entry.title}</strong>
                    <small>{entry.path}</small>
                    <small>
                      {entry.available
                        ? `${entry.license} · AI guidance`
                        : entry.reason}
                    </small>
                    {!!entry.dependencies?.length && (
                      <small>
                        Separate adapters: {entry.dependencies.join(", ")}
                      </small>
                    )}
                  </span>
                </label>
              ))}
              <button
                className="button primary full"
                disabled={!!pending || !selected.length}
                onClick={() => void install()}
              >
                <Download size={14} />
                {pending === "install"
                  ? "Importing…"
                  : `Import ${selected.length} guidance ${selected.length === 1 ? "node" : "nodes"}`}
              </button>
            </section>
          )}
        </>
      )}
    </div>
  );
}
