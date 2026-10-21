import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  Check,
  ChevronDown,
  Copy,
  FolderPlus,
  HardDrive,
  LogIn,
  Plus,
  RefreshCw,
  Share2,
  Users,
  X,
} from "lucide-react";
import { rpc } from "./api";
import "./workspaceSpaces.css";

type Any = Record<string, any>;

function Dialog({
  title,
  children,
  close,
  locked,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
  locked: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const frame = requestAnimationFrame(() =>
      box.current
        ?.querySelector<HTMLElement>("button,input,select,textarea")
        ?.focus(),
    );
    return () => {
      cancelAnimationFrame(frame);
      previous?.focus();
    };
  }, []);
  return createPortal(
    <div
      className="modal-shade spaces-shade"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !locked) close();
      }}
    >
      <section
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="modal spaces-dialog"
        onKeyDown={(e) => {
          if (e.key === "Escape" && !locked) {
            e.stopPropagation();
            close();
          }
          if (e.key === "Tab") {
            const items = [
              ...(box.current?.querySelectorAll<HTMLElement>(
                "button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]",
              ) || []),
            ];
            const first = items[0],
              last = items.at(-1);
            if (e.shiftKey && document.activeElement === first) {
              e.preventDefault();
              last?.focus();
            }
            if (!e.shiftKey && document.activeElement === last) {
              e.preventDefault();
              first?.focus();
            }
          }
        }}
      >
        <header>
          <h2>{title}</h2>
          <button
            className="icon-button"
            aria-label="Close spaces dialog"
            disabled={locked}
            onClick={close}
          >
            <X size={18} />
          </button>
        </header>
        {children}
      </section>
    </div>,
    document.body,
  );
}

export default function WorkspaceSpaces({
  snapshot,
  disabled,
  dirty,
  workflow,
  shareRequest,
  onImported,
}: {
  snapshot: Any;
  disabled: boolean;
  dirty: boolean;
  workflow: Any | null;
  shareRequest: number;
  onImported: (graph: Any) => void;
}) {
  const [panel, setPanel] = useState("");
  const [spaces, setSpaces] = useState<Any>({ items: [] });
  const [account, setAccount] = useState<Any>({
    connected: false,
    server: "http://127.0.0.1:8765",
  });
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [name, setName] = useState(""),
    [convert, setConvert] = useState(false);
  const [code, setCode] = useState(""),
    [invitation, setInvitation] = useState<Any | null>(null);
  const [email, setEmail] = useState(""),
    [password, setPassword] = useState(""),
    [displayName, setDisplayName] = useState("");
  const [register, setRegister] = useState(false),
    [nextPanel, setNextPanel] = useState("select");
  const [teams, setTeams] = useState<Any[]>([]),
    [teamId, setTeamId] = useState("");
  const [library, setLibrary] = useState<Any | null>(null),
    [details, setDetails] = useState<Any | null>(null);
  const [invite, setInvite] = useState<Any | null>(null);
  const [title, setTitle] = useState("Shared workflow"),
    [description, setDescription] = useState("");
  const [configuration, setConfiguration] = useState(true),
    [preview, setPreview] = useState<Any | null>(null);
  const [publishTarget, setPublishTarget] = useState("");
  const createKey = useRef(crypto.randomUUID());
  const space = snapshot.workspace;
  const currentTeam = space.kind === "team" ? space.team?.id : "";
  const compatible =
    account.connected &&
    space.team?.account_id === account.user?.id &&
    space.team?.server === account.server;
  const role = teams.find((t) => t.id === teamId)?.role;

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const show = (value: string) => {
    setError("");
    setNotice("");
    setPanel(value);
  };
  async function initialize() {
    const [list, identity] = await Promise.all([
      rpc("spaces.list"),
      rpc("account.status"),
    ]);
    setSpaces(list);
    setAccount(identity);
    return identity;
  }
  function canSwitch() {
    if (disabled)
      throw Error(
        "Wait for the current action to finish before switching spaces.",
      );
    if (
      dirty &&
      !window.confirm(
        "Switch spaces and discard unapplied canvas changes? Your saved workflow will remain available.",
      )
    )
      return false;
    const draft = document.querySelector<HTMLTextAreaElement>(
      ".assistant-panel textarea",
    );
    if (
      draft?.value.trim() &&
      !window.confirm("Switch spaces and discard the unsent chat draft?")
    )
      return false;
    return true;
  }
  async function activate(id: string) {
    if (!canSwitch()) return;
    await rpc("spaces.activate", { id });
    window.location.reload();
  }
  async function choose(value: string, identity = account) {
    if (value === "local") {
      setName("");
      show("local");
      return;
    }
    if (value === "rename") {
      setName(space.name);
      show("rename");
      return;
    }
    if (value === "select" || value === "account") {
      show(value);
      return;
    }
    if (!identity.connected) {
      setNextPanel(value);
      show("account");
      return;
    }
    setTeams([]);
    setLibrary(null);
    setDetails(null);
    setInvitation(null);
    setInvite(null);
    const result = await rpc("team.list");
    setTeams(result.items);
    const selected =
      (compatible && result.items.some((t: Any) => t.id === currentTeam)
        ? currentTeam
        : result.items[0]?.id) || "";
    setTeamId(selected);
    if (value === "team") {
      setName("");
      setConvert(false);
      createKey.current = crypto.randomUUID();
    }
    if (value === "join") {
      setCode("");
      setInvitation(null);
    }
    if (value === "share") {
      if (!workflow?.id || dirty)
        throw Error("Apply your canvas changes before sharing a workflow.");
      setTitle("Shared workflow");
      setDescription("");
      setPreview(null);
      setConfiguration(true);
      setPublishTarget("");
    }
    show(value);
    if (value === "library" && selected) await loadLibrary(selected);
    if (value === "manage" && selected) await loadDetails(selected);
  }
  async function loadLibrary(id = teamId) {
    setLibrary(null);
    setLibrary(await rpc("team.library", { team_id: id }));
  }
  async function loadDetails(id = teamId) {
    setDetails(null);
    setDetails(await rpc("team.detail", { team_id: id }));
  }
  const seenShare = useRef(shareRequest);
  useEffect(() => {
    if (seenShare.current === shareRequest) return;
    seenShare.current = shareRequest;
    show("select");
    void act(async () => {
      const identity = await initialize();
      await choose("share", identity);
    });
  }, [shareRequest]);
  useEffect(() => {
    if (!panel) setPassword("");
  }, [panel]);
  const close = () => {
    if (!busy) {
      setPanel("");
      setPassword("");
      setInvite(null);
      setPreview(null);
    }
  };
  const titleMap: Any = {
    select: "Your spaces",
    local: "Create a local space",
    rename: "Rename this space",
    team: "Create a team space",
    join: "Join a team space",
    account: "Team account",
    library: "Team workflows",
    manage: "Team members & invites",
    share: "Share workflow to team",
    preview: "Review workflow upload",
  };
  const teamSelect = (onChange?: (id: string) => Promise<void>) => (
    <label>
      Team
      <select
        aria-label="Team"
        value={teamId}
        disabled={busy}
        onChange={(e) => {
          const id = e.target.value;
          setTeamId(id);
          setInvite(null);
          setPreview(null);
          setPublishTarget("");
          if (onChange) void act(() => onChange(id));
        }}
      >
        <option value="" disabled>
          Choose a team
        </option>
        {teams.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
          </option>
        ))}
      </select>
    </label>
  );
  const noTeams = (
    <div className="space-empty">
      <Users size={26} />
      <h3>No teams yet</h3>
      <p>Create a team or join one with an invite code.</p>
      <div className="space-actions">
        <button
          className="button primary"
          onClick={() => act(() => choose("team"))}
        >
          Create team
        </button>
        <button
          className="button secondary"
          onClick={() => act(() => choose("join"))}
        >
          Join team
        </button>
      </div>
    </div>
  );

  return (
    <>
      <button
        className="workspace-switch space-selector"
        aria-label={`Select space: ${space.name}`}
        aria-haspopup="dialog"
        aria-expanded={!!panel}
        disabled={disabled}
        onClick={() => {
          show("select");
          void act(async () => {
            await initialize();
          });
        }}
      >
        <span
          className={`avatar ${space.kind === "team" ? "team-avatar" : ""}`}
        >
          {space.kind === "team" ? (
            <Users size={18} />
          ) : (
            space.name.slice(0, 1).toUpperCase()
          )}
        </span>
        <div>
          <strong>{space.name}</strong>
          <small>
            {space.kind === "team"
              ? "Team space · local execution"
              : "Local workspace"}
          </small>
        </div>
        <ChevronDown size={14} />
      </button>
      {!!panel && (
        <Dialog
          title={titleMap[panel] || "Your spaces"}
          close={close}
          locked={busy}
        >
          {error && (
            <div className="space-error" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="space-notice" role="status">
              {notice}
            </div>
          )}
          {busy && (
            <div className="space-progress" role="status">
              Working…
            </div>
          )}
          <div className="spaces-body" aria-busy={busy}>
            {panel === "select" && (
              <>
                <p className="space-intro">
                  Keep work on this computer, or connect a space to a shared
                  team library. Nothing uploads automatically.
                </p>
                <div className="space-list">
                  {spaces.items.map((s: Any) => (
                    <button
                      key={s.id}
                      className={`space-row ${s.id === space.space_id ? "selected" : ""}`}
                      disabled={busy || disabled}
                      onClick={() =>
                        act(async () => {
                          if (s.id !== space.space_id) await activate(s.id);
                          else close();
                        })
                      }
                    >
                      {s.kind === "team" ? (
                        <Users size={19} />
                      ) : (
                        <HardDrive size={19} />
                      )}
                      <span>
                        <strong>
                          {s.id === space.space_id ? space.name : s.name}
                        </strong>
                        <small>
                          {s.kind === "team"
                            ? "Team space · working copy on this computer"
                            : "Local · no account required"}
                        </small>
                      </span>
                      {s.id === space.space_id && <Check size={17} />}
                    </button>
                  ))}
                </div>
                {currentTeam && !compatible && (
                  <p className="space-hint">
                    Sign in to the account that joined this team to access its
                    shared library. Your downloaded workflows remain usable
                    locally.
                  </p>
                )}
                <div className="space-option-grid">
                  <button disabled={busy} onClick={() => choose("local")}>
                    <FolderPlus size={18} />
                    <strong>New local space</strong>
                    <small>Separate files, chats and AI settings</small>
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => act(() => choose("team"))}
                  >
                    <Plus size={18} />
                    <strong>Create team</strong>
                    <small>Start a team or connect this local space</small>
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => act(() => choose("join"))}
                  >
                    <LogIn size={18} />
                    <strong>Join team</strong>
                    <small>Preview and accept an invite code</small>
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => act(() => choose("library"))}
                  >
                    <Users size={18} />
                    <strong>Team workflows</strong>
                    <small>Browse and download independent copies</small>
                  </button>
                </div>
                <div className="space-actions">
                  <button
                    className="button secondary"
                    disabled={busy}
                    onClick={() => choose("rename")}
                  >
                    Rename this space
                  </button>
                  <button
                    className="button secondary"
                    disabled={busy}
                    onClick={() => act(() => choose("manage"))}
                  >
                    Members & invites
                  </button>
                </div>
                <footer className="space-account-line">
                  <span>
                    {account.connected
                      ? `${account.user.display_name || account.user.email} · ${new URL(account.server).host}`
                      : "Team account optional"}
                  </span>
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => {
                      setNextPanel("select");
                      show("account");
                    }}
                  >
                    {account.connected ? "Account" : "Sign in"}
                  </button>
                </footer>
              </>
            )}
            {panel === "account" && (
              <>
                {account.connected ? (
                  <>
                    <h3>{account.user.display_name || account.user.email}</h3>
                    <p>{account.user.email}</p>
                    <p className="space-hint">
                      Connected to {account.server}. This account is for team
                      sharing. It does not change local workspace ownership or
                      share your API keys.
                    </p>
                    <div className="space-actions">
                      <button
                        className="button primary"
                        disabled={busy}
                        onClick={() => act(() => choose(nextPanel))}
                      >
                        Continue
                      </button>
                      <button
                        className="button secondary"
                        disabled={busy}
                        onClick={() =>
                          act(async () => {
                            const identity = await rpc("account.logout");
                            setAccount(identity);
                            setTeams([]);
                            setLibrary(null);
                            setDetails(null);
                            setNotice(
                              "Signed out. Local workflows remain available.",
                            );
                          })
                        }
                      >
                        Sign out on this device
                      </button>
                    </div>
                  </>
                ) : (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      void act(async () => {
                        const identity = await rpc(
                          register ? "account.register" : "account.login",
                          {
                            server: account.server,
                            email,
                            password,
                            display_name: displayName,
                          },
                        );
                        setAccount(identity);
                        setPassword("");
                        await choose(nextPanel, identity);
                      });
                    }}
                  >
                    <p className="space-intro">
                      A team account is required to create, join and share.
                      Local work stays available without signing in.
                    </p>
                    <label>
                      Team server
                      <input
                        aria-label="Team server"
                        type="url"
                        value={account.server}
                        disabled={busy}
                        onChange={(e) =>
                          setAccount({ ...account, server: e.target.value })
                        }
                        required
                      />
                    </label>
                    <small className="space-hint">
                      Use your hosted Combine server. For development, a
                      loopback HTTP server is supported.
                    </small>
                    {register && (
                      <label>
                        Display name
                        <input
                          aria-label="Display name"
                          value={displayName}
                          disabled={busy}
                          maxLength={80}
                          onChange={(e) => setDisplayName(e.target.value)}
                          required
                        />
                      </label>
                    )}
                    <label>
                      Email
                      <input
                        aria-label="Team account email"
                        type="email"
                        value={email}
                        disabled={busy}
                        autoComplete="username"
                        onChange={(e) => setEmail(e.target.value)}
                        required
                      />
                    </label>
                    <label>
                      Password
                      <input
                        aria-label="Team account password"
                        type="password"
                        value={password}
                        disabled={busy}
                        minLength={8}
                        autoComplete={
                          register ? "new-password" : "current-password"
                        }
                        onChange={(e) => setPassword(e.target.value)}
                        required
                      />
                    </label>
                    <div className="space-actions">
                      <button
                        className="button primary"
                        disabled={busy}
                        type="submit"
                      >
                        {register
                          ? "Create account & continue"
                          : "Sign in & continue"}
                      </button>
                      <button
                        className="text-button"
                        disabled={busy}
                        type="button"
                        onClick={() => setRegister(!register)}
                      >
                        {register
                          ? "Use an existing account"
                          : "Create an account"}
                      </button>
                    </div>
                  </form>
                )}
              </>
            )}
            {(panel === "local" || panel === "rename") && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(async () => {
                    if (panel === "rename") {
                      if (!canSwitch()) return;
                      await rpc("spaces.rename", { name });
                      window.location.reload();
                    } else {
                      if (!canSwitch()) return;
                      const created = await rpc("spaces.create", { name });
                      await rpc("spaces.activate", { id: created.id });
                      window.location.reload();
                    }
                  });
                }}
              >
                <p className="space-intro">
                  This space has its own files, conversations, workflows and AI
                  connections on this computer.
                </p>
                <label>
                  Space name
                  <input
                    autoFocus
                    aria-label="Space name"
                    maxLength={80}
                    value={name}
                    disabled={busy}
                    onChange={(e) => setName(e.target.value)}
                    required
                  />
                </label>
                <button
                  className="button primary"
                  type="submit"
                  disabled={busy || !name.trim()}
                >
                  {panel === "rename" ? "Save name" : "Create local space"}
                </button>
              </form>
            )}
            {panel === "team" && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(async () => {
                    if (!canSwitch()) return;
                    const result = await rpc("team.create", {
                      name,
                      convert_current: convert,
                      request_key: createKey.current,
                    });
                    await rpc("spaces.activate", { id: result.space.id });
                    window.location.reload();
                  });
                }}
              >
                <ol className="space-steps">
                  <li>1. Name your team</li>
                  <li>2. Choose its local working copy</li>
                  <li>3. Invite members and share workflows</li>
                </ol>
                <label>
                  Team name
                  <input
                    aria-label="Team name"
                    maxLength={80}
                    value={name}
                    disabled={busy}
                    onChange={(e) => setName(e.target.value)}
                    required
                  />
                </label>
                {space.kind !== "team" && (
                  <label className="space-check">
                    <input
                      type="checkbox"
                      checked={convert}
                      disabled={busy}
                      onChange={(e) => setConvert(e.target.checked)}
                    />
                    Connect this local space to the new team
                  </label>
                )}
                <p className="space-hint">
                  {convert
                    ? "Your current files, chats and workflows remain in place. Only the team name and your membership are created online."
                    : "A separate working copy will be created on this computer. Configure its AI connection after switching."}{" "}
                  Upload each workflow explicitly after reviewing its template.
                </p>
                <button
                  className="button primary"
                  type="submit"
                  disabled={busy || !name.trim()}
                >
                  Create team space
                </button>
              </form>
            )}
            {panel === "join" && (
              <>
                <ol className="space-steps">
                  <li>1. Paste an invite</li>
                  <li>2. Review the team</li>
                  <li>3. Join and open</li>
                </ol>
                <label>
                  Invite code
                  <input
                    aria-label="Invite code"
                    value={code}
                    maxLength={100}
                    disabled={busy}
                    onChange={(e) => {
                      setCode(e.target.value);
                      setInvitation(null);
                    }}
                  />
                </label>
                {!invitation ? (
                  <button
                    className="button primary"
                    disabled={busy || !code.trim()}
                    onClick={() =>
                      act(async () =>
                        setInvitation(
                          await rpc("team.invite_preview", {
                            code: code.trim(),
                          }),
                        ),
                      )
                    }
                  >
                    Preview team
                  </button>
                ) : (
                  <div className="space-review">
                    <h3>{invitation.team.name}</h3>
                    <p>
                      {invitation.already_joined
                        ? "You are already a member. Open your local working copy."
                        : "You will join as a member. Your existing local spaces stay separate."}
                    </p>
                    <p className="space-hint">
                      The team can see workflows you publish, not your files,
                      chat history, API keys or local results.
                    </p>
                    <button
                      className="button primary"
                      disabled={busy}
                      onClick={() =>
                        act(async () => {
                          if (!canSwitch()) return;
                          const result = await rpc("team.join", {
                            code: code.trim(),
                          });
                          await rpc("spaces.activate", { id: result.space.id });
                          window.location.reload();
                        })
                      }
                    >
                      {invitation.already_joined
                        ? "Open team space"
                        : "Join & open team space"}
                    </button>
                  </div>
                )}
                {!!teams.length && (
                  <>
                    <h3>Your existing teams</h3>
                    {teams.map((t) => (
                      <div className="space-remote-team" key={t.id}>
                        <span>
                          {t.name}
                          <small>{t.role}</small>
                        </span>
                        <button
                          className="button secondary small"
                          disabled={busy}
                          onClick={() =>
                            act(async () => {
                              if (!canSwitch()) return;
                              const result = await rpc("team.attach", {
                                team_id: t.id,
                              });
                              await rpc("spaces.activate", {
                                id: result.space.id,
                              });
                              window.location.reload();
                            })
                          }
                        >
                          Open on this computer
                        </button>
                      </div>
                    ))}
                  </>
                )}
              </>
            )}
            {panel === "library" && (
              <>
                {teams.length ? (
                  <>
                    {teamSelect(loadLibrary)}
                    <div className="space-actions">
                      <button
                        className="button secondary small"
                        disabled={busy}
                        onClick={() => act(() => loadLibrary())}
                      >
                        <RefreshCw size={14} />
                        Refresh
                      </button>
                      {workflow?.id && (
                        <button
                          className="button primary small"
                          disabled={busy || dirty}
                          onClick={() => act(() => choose("share"))}
                        >
                          <Share2 size={14} />
                          Share current workflow
                        </button>
                      )}
                    </div>
                    {library?.offline && (
                      <p className="space-hint" role="status">
                        Offline · showing the last cached list from{" "}
                        {new Date(library.cached_at).toLocaleString("en")}.
                        Connect to download or publish. Your local workflows
                        still run offline.
                      </p>
                    )}
                    {library && !library.items.length && (
                      <div className="space-empty">
                        <h3>No shared workflows yet</h3>
                        <p>Open a saved workflow and choose Share to team.</p>
                      </div>
                    )}
                    {library?.items.map((t: Any) => (
                      <article className="space-template" key={t.id}>
                        <div>
                          <h3>{t.title}</h3>
                          <span className="space-version">
                            v{t.version} · {t.author_name || "Team member"}
                          </span>
                        </div>
                        <p>{t.description || "Reusable workflow template"}</p>
                        <div className="space-actions">
                          <button
                            className="button primary small"
                            disabled={busy || library.offline || disabled}
                            onClick={() =>
                              act(async () => {
                                if (
                                  dirty &&
                                  !window.confirm(
                                    "Open the downloaded workflow and discard unapplied canvas changes?",
                                  )
                                )
                                  return;
                                const graph = await rpc("team.import", {
                                  team_id: teamId,
                                  template_id: t.id,
                                  version: t.version,
                                });
                                onImported(graph);
                                close();
                              })
                            }
                          >
                            Use a local copy
                          </button>
                          {(t.author_id === account.user?.id ||
                            role === "owner") && (
                            <button
                              className="text-button"
                              disabled={busy || library.offline}
                              onClick={() =>
                                act(async () => {
                                  if (
                                    !window.confirm(
                                      "Remove this shared workflow and all its team versions? Downloaded local copies remain available.",
                                    )
                                  )
                                    return;
                                  await rpc("team.delete_template", {
                                    team_id: teamId,
                                    template_id: t.id,
                                  });
                                  await loadLibrary();
                                })
                              }
                            >
                              Remove from team
                            </button>
                          )}
                        </div>
                      </article>
                    ))}
                    <p className="space-hint">
                      Each download is independent. Review the nodes, add your
                      local inputs and select an AI connection before running.
                    </p>
                  </>
                ) : (
                  noTeams
                )}
              </>
            )}
            {panel === "manage" && (
              <>
                {teams.length ? (
                  <>
                    {teamSelect(loadDetails)}
                    {details && (
                      <>
                        <h3>Members · {details.members.length}</h3>
                        <div className="space-list">
                          {details.members.map((m: Any) => (
                            <div className="space-member" key={m.user_id}>
                              <span>
                                {m.display_name || "Team member"}
                                <small>
                                  {m.role}
                                  {m.user_id === account.user?.id
                                    ? " · you"
                                    : ""}
                                </small>
                              </span>
                              {details.team.role === "owner" &&
                                m.role !== "owner" && (
                                  <button
                                    className="text-button"
                                    disabled={busy}
                                    onClick={() =>
                                      act(async () => {
                                        if (
                                          !window.confirm(
                                            "Remove this member's access to shared team workflows?",
                                          )
                                        )
                                          return;
                                        await rpc("team.remove_member", {
                                          team_id: teamId,
                                          user_id: m.user_id,
                                        });
                                        await loadDetails();
                                      })
                                    }
                                  >
                                    Remove member
                                  </button>
                                )}
                            </div>
                          ))}
                        </div>
                        {details.team.role === "owner" ? (
                          <>
                            <h3>Invite members</h3>
                            <p className="space-hint">
                              Generate a code and share it yourself. Members
                              open the space selector → Join team → paste the
                              code. Codes expire after 7 days and allow up to 10
                              joins.
                            </p>
                            <button
                              className="button secondary"
                              disabled={busy}
                              onClick={() =>
                                act(async () => {
                                  setInvite(
                                    await rpc("team.invite", {
                                      team_id: teamId,
                                    }),
                                  );
                                  await loadDetails();
                                })
                              }
                            >
                              Generate invite code
                            </button>
                            {invite && (
                              <div className="space-review">
                                <label>
                                  New invite code
                                  <input
                                    aria-label="New invite code"
                                    readOnly
                                    value={invite.code}
                                  />
                                </label>
                                <button
                                  className="button secondary small"
                                  disabled={busy}
                                  onClick={() =>
                                    act(async () => {
                                      await navigator.clipboard.writeText(
                                        invite.code,
                                      );
                                      setNotice("Invite code copied.");
                                    })
                                  }
                                >
                                  <Copy size={13} />
                                  Copy invite code
                                </button>
                              </div>
                            )}
                            {details.invites
                              .filter(
                                (i: Any) =>
                                  !i.revoked && i.expires_at > Date.now(),
                              )
                              .map((i: Any) => (
                                <div className="space-invite" key={i.id}>
                                  <span>
                                    {i.uses}/{i.max_uses} used · expires{" "}
                                    {new Date(i.expires_at).toLocaleDateString(
                                      "en",
                                    )}
                                  </span>
                                  <button
                                    className="text-button"
                                    disabled={busy}
                                    onClick={() =>
                                      act(async () => {
                                        await rpc("team.revoke_invite", {
                                          team_id: teamId,
                                          id: i.id,
                                        });
                                        if (invite?.id === i.id)
                                          setInvite(null);
                                        await loadDetails();
                                      })
                                    }
                                  >
                                    Revoke invite
                                  </button>
                                </div>
                              ))}
                          </>
                        ) : (
                          <button
                            className="button secondary"
                            disabled={busy}
                            onClick={() =>
                              act(async () => {
                                if (
                                  !window.confirm(
                                    "Leave this team? Your downloaded local workflows will remain on this computer.",
                                  )
                                )
                                  return;
                                await rpc("team.remove_member", {
                                  team_id: teamId,
                                  user_id: account.user.id,
                                });
                                await choose("select");
                                setNotice(
                                  "You left the team. Your local working copy is preserved.",
                                );
                              })
                            }
                          >
                            Leave team
                          </button>
                        )}
                      </>
                    )}
                  </>
                ) : (
                  noTeams
                )}
              </>
            )}
            {panel === "share" && (
              <>
                {teams.length ? (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      void act(async () => {
                        if (!workflow?.id || dirty)
                          throw Error(
                            "Save and apply your workflow before sharing.",
                          );
                        const value = await rpc("sharing.preview", {
                          workflow_id: workflow.id,
                          version: workflow.version,
                          title,
                          description,
                          include_configuration: configuration,
                        });
                        setPreview(value);
                        setPublishTarget(teamId);
                        show("preview");
                      });
                    }}
                  >
                    {teamSelect()}
                    <label>
                      Shared workflow title
                      <input
                        aria-label="Shared workflow title"
                        value={title}
                        maxLength={160}
                        disabled={busy}
                        onChange={(e) => setTitle(e.target.value)}
                        required
                      />
                    </label>
                    <label>
                      Description
                      <textarea
                        aria-label="Shared workflow description"
                        value={description}
                        maxLength={2000}
                        disabled={busy}
                        onChange={(e) => setDescription(e.target.value)}
                        rows={3}
                      />
                    </label>
                    <label className="space-check">
                      <input
                        type="checkbox"
                        checked={configuration}
                        disabled={busy}
                        onChange={(e) => setConfiguration(e.target.checked)}
                      />
                      Include reusable instructions and public configuration
                    </label>
                    <p className="space-hint">
                      Inputs and local references are reset to placeholders.
                      Protected sources and their downstream configuration are
                      reset. Review the exact template before publishing.
                    </p>
                    <button
                      className="button primary"
                      type="submit"
                      disabled={busy || !teamId || !title.trim()}
                    >
                      Prepare upload preview
                    </button>
                  </form>
                ) : (
                  noTeams
                )}
              </>
            )}
            {panel === "preview" && preview && (
              <>
                <h3>{preview.payload.title}</h3>
                <p>
                  {preview.node_count} top-level nodes →{" "}
                  {teams.find((t) => t.id === publishTarget)?.name}
                </p>
                <div className="space-review">
                  <strong>Excluded from upload</strong>
                  <ul>
                    {preview.excluded.map((x: string) => (
                      <li key={x}>{x}</li>
                    ))}
                  </ul>
                </div>
                {!!preview.redactions.length && (
                  <details>
                    <summary>
                      {preview.redactions.length} protected nodes reset
                    </summary>
                    <ul>
                      {preview.redactions.map((x: string, i: number) => (
                        <li key={i}>{x}</li>
                      ))}
                    </ul>
                  </details>
                )}
                <details className="space-json" open>
                  <summary>Exact workflow template to upload</summary>
                  <pre>{JSON.stringify(preview.payload, null, 2)}</pre>
                </details>
                <p className="space-hint">
                  Team members can download this template. Reusable prompts may
                  be visible in the JSON above. Publishing never starts a run.
                </p>
                <div className="space-actions">
                  <button
                    className="button secondary"
                    disabled={busy}
                    onClick={() => show("share")}
                  >
                    Back to edit
                  </button>
                  <button
                    className="button primary"
                    disabled={busy || disabled}
                    onClick={() =>
                      act(async () => {
                        const result = await rpc("team.publish", {
                          team_id: publishTarget,
                          preview_token: preview.token,
                        });
                        setTeamId(publishTarget);
                        setPreview(null);
                        show("library");
                        await loadLibrary(publishTarget);
                        setNotice(
                          `Published “${result.template.title}” · v${result.template.version}.`,
                        );
                      })
                    }
                  >
                    Publish workflow to team
                  </button>
                </div>
              </>
            )}
            {panel !== "select" && (
              <button
                className="text-button spaces-back"
                disabled={busy}
                onClick={() => show("select")}
              >
                ← All spaces
              </button>
            )}
          </div>
        </Dialog>
      )}
    </>
  );
}
