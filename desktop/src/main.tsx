import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  type NodeProps,
  type NodeChange,
  type Connection,
  type Edge,
  type ReactFlowInstance,
  MarkerType,
} from "@xyflow/react";
import {
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  Bell,
  BookOpen,
  Box,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  Copy,
  Database,
  Download,
  File,
  FileText,
  Folder,
  FolderOpen,
  GitBranch,
  HardDrive,
  Home,
  Layers,
  LayoutGrid,
  Loader2,
  Maximize2,
  MoreHorizontal,
  Network,
  Pause,
  Play,
  Plus,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Square,
  Trash2,
  Upload,
  Workflow,
  X,
  Zap,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  StickyNote,
  GitMerge,
  Bot,
  GraduationCap,
  Share2,
} from "lucide-react";
import { rpc, download, localFiles } from "./api";
import "@xyflow/react/dist/style.css";
import "./style.css";
import "./resizable.css";
import "./conversationLayout.css";
import AssistantPanel from "./AssistantPanel";
import AISettings from "./AISettings";
import WebSources from "./WebSources";
import FinalAnswerPanel from "./FinalAnswerPanel";
import ResizeDivider, {
  clamp,
  useElementSize,
  useWorkspaceLayout,
} from "./ResizablePanels";
import NodeSizeControls from "./NodeSizeControls";
import ConversationHistory from "./ConversationHistory";
import CapabilityLibrary, { SaveCapabilityForm } from "./CapabilityLibrary";
import { CapabilitySource } from "./CommunityLibrary";
import WorkspaceSpaces from "./WorkspaceSpaces";
import { CAPABILITIES, capabilityKey, capabilityColor } from "./capabilities";
import {
  executionFeedback,
  executionView,
  edgeFeedback,
  type ExecutionDetail,
} from "./executionVisuals";
import NodeContent, { AssetImage, MapContent } from "./NodeContent";

type Any = Record<string, any>;
const finishedRunStates = new Set([
  "completed",
  "partial",
  "failed",
  "cancelled",
]);
type Graph = {
  id: string;
  title: string;
  version: number;
  nodes: Any[];
  edges: Any[];
  [key: string]: any;
};
const pages = [
  { id: "canvas", label: "Canvas", icon: GitBranch },
  { id: "workflows", label: "Workflows", icon: Workflow },
  { id: "knowledge", label: "Knowledge", icon: BookOpen },
  { id: "settings", label: "Settings", icon: Settings },
  { id: "diagnostics", label: "Diagnostics", icon: ShieldCheck },
];
const date = (time: number) =>
  new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(time);
const bytes = (n = 0) =>
  n > 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`;
const same = (a: unknown, b: unknown) =>
  JSON.stringify(a) === JSON.stringify(b);
const emptyCanvas = (): Graph => ({
  id: "",
  title: "New conversation",
  version: 1,
  nodes: [],
  edges: [],
  annotations: [],
});
function def(item: Any, types: Any[]) {
  const type = types.find((t) => t.id === item.type_id);
  if (item.type_id === "data.merge_text") {
    const count = item.config?.input_count ?? 2;
    const labels = String(item.config?.labels || "").split("\n");
    return {
      ...type,
      inputs: Array.from(
        {
          length:
            Number.isInteger(count) && count >= 2 && count <= 16 ? count : 2,
        },
        (_, i) => ({
          id: `text_${i + 1}`,
          kind: "text",
          required: true,
          label: labels[i] || `Input ${i + 1}`,
        }),
      ),
    };
  }
  return item.type_id === "workflow.composite"
    ? {
        ...type,
        ...(["skill", "agent", "expert"].includes(
          item.config?.capability_kind || item.config?.definition?.kind,
        )
          ? {
              capability:
                item.config?.capability_kind || item.config?.definition?.kind,
              category:
                item.config?.capability_kind || item.config?.definition?.kind,
            }
          : {}),
        inputs: item.config?.definition?.inputs || [],
        outputs: item.config?.definition?.outputs || [],
      }
    : type;
}
function Badge({
  state,
  children,
}: {
  state: string;
  children?: React.ReactNode;
}) {
  return (
    <span className={`badge ${state}`}>
      {state === "ready" || state === "completed" || state === "succeeded" ? (
        <Check size={11} />
      ) : state === "running" || state === "downloading" ? (
        <Loader2 size={11} className="spin" />
      ) : (
        <span className="status-dot" />
      )}
      {children || state.replaceAll("_", " ")}
    </span>
  );
}
function Empty({
  title,
  text,
  icon: Icon = FolderOpen,
  action,
}: {
  title: string;
  text: string;
  icon?: any;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon size={25} />
      </div>
      <h3>{title}</h3>
      <p>{text}</p>
      {action}
    </div>
  );
}
function Modal({
  title,
  children,
  close,
  wide = false,
}: {
  title: string;
  children: React.ReactNode;
  close: () => void;
  wide?: boolean;
}) {
  return (
    <div
      className="modal-shade"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <section
        className={`modal ${wide ? "wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header>
          <h2>{title}</h2>
          <button
            className="icon-button"
            onClick={close}
            aria-label="Close dialog"
          >
            <X size={19} />
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
function CapabilityNode({ data, selected }: NodeProps) {
  const d = data as Any,
    item = d.item,
    type = d.definition;
  const group = capabilityKey(type);
  const feedback = executionFeedback(d.state);
  const Icon = (
    {
      input: FileText,
      research: Network,
      knowledge: BookOpen,
      processing: SlidersHorizontal,
      integration: GitMerge,
      reasoning: Sparkles,
      visualization: LayoutGrid,
      delivery: FileText,
      interaction: MessageSquare,
      workflow: Layers,
      skill: Sparkles,
      agent: Bot,
      expert: GraduationCap,
    } as Any
  )[group];
  return (
    <div
      className={`capability-node rich-node ${selected ? "selected" : ""} ${d.customSize ? "custom-sized" : ""} ${d.state || ""} execution-${feedback.tone}`}
      data-capability={group}
      data-execution={d.state || "idle"}
      style={{ "--cap-color": capabilityColor(type) } as React.CSSProperties}
    >
      <NodeSizeControls
        selected={selected}
        onResize={d.onResize}
        onReset={d.onResetSize}
      />
      <header className="rich-node-header">
        <span className={`node-icon ${type?.category?.toLowerCase()}`}>
          <Icon size={17} />
        </span>
        <div className="node-text">
          <span className="node-category">{CAPABILITIES[group].label}</span>
          <strong>{item.title || type?.title}</strong>
        </div>
        <ExecutionLight state={d.state} />
      </header>
      <div
        className="node-port-guide"
        title="Connect matching typed ports. A text input accepts one branch; use Combine text for multiple branches."
      >
        <span>
          In:{" "}
          {type?.inputs.length
            ? type.inputs.map((p: Any) => p.id).join(" · ")
            : "—"}
        </span>
        <span>Out: {type?.outputs.map((p: Any) => p.id).join(" · ")}</span>
      </div>
      <div
        className="node-content-body nodrag nowheel"
        onWheel={(event) => event.stopPropagation()}
      >
        <NodeContent
          item={item}
          outputs={d.outputs}
          asset={d.asset}
          runId={d.runId}
          nodeId={d.nodeId}
          state={d.state}
          message={d.message}
        />
      </div>
      {type?.inputs.map((port: Any, i: number) => (
        <Handle
          key={port.id}
          id={port.id}
          type="target"
          position={Position.Left}
          title={`${port.id}: ${port.kind}${port.label ? ` · ${port.label}` : ""}`}
          style={{ top: `${((i + 1) * 100) / (type.inputs.length + 1)}%` }}
        />
      ))}
      {type?.outputs.map((port: Any, i: number) => (
        <Handle
          key={port.id}
          id={port.id}
          type="source"
          position={Position.Right}
          title={`${port.id}: ${port.kind}`}
          style={{ top: `${((i + 1) * 100) / (type.outputs.length + 1)}%` }}
        />
      ))}
      <footer className="rich-node-footer">
        <span>
          {d.state && d.state !== "idle"
            ? feedback.label
            : item.type_id === "model.api"
              ? d.model || "Connect your API"
              : item.type_id === "workflow.composite"
                ? `${item.config?.definition?.nodes?.length || 0} internal nodes`
                : type?.network === "required"
                  ? "Internet required"
                  : "On this computer"}
        </span>
        <span>
          {d.final
            ? "Final answer"
            : d.outputs
              ? "Actual output"
              : d.state === "failed"
                ? "No completed output"
                : d.state === "skipped"
                  ? "Blocked upstream"
                  : d.state && d.state !== "idle"
                    ? d.state === "running"
                      ? "Output pending"
                      : d.state === "queued"
                        ? "Waiting to run"
                        : "No completed output"
                    : "Ready to configure"}
        </span>
      </footer>
    </div>
  );
}
function ExecutionLight({ state }: { state?: string }) {
  const feedback = executionFeedback(state);
  return (
    <span
      className={`execution-light execution-${feedback.tone}`}
      role="img"
      aria-label={`Execution: ${feedback.label}`}
      title={`${feedback.label} — ${feedback.description}`}
    >
      {feedback.tone === "success" ? (
        <Check size={9} />
      ) : feedback.tone === "error" ? (
        <X size={9} />
      ) : feedback.tone === "paused" ? (
        <Pause size={9} />
      ) : null}
    </span>
  );
}
function CanvasNote({ data, selected }: NodeProps) {
  const note = data as Any;
  return (
    <div
      className={`canvas-note ${selected ? "selected" : ""} ${note.customSize ? "custom-sized" : ""}`}
    >
      <NodeSizeControls
        selected={selected}
        note
        onResize={note.onResize}
        onReset={note.onResetSize}
      />
      <header>
        <StickyNote size={13} />
        <span>Canvas note</span>
      </header>
      <textarea
        key={note.item.text}
        className="nodrag nowheel"
        aria-label="Canvas note text"
        defaultValue={note.item.text}
        onBlur={(e) => {
          if (e.target.value !== note.item.text) note.onEdit(e.target.value);
        }}
        rows={4}
      />
    </div>
  );
}
const nodeTypes = { capability: CapabilityNode, annotation: CanvasNote };

function Output({ data }: { data: Any }) {
  if (data.kind === "text")
    return <pre className="document-output">{String(data.value)}</pre>;
  if (data.kind === "table")
    return (
      <div className="table-output">
        <table>
          <thead>
            <tr>
              {data.value.columns.map((c: string, i: number) => (
                <th key={i}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.value.rows.map((r: any[], i: number) => (
              <tr key={i}>
                {r.map((c, j) => (
                  <td key={j}>{String(c ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <button
          className="text-button"
          onClick={() =>
            download(
              "table.csv",
              [data.value.columns, ...data.value.rows]
                .map((r: any[]) =>
                  r
                    .map((c) => `"${String(c ?? "").replaceAll('"', '""')}"`)
                    .join(","),
                )
                .join("\r\n"),
            )
          }
        >
          <Download size={13} /> Export CSV
        </button>
      </div>
    );
  if (data.kind === "chart") {
    const table = data.value.table,
      li = table.columns.indexOf(data.value.label_column),
      vi = table.columns.indexOf(data.value.value_column);
    if (li < 0 || vi < 0)
      return <p>Choose existing label and numeric columns in this node.</p>;
    const rows = table.rows,
      peak = rows.reduce(
        (maximum: number, row: any[]) =>
          Math.max(maximum, Math.abs(Number(row[vi]) || 0)),
        1,
      );
    return (
      <div className="chart-output">
        {rows.map((r: any[], i: number) => (
          <div className="chart-row" key={i}>
            <span>{String(r[li])}</span>
            <div>
              <i
                style={{
                  width: `${(Math.abs(Number(r[vi]) || 0) / peak) * 100}%`,
                }}
              />
            </div>
            <b>{String(r[vi])}</b>
          </div>
        ))}
      </div>
    );
  }
  if (data.kind === "map") return <MapContent data={data} />;
  if (data.kind === "image")
    return <AssetImage id={data.value.asset_id} alt={data.value.alt} />;
  return (
    <pre className="document-output">{JSON.stringify(data.value, null, 2)}</pre>
  );
}

function App() {
  const layout = useWorkspaceLayout();
  const shellSize = useElementSize();
  const splitSize = useElementSize();
  const [snapshot, setSnapshot] = useState<Any | null>(null),
    [page, setPage] = useState("canvas"),
    [error, setError] = useState("");
  const [toast, setToast] = useState(""),
    [search, setSearch] = useState(""),
    [busy, setBusy] = useState("");
  const [graph, setGraph] = useState<Graph | null>(emptyCanvas),
    [dirty, setDirty] = useState(false),
    [selected, setSelected] = useState<string[]>([]);
  const [offline, setOffline] = useState<Any | null>(null),
    [runId, setRunId] = useState(""),
    [detail, setDetail] = useState<(Any & ExecutionDetail) | null>(null);
  const [launchingRun, setLaunchingRun] = useState<{
    id: string;
    workflow_id: string;
  } | null>(null);
  const runLaunch = useRef<typeof launchingRun>(null);
  const [stoppingRunId, setStoppingRunId] = useState("");
  const stopRequests = useRef(new Set<string>());
  const [panel, setPanel] = useState("runs"),
    [modal, setModal] = useState(""),
    [preview, setPreview] = useState<Any | null>(null),
    [history, setHistory] = useState<Any[]>([]);
  const [expandedAnswer, setExpandedAnswer] = useState(false);
  const presentedFinal = useRef("");
  const [newTitle, setNewTitle] = useState(""),
    [template, setTemplate] = useState("blank"),
    [goal, setGoal] = useState("");
  const [note, setNote] = useState<Any>({ title: "", content: "" }),
    [knowledgeQuery, setKnowledgeQuery] = useState(""),
    [knowledgeHits, setKnowledgeHits] = useState<Any[] | null>(null);
  const [settings, setSettings] = useState<Any>({}),
    [diagnostics, setDiagnostics] = useState<Any | null>(null),
    [editText, setEditText] = useState("");
  const [stack, setStack] = useState<{ graph: Graph; nodeId: string }[]>([]);
  const [nodeSizes, setNodeSizes] = useState<{
    context: string;
    sizes: Record<string, { width: number; height: number }>;
  }>({ context: "", sizes: {} });
  const [conversationId, setConversationId] = useState(""),
    [assistantBusy, setAssistantBusy] = useState(false),
    [sideOpen, setSideOpen] = useState(false);
  const [blankSession, setBlankSession] = useState(0);
  const [adoptConversation, setAdoptConversation] = useState("");
  const bootedWorkspace = useRef("");
  const navigationRequest = useRef(0);
  const canvasCreation = useRef<Promise<Graph | null> | null>(null);
  const [showMinimap, setShowMinimap] = useState(true);
  const [shareRequest, setShareRequest] = useState(0);
  const flowRef = useRef<ReactFlowInstance<any> | null>(null);
  const canvasArea = useRef<HTMLDivElement | null>(null);
  const ai = snapshot?.ai || { mode: "api", providers: [] };
  const configureAI = () => {
    setPage("settings");
    setTimeout(
      () =>
        document
          .getElementById("ai-connections")
          ?.scrollIntoView({ behavior: "smooth", block: "start" }),
      80,
    );
  };
  const fileInput = useRef<HTMLInputElement>(null);
  const types = snapshot?.types || [];
  const refresh = useCallback(async () => {
    try {
      const value = await rpc("workspace.snapshot");
      setSnapshot(value);
      setError("");
      return value;
    } catch (e) {
      setError(String((e as Error).message));
      return null;
    }
  }, []);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    if (snapshot) {
      document.documentElement.dataset.theme = snapshot.settings.theme;
      setSettings(snapshot.settings);
    }
  }, [snapshot?.settings?.updated_at]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 6000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (!runId) {
      setDetail(null);
      return;
    }
    let active = true;
    const update = () =>
      rpc("run.detail", { id: runId })
        .then((value) => {
          if (active) setDetail(value);
        })
        .catch(() => {});
    update();
    const timer = setInterval(update, 1200);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [runId]);
  useEffect(() => {
    if (
      !dirty &&
      detail?.run?.id === runId &&
      detail?.final?.state === "ready" &&
      presentedFinal.current !== runId
    ) {
      presentedFinal.current = runId;
      setPanel("answer");
    }
  }, [detail?.final?.state, detail?.run?.id, dirty, runId]);
  const act = async (label: string, action: () => Promise<any>) => {
    setBusy(label);
    try {
      const result = await action();
      await refresh();
      return result;
    } catch (e) {
      setToast((e as Error).message);
      return undefined;
    } finally {
      setBusy("");
    }
  };
  const rememberConversation = (id: string) => {
    setConversationId(id);
    try {
      localStorage.setItem(
        `combine.desktop.active-conversation.${snapshot?.workspace?.id || "local"}`,
        id,
      );
    } catch {}
  };
  const loadCanvas = (value: Graph, cid: string, source: Any) => {
    setToast("");
    setModal("");
    setAdoptConversation("");
    rememberConversation(cid);
    setGraph(structuredClone(value));
    setDirty(false);
    setStack([]);
    setSelected([]);
    setPage("canvas");
    setOffline(null);
    setSideOpen(false);
    setDetail(null);
    const previous = source?.runs?.find(
      (r: Any) => r.workflow_id === value.id && r.version === value.version,
    );
    setRunId(previous?.id || "");
    const request = navigationRequest.current;
    if (value.id)
      rpc("workflow.check", { graph: value })
        .then((report) => {
          if (request === navigationRequest.current) setOffline(report);
        })
        .catch((e) => {
          if (request === navigationRequest.current) setToast(e.message);
        });
  };
  const newConversation = () => {
    if (
      assistantBusy ||
      busy ||
      (dirty && !window.confirm("Leave unapplied changes?"))
    )
      return;
    navigationRequest.current++;
    setBlankSession((value) => value + 1);
    loadCanvas(emptyCanvas(), "", snapshot || {});
  };
  const open = async (value: Graph, desiredConversation?: string) => {
    if (assistantBusy) return;
    if (dirty && !window.confirm("Leave unapplied changes?")) return;
    const request = ++navigationRequest.current;
    const latest = (await refresh()) || snapshot;
    if (request !== navigationRequest.current) return;
    value = latest?.workflows.find((w: Graph) => w.id === value.id) || value;
    loadCanvas(
      value,
      desiredConversation ??
        latest?.conversations?.find((c: Any) => c.workflow_id === value.id)
          ?.id ??
        "",
      latest || {},
    );
  };
  const openConversation = async (item: Any) => {
    if (assistantBusy || (dirty && !window.confirm("Leave unapplied changes?")))
      return;
    const request = ++navigationRequest.current;
    const latest = (await refresh()) || snapshot;
    if (request !== navigationRequest.current) return;
    const conversation =
      latest?.conversations?.find((c: Any) => c.id === item.id) || item;
    const workflow = latest?.workflows.find(
      (w: Graph) => w.id === conversation.workflow_id,
    );
    loadCanvas(workflow || emptyCanvas(), conversation.id, latest || {});
    if (conversation.workflow_id && !workflow)
      setToast(
        "This conversation's previous workflow is unavailable. You can continue on a new canvas.",
      );
  };
  const ensureCanvas = async (): Promise<Graph | null> => {
    if (graph?.id) return graph;
    if (canvasCreation.current) return canvasCreation.current;
    const cid =
      conversationId ||
      "conversation_" + crypto.randomUUID().replaceAll("-", "").slice(0, 16);
    const navigation = navigationRequest.current;
    setBusy("Creating a local canvas");
    canvasCreation.current = rpc("workflow.create", {
      template: "blank",
      title: "Untitled workflow",
      conversation_id: cid,
    })
      .then(async (value) => {
        await refresh();
        if (navigation !== navigationRequest.current) return null;
        setAdoptConversation(cid);
        rememberConversation(cid);
        setGraph(value);
        return value;
      })
      .catch((e) => {
        setToast(e.message);
        return null;
      })
      .finally(() => {
        canvasCreation.current = null;
        setBusy("");
      });
    return canvasCreation.current;
  };
  const change = (fn: (g: Graph) => Graph) => {
    setGraph((current) => (current ? fn(current) : current));
    setDirty(true);
    setOffline(null);
  };
  const updateNode = (id: string, patch: Any) =>
    change((g) => ({
      ...g,
      nodes: g.nodes.map((n) => (n.id === id ? { ...n, ...patch } : n)),
    }));
  const addNode = async (type: Any) => {
    if (!(await ensureCanvas())) return;
    const id = "node_" + crypto.randomUUID().slice(0, 12);
    change((g) => ({
      ...g,
      nodes: [
        ...g.nodes,
        {
          id,
          type_id: type.id,
          type_version: type.version,
          title: type.title,
          position: {
            x: 130 + (g.nodes.length % 3) * 430,
            y: 80 + Math.floor(g.nodes.length / 3) * 400,
          },
          config: structuredClone(type.defaults),
        },
      ],
    }));
    setSelected([id]);
    setSideOpen(false);
    setTimeout(
      () => flowRef.current?.fitView({ padding: 0.12, duration: 250 }),
      100,
    );
  };
  const remove = () => {
    if (!selected.length) return;
    change((g) => {
      const next = {
        ...g,
        nodes: g.nodes.filter((n) => !selected.includes(n.id)),
        annotations: (g.annotations || []).filter(
          (n: Any) => !selected.includes(n.id),
        ),
        edges: g.edges.filter(
          (e) =>
            !selected.includes(e.id) &&
            !selected.includes(e.source) &&
            !selected.includes(e.target),
        ),
        ...(g.final_outputs
          ? {
              final_outputs: g.final_outputs.filter(
                (id: string) => !selected.includes(id),
              ),
            }
          : {}),
      };
      if (next.final_outputs && !next.final_outputs.length)
        delete next.final_outputs;
      return next;
    });
    setSelected([]);
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (
        ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) ||
        target.isContentEditable ||
        target.closest(
          '[role="separator"], .map-content, .react-flow__resize-control, .node-size-reset',
        )
      )
        return;
      if (
        page === "canvas" &&
        !assistantBusy &&
        ["Delete", "Backspace"].includes(event.key)
      ) {
        event.preventDefault();
        remove();
      }
      if (event.key === "Escape") setSelected([]);
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [selected, page, assistantBusy]);
  const execution = useMemo(
    () =>
      executionView(
        graph,
        stack[0]?.graph || graph,
        detail,
        runId,
        stack.map((s) => s.nodeId),
        dirty,
      ),
    [graph, stack, detail, runId, dirty],
  );
  const canvasContext = [
    (stack[0]?.graph || graph)?.id,
    ...stack.map((s) => s.nodeId),
  ].join("/");
  useEffect(() => {
    if (page !== "canvas" || !canvasArea.current) return;
    let previous: { width: number; height: number } | null = null;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (previous && flowRef.current) {
        const view = flowRef.current.getViewport();
        void flowRef.current.setViewport({
          ...view,
          x: view.x + (width - previous.width) / 2,
          y: view.y + (height - previous.height) / 2,
        });
      }
      previous = { width, height };
    });
    observer.observe(canvasArea.current);
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => {
        void flowRef.current?.fitView({ padding: 0.16, duration: 0 });
      });
    });
    return () => {
      observer.disconnect();
      cancelAnimationFrame(first);
      cancelAnimationFrame(second);
    };
  }, [page, canvasContext]);
  const flowNodes = useMemo(
    () =>
      (
        graph?.nodes.map((item) => ({
          id: item.id,
          position: item.position || { x: 0, y: 0 },
          type: "capability",
          style: layout.nodes[canvasContext]?.[item.id],
          measured:
            nodeSizes.context === canvasContext
              ? nodeSizes.sizes[item.id]
              : undefined,
          selected: selected.includes(item.id),
          data: {
            customSize: !!layout.nodes[canvasContext]?.[item.id],
            onResize: (size: { width: number; height: number }) =>
              layout.setNode(canvasContext, item.id, size),
            onResetSize: () => layout.setNode(canvasContext, item.id),
            item,
            definition: def(item, types),
            asset: snapshot?.assets.find(
              (a: Any) => a.id === item.config?.asset_id,
            ),
            outputs: execution.nodes.get(item.id)?.record?.outputs,
            nodeId: [...stack.map((s) => s.nodeId), item.id].join("/"),
            runId,
            model: ai.providers?.find(
              (p: Any) => p.id === (item.config?.provider_id || ai.provider_id),
            )?.model,
            state: execution.nodes.get(item.id)?.state,
            message: execution.nodes.get(item.id)?.record?.message,
            final: (
              graph?.final_outputs ||
              detail?.final?.node_ids ||
              []
            ).includes(item.id),
          },
        })) || []
      ).concat(
        (graph?.annotations || []).map((item: Any) => ({
          id: item.id,
          position: item.position,
          type: "annotation",
          style: layout.nodes[canvasContext]?.[item.id],
          measured:
            nodeSizes.context === canvasContext
              ? nodeSizes.sizes[item.id]
              : undefined,
          selected: selected.includes(item.id),
          data: {
            customSize: !!layout.nodes[canvasContext]?.[item.id],
            onResize: (size: { width: number; height: number }) =>
              layout.setNode(canvasContext, item.id, size),
            onResetSize: () => layout.setNode(canvasContext, item.id),
            item,
            definition: undefined,
            state: undefined,
            asset: undefined,
            outputs: undefined,
            nodeId: item.id,
            runId,
            model: undefined,
            onEdit: (text: string) =>
              change((g) => ({
                ...g,
                annotations: g.annotations.map((a: Any) =>
                  a.id === item.id ? { ...a, text, private: true } : a,
                ),
              })),
            message: undefined,
            final: false,
          },
        })),
      ),
    [
      graph,
      selected,
      types,
      detail,
      execution,
      snapshot?.assets,
      ai,
      runId,
      stack,
      nodeSizes,
      canvasContext,
      layout.nodes,
    ],
  );
  const flowEdges = useMemo(
    () =>
      (graph?.edges.map((e) => {
        const status = edgeFeedback(execution, e.source, e.target);
        const color =
          status === "idle"
            ? capabilityColor(
                def(graph.nodes.find((n) => n.id === e.source) || {}, types),
              )
            : `var(--execution-${status === "active" ? "running" : status === "completed" ? "success" : status === "blocked" ? "error" : status})`;
        return {
          ...e,
          selected: selected.includes(e.id),
          type: "smoothstep",
          className: `execution-edge execution-edge-${status}`,
          animated: status === "active",
          ariaLabel: `${graph.nodes.find((n) => n.id === e.source)?.title || e.source} to ${graph.nodes.find((n) => n.id === e.target)?.title || e.target}: ${status === "active" ? "Data flowing" : status}`,
          style: {
            "--edge-execution-color": color,
            strokeWidth: status === "active" ? 2.6 : 1.8,
            stroke: color,
          } as React.CSSProperties,
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 16,
            height: 16,
            color,
          },
        };
      }) || []) as Edge[],
    [graph, types, execution, selected],
  );
  const onNodesChange = (changes: NodeChange[]) => {
    // Controlled nodes must retain ResizeObserver measurements. These are view
    // state, separate from the saved definition, so measurement never edits a draft.
    const measured = Object.fromEntries(
      changes.flatMap((c) =>
        c.type === "dimensions" &&
        c.dimensions &&
        c.dimensions.width > 0 &&
        c.dimensions.height > 0
          ? [[c.id, { width: c.dimensions.width, height: c.dimensions.height }]]
          : [],
      ),
    );
    if (Object.keys(measured).length)
      setNodeSizes((previous) => {
        const sizes = previous.context === canvasContext ? previous.sizes : {};
        const updated = Object.entries(measured).filter(
          ([id, size]) =>
            sizes[id]?.width !== size.width ||
            sizes[id]?.height !== size.height,
        );
        if (previous.context === canvasContext && !updated.length)
          return previous;
        return {
          context: canvasContext,
          sizes: { ...sizes, ...Object.fromEntries(updated) },
        };
      });
    for (const c of changes) {
      if (c.type === "select")
        setSelected((current) =>
          c.selected
            ? current.includes(c.id)
              ? current
              : [...current, c.id]
            : current.filter((id) => id !== c.id),
        );
      if (c.type === "position" && c.position)
        setGraph((current) =>
          current
            ? {
                ...current,
                nodes: current.nodes.map((n) =>
                  n.id === c.id ? { ...n, position: c.position } : n,
                ),
                annotations: (current.annotations || []).map((n: Any) =>
                  n.id === c.id ? { ...n, position: c.position } : n,
                ),
              }
            : current,
        );
    }
  };
  const connect = (c: Connection) =>
    change((g) => ({
      ...g,
      edges: [...g.edges, { ...c, id: "edge_" + crypto.randomUUID() }],
    }));
  const create = () =>
    act("Creating workflow", async () => {
      const result = await rpc("workflow.create", {
        title: newTitle,
        template,
      });
      setModal("");
      setNewTitle("");
      await open(result);
    });
  const apply = () =>
    act("Applying changes", async () => {
      if (!graph) return;
      const result = await rpc("workflow.save", { graph });
      setGraph(result.workflow);
      if (result.workflow.version !== graph.version) {
        setRunId("");
        setDetail(null);
        presentedFinal.current = "";
      }
      setOffline(result.check);
      setDirty(false);
      setModal("");
      setToast("Changes applied. No nodes have been run.");
    });
  const start = (single = false, resume = false) => {
    if (!graph?.id || dirty || stack.length) {
      setToast("Apply your workflow changes before running.");
      return;
    }
    if (runLaunch.current || (!resume && workflowRun)) return;
    const id = resume
      ? runId
      : "run_" + crypto.randomUUID().replaceAll("-", "");
    const request = { id, workflow_id: graph.id };
    runLaunch.current = request;
    setLaunchingRun(request);
    setDetail(null);
    setRunId(id);
    setPanel("runs");
    void act("Running locally", async () => {
      const result = await rpc("run.start", {
        workflow_id: graph.id,
        run_id: id,
        single,
      });
      setToast("Run " + result.state.replaceAll("_", " "));
    }).finally(() => {
      if (runLaunch.current?.id === id) runLaunch.current = null;
      setLaunchingRun((current) => (current?.id === id ? null : current));
    });
  };
  const stopWorkflow = async (run: Any) => {
    if (stopRequests.current.has(run.id)) return;
    stopRequests.current.add(run.id);
    setStoppingRunId(run.id);
    if (runId !== run.id) {
      setRunId(run.id);
      setDetail(null);
    }
    try {
      const result = await rpc("run.control", {
        run_id: run.id,
        action: "cancel",
      });
      setDetail((current) =>
        current && current.run.id === run.id
          ? { ...current, run: result }
          : current,
      );
      setSnapshot((current) =>
        current
          ? {
              ...current,
              runs: current.runs.map((item: Any) =>
                item.id === run.id ? result : item,
              ),
            }
          : current,
      );
      setToast(
        result.state === "cancelling"
          ? "Stopping workflow. Completed outputs are kept."
          : "Run " + result.state.replaceAll("_", " "),
      );
      await refresh();
    } catch (error) {
      setToast((error as Error).message);
    } finally {
      stopRequests.current.delete(run.id);
      setStoppingRunId((current) => (current === run.id ? "" : current));
    }
  };
  const selectedNode = graph?.nodes.find(
    (n) => selected.length === 1 && n.id === selected[0],
  );
  const selectedDefinition: Any = selectedNode
    ? def(selectedNode, types) || { inputs: [], outputs: [] }
    : { inputs: [], outputs: [] };
  const selectedOutput =
    selectedNode &&
    detail?.nodes.find((n: Any) => n.node_id === selectedNode.id);
  const onAIApplied = async (result: Any) => {
    const navigation = navigationRequest.current;
    await refresh();
    if (navigation !== navigationRequest.current) return;
    if (!result.workflow) return;
    if (
      Array.isArray(result.actions) &&
      !result.actions.length &&
      !result.should_run
    )
      return;
    const value = result.workflow as Graph;
    const changed =
      !graph || graph.id !== value.id || graph.version !== value.version;
    setGraph(value);
    setDirty(false);
    setStack([]);
    setSelected(result.selected || []);
    setPage("canvas");
    setSideOpen(false);
    rpc("workflow.check", { graph: value })
      .then(setOffline)
      .catch(() => {});
    if (changed) {
      setRunId("");
      setDetail(null);
    }
    setTimeout(
      () => flowRef.current?.fitView({ padding: 0.12, duration: 450 }),
      120,
    );
    if (result.should_run) {
      const id = "run_" + crypto.randomUUID().replaceAll("-", "");
      setRunId(id);
      setPanel("runs");
      rpc("run.start", { workflow_id: value.id, run_id: id })
        .then((value) => {
          setToast("Run " + value.state.replaceAll("_", " "));
          refresh();
        })
        .catch((e) => setToast(e.message));
    } else if (changed)
      setToast("Canvas updated. Changes are saved as a new version.");
  };
  const assistantProps = {
    graph: graph?.id ? graph : null,
    selected,
    runId,
    ai,
    conversationId,
    onConversation: rememberConversation,
    blankSession,
    adoptConversation,
    onApplied: onAIApplied,
    onConfigure: configureAI,
    onLocalResources: () => {
      setPage("resources");
    },
    onBusy: setAssistantBusy,
    refresh,
  };
  const importFiles = async (files: FileList | null) => {
    if (!files) return;
    await act("Reading local files", async () => {
      const result = await localFiles(files);
      if (snapshot?.ai?.auto_classify !== false)
        for (const item of result) {
          if (item.parsed?.text)
            rpc("knowledge.classify", { id: item.id })
              .then(refresh)
              .catch(() => {});
        }
      setToast(
        `${result.length} file${result.length === 1 ? "" : "s"} read locally. Review any parsing issues in Knowledge.`,
      );
    });
    if (fileInput.current) fileInput.current.value = "";
  };
  const workflows =
    snapshot?.workflows.filter((w: Any) =>
      w.title.toLowerCase().includes(search.toLowerCase()),
    ) || [];
  const activeRun = detail?.run;
  // The control belongs to the root workflow, including when editing its internals
  // or an unapplied draft. Newer detail overrides the slower snapshot for that run.
  const controlWorkflowId = stack[0]?.graph.id || graph?.id;
  const controlRuns: Any[] = (snapshot?.runs || [])
    .filter((run: Any) => run.workflow_id === controlWorkflowId)
    .map((run: Any) => (run.id === activeRun?.id ? activeRun : run));
  if (
    activeRun &&
    activeRun.workflow_id === controlWorkflowId &&
    !controlRuns.some((run) => run.id === activeRun.id)
  )
    controlRuns.unshift(activeRun);
  const workflowRun = controlRuns.find(
    (run) => run.state && !finishedRunStates.has(run.state),
  );
  const startingWorkflow =
    !!launchingRun &&
    launchingRun.workflow_id === controlWorkflowId &&
    (!workflowRun ||
      !["running", "pausing", "cancelling"].includes(workflowRun.state));
  const stoppingWorkflow =
    !!workflowRun &&
    (stoppingRunId === workflowRun.id ||
      workflowRun.state === "cancelling" ||
      workflowRun.control === "cancel");
  const showStopWorkflow = !!workflowRun || startingWorkflow;
  const newerSaved =
    graph && !stack.length
      ? snapshot?.workflows.find(
          (w: Graph) => w.id === graph.id && w.version > graph.version,
        )
      : null;

  // Constrain the displayed sizes to the available window; keep the user's
  // preferred sizes so a temporary small window does not overwrite them.
  const sidebarWidth = clamp(
    layout.panels.sidebar,
    156,
    Math.min(420, shellSize.width - 792),
  );
  const assistantWidth = clamp(
    layout.panels.assistant,
    280,
    Math.min(760, shellSize.width - sidebarWidth - 512),
  );
  const mainWidth = shellSize.width - sidebarWidth - assistantWidth - 12;
  const inspectorVisible = !!selectedNode && !sideOpen;
  const inspectorWidth = clamp(
    layout.panels.inspector,
    220,
    Math.min(760, mainWidth - 246),
  );
  const libraryWidth = clamp(
    layout.panels.library,
    220,
    Math.min(640, mainWidth - 246),
  );
  const runSizeName = panel === "answer" ? "answer" : "runs";
  const maxRunHeight = Math.max(120, splitSize.height - 186);
  const runHeight = clamp(
    expandedAnswer && panel === "answer"
      ? splitSize.height * 0.65
      : layout.panels[runSizeName],
    120,
    maxRunHeight,
  );
  useEffect(() => {
    if (!snapshot) return;
    const workspace = snapshot.workspace?.id || "local";
    if (bootedWorkspace.current === workspace) return;
    bootedWorkspace.current = workspace;
    try {
      const cid = localStorage.getItem(
        `combine.desktop.active-conversation.${workspace}`,
      );
      const item = snapshot.conversations?.find((c: Any) => c.id === cid);
      if (item)
        loadCanvas(
          snapshot.workflows.find((w: Graph) => w.id === item.workflow_id) ||
            emptyCanvas(),
          item.id,
          snapshot,
        );
    } catch {}
  }, [snapshot?.workspace?.id, !!snapshot]);

  if (!snapshot)
    return (
      <div className="startup">
        <div className="startup-brand">Combine</div>
        {error ? (
          <>
            <h2>Connect to your local workspace</h2>
            <p>{error}</p>
            <button className="button primary" onClick={refresh}>
              Try again
            </button>
          </>
        ) : (
          <>
            <Loader2 className="spin" size={25} />
            <p>Opening your local workspace…</p>
          </>
        )}
      </div>
    );

  return (
    <div className="shell" ref={shellSize.ref}>
      <aside
        className="sidebar"
        id="workspace-navigation"
        style={{ width: sidebarWidth }}
      >
        <button
          className="brand"
          disabled={assistantBusy || !!busy}
          onClick={() => setPage("canvas")}
        >
          Combine
        </button>
        <WorkspaceSpaces
          snapshot={snapshot}
          workflow={graph}
          dirty={dirty}
          disabled={assistantBusy || !!busy}
          shareRequest={shareRequest}
          onImported={async (value) => {
            const source = await refresh();
            if (source) loadCanvas(value as Graph, "", source);
          }}
        />
        <button
          className="new-conversation-button"
          disabled={assistantBusy || !!busy}
          onClick={newConversation}
        >
          <Plus size={16} />
          New conversation
        </button>
        <div className="nav-label">WORKSPACE</div>
        <nav>
          {pages.map((p) => (
            <button
              key={p.id}
              className={page === p.id ? "active" : ""}
              onClick={() => {
                if (p.id === page) return;
                if (dirty && !window.confirm("Leave unapplied changes?"))
                  return;
                if (dirty)
                  setGraph(
                    snapshot.workflows.find((w: Graph) => w.id === graph?.id) ||
                      emptyCanvas(),
                  );
                setDirty(false);
                setPage(p.id);
                if (p.id === "diagnostics")
                  rpc("workspace.diagnostics").then(setDiagnostics);
              }}
              disabled={assistantBusy || !!busy}
            >
              <p.icon size={18} />
              <span>{p.label}</span>
              {p.id === "workflows" && (
                <small>{snapshot.workflows.length}</small>
              )}
            </button>
          ))}
        </nav>
        <ConversationHistory
          conversations={snapshot.conversations || []}
          active={conversationId}
          disabled={assistantBusy || !!busy}
          onOpen={openConversation}
        />
        <div className="sidebar-bottom">
          {import.meta.env.DEV && (
            <div
              className="development-marker"
              title="Development data and API connections are separate from your installed application. Ctrl+Shift+I opens DevTools."
            >
              <strong>Development</strong>
              <small>Separate workspace · Hot reload</small>
            </div>
          )}
          <div className="local-marker">
            <span />
            <div>
              <strong>On this computer</strong>
              <small>Your workspace stays local</small>
            </div>
            <HardDrive size={16} />
          </div>
          <button className="sidebar-help" onClick={() => setModal("help")}>
            <CircleHelp size={16} /> Getting started <ArrowRight size={14} />
          </button>
          <div className="user-row">
            <span className="avatar">L</span>
            <div>
              <strong>Local workspace</strong>
              <small>No sign-in required</small>
            </div>
            <ShieldCheck size={16} />
          </div>
        </div>
      </aside>
      <ResizeDivider
        label="Resize navigation"
        controls="workspace-navigation"
        value={sidebarWidth}
        min={156}
        max={Math.min(420, shellSize.width - assistantWidth - 512)}
        onChange={(value) => layout.setPanel("sidebar", value)}
        onReset={() => layout.resetPanel("sidebar")}
      />
      <main className={`main ${page === "canvas" ? "canvas-main" : ""}`}>
        <header className="topbar">
          <div className="breadcrumbs">
            <span>Workspace</span>
            <ChevronRight size={13} />
            <strong>
              {page === "canvas"
                ? graph?.title
                : pages.find((p) => p.id === page)?.label || "Local models"}
            </strong>
          </div>
          <div className="topbar-right">
            <button
              className="icon-button reset-panel-sizes"
              aria-label="Reset panel sizes"
              title="Reset panel sizes"
              onClick={() => {
                layout.resetPanels();
                setExpandedAnswer(false);
              }}
            >
              <SlidersHorizontal size={16} />
            </button>
            <span className="desktop-label">
              <span /> Conversation workspace · v0.2
            </span>
            <button
              className="icon-button notice-button"
              aria-label="Notifications"
              onClick={() => setModal("notifications")}
            >
              <Bell size={18} />
              {snapshot.notifications.some((n: Any) => !n.read) && <i />}
            </button>
          </div>
        </header>
        {busy && (
          <div className="activity-strip">
            <Loader2 size={12} className="spin" />
            {busy}
          </div>
        )}
        {error && <div className="error-strip">{error}</div>}

        {page === "workflows" && (
          <div className="page-content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">BUILD · EDIT · REUSE</div>
                <h1>Your workflows</h1>
                <p>
                  A place for the workflows you are building and the ones you
                  use every day.
                </p>
              </div>
              <button
                className="button primary"
                onClick={() => {
                  setTemplate("blank");
                  setModal("create");
                }}
              >
                <Plus size={17} /> New workflow
              </button>
            </div>
            <div className="section-title">
              <div>
                <h2>
                  All workflows
                  <span>{snapshot.workflows.length}</span>
                </h2>
              </div>
              <label className="search-box">
                <Search size={15} />
                <input
                  placeholder="Search workflows…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
            </div>
            {workflows.length ? (
              <div className="workflow-list">
                {workflows.map((w: Graph) => {
                  const last = snapshot.runs.find(
                    (r: Any) => r.workflow_id === w.id,
                  );
                  return (
                    <div className="workflow-row" key={w.id}>
                      <button className="workflow-open" onClick={() => open(w)}>
                        <span className="workflow-symbol">
                          <GitBranch size={22} />
                        </span>
                        <div>
                          <strong>{w.title}</strong>
                          <small>
                            {w.nodes.length} nodes <span>·</span> Version{" "}
                            {w.version} <span>·</span> Edited{" "}
                            {date(w.updated_at)}
                          </small>
                        </div>
                      </button>
                      <div className="workflow-row-actions">
                        {last ? (
                          <Badge state={last.state} />
                        ) : (
                          <Badge state="draft">Not run yet</Badge>
                        )}
                        <button
                          className="icon-button"
                          aria-label={`Duplicate ${w.title}`}
                          onClick={() =>
                            act("Duplicating workflow", () =>
                              rpc("workflow.duplicate", { id: w.id }),
                            )
                          }
                        >
                          <Copy size={15} />
                        </button>
                        <button
                          className="icon-button"
                          aria-label={`Delete ${w.title}`}
                          onClick={() => {
                            if (
                              window.confirm(
                                `Delete “${w.title}”? Your original files will be preserved.`,
                              )
                            )
                              act("Deleting workflow", () =>
                                rpc("workflow.delete", { id: w.id }),
                              );
                          }}
                        >
                          <Trash2 size={15} />
                        </button>
                        <button
                          className="icon-button"
                          aria-label={`Open ${w.title}`}
                          onClick={() => open(w)}
                        >
                          <ArrowRight size={17} />
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <Empty
                title={
                  search
                    ? "No matching workflows"
                    : "Your first workflow starts here"
                }
                text={
                  search
                    ? "Try a different name."
                    : "Start from a blank canvas or use a small, runnable example."
                }
                action={
                  <button
                    className="button secondary"
                    onClick={() => setModal("create")}
                  >
                    <Plus size={15} /> Create a workflow
                  </button>
                }
              />
            )}
            <div className="section-title starter-heading">
              <div>
                <h2>A starting point, made yours</h2>
                <p>
                  These examples use sample inputs. Results are only created
                  when you run them.
                </p>
              </div>
            </div>
            <div className="starter-grid">
              {[
                {
                  id: "text",
                  icon: FileText,
                  title: "Transform text",
                  text: "Connect an input, a transformation and a document.",
                },
                {
                  id: "table",
                  icon: LayoutGrid,
                  title: "Explore a table",
                  text: "Calculate a total and visualize example CSV data.",
                },
                {
                  id: "ai",
                  icon: Sparkles,
                  title: "Build with AI",
                  text: "Use your configured API model to create useful output.",
                },
              ].map((t) => (
                <button
                  className="starter-card"
                  key={t.id}
                  onClick={() => {
                    setTemplate(t.id);
                    setNewTitle(t.title);
                    setModal("create");
                  }}
                >
                  <span className="starter-icon">
                    <t.icon size={21} />
                  </span>
                  <strong>{t.title}</strong>
                  <p>{t.text}</p>
                  <span className="starter-bottom">
                    Use this starting point <ArrowRight size={15} />
                  </span>
                </button>
              ))}
            </div>
            {!!snapshot.library.length && (
              <>
                <div className="section-title">
                  <h2>Reusable templates</h2>
                </div>
                <div className="starter-grid">
                  {snapshot.library.map((item: Any) => (
                    <button
                      className="starter-card"
                      key={item.id}
                      onClick={() =>
                        act("Creating an independent workflow", async () =>
                          open(
                            await rpc("workflow.from_library", { id: item.id }),
                          ),
                        )
                      }
                    >
                      <Layers size={22} />
                      <strong>{item.title}</strong>
                      <p>
                        Version {item.version} · creates an independent instance
                      </p>
                    </button>
                  ))}
                </div>
              </>
            )}
            <footer className="page-footer">
              <span>
                <HardDrive size={13} /> Everything in this workspace is saved on
                your computer.
              </span>
              <button onClick={configureAI}>
                AI connections <ArrowRight size={13} />
              </button>
            </footer>
          </div>
        )}

        {page === "canvas" && graph && (
          <div className="workbench">
            <div className="canvas-toolbar">
              <div className="canvas-heading">
                <button
                  className="icon-button"
                  aria-label="Back to workflows"
                  disabled={assistantBusy || !!busy}
                  onClick={() => {
                    if (!dirty || window.confirm("Leave unapplied changes?")) {
                      setPage("workflows");
                      setDirty(false);
                    }
                  }}
                >
                  <ArrowLeft size={17} />
                </button>
                <input
                  aria-label="Workflow title"
                  value={graph.title}
                  readOnly={!graph.id}
                  onChange={(e) =>
                    change((g) => ({ ...g, title: e.target.value }))
                  }
                />
                {graph.id && (
                  <span className="version-label">v{graph.version}</span>
                )}
                {newerSaved && (
                  <button
                    className="text-button saved-version-link"
                    onClick={() => open(newerSaved)}
                  >
                    Open latest v{newerSaved.version}
                  </button>
                )}
                {dirty && (
                  <span className="draft-label">Unapplied changes</span>
                )}
              </div>
              <div className="canvas-actions">
                <button
                  className="button small secondary"
                  disabled={
                    assistantBusy ||
                    !!busy ||
                    dirty ||
                    !graph.id ||
                    !!stack.length
                  }
                  title={
                    dirty
                      ? "Apply canvas changes before sharing"
                      : "Preview and publish a reusable template"
                  }
                  onClick={() => setShareRequest((value) => value + 1)}
                >
                  <Share2 size={14} />
                  Share to team
                </button>
                <button
                  className="button small secondary"
                  disabled={assistantBusy}
                  onClick={() => {
                    setSideOpen(true);
                  }}
                >
                  <Plus size={13} />
                  Add node
                </button>
                <button
                  className="icon-button"
                  aria-label="Add canvas note"
                  disabled={assistantBusy}
                  onClick={async () => {
                    if (!(await ensureCanvas())) return;
                    change((g) => ({
                      ...g,
                      annotations: [
                        ...(g.annotations || []),
                        {
                          id: "note_" + crypto.randomUUID().slice(0, 12),
                          text: "",
                          position: { x: 60, y: -180 },
                          private: true,
                        },
                      ],
                    }));
                    setTimeout(
                      () =>
                        flowRef.current?.fitView({
                          padding: 0.12,
                          duration: 250,
                        }),
                      100,
                    );
                  }}
                >
                  <StickyNote size={16} />
                </button>
                {offline && (
                  <Badge state={offline.status}>{offline.label}</Badge>
                )}
                {graph.nodes.filter(
                  (n) => n.type_id.startsWith("network.") && !n.disabled,
                ).length >= 2 && (
                  <button
                    className="button small secondary"
                    disabled={
                      assistantBusy ||
                      !!stack.length ||
                      activeRun?.state === "running"
                    }
                    onClick={() =>
                      act("Integrating research branches", async () => {
                        const value = await rpc("workflow.integrate", {
                          graph,
                        });
                        setGraph(value.graph);
                        setDirty(true);
                        setOffline(value.check);
                        setSelected(value.graph.final_outputs || []);
                        setToast(
                          "Branches connected in a draft. Preview and Apply before running.",
                        );
                        setTimeout(
                          () =>
                            flowRef.current?.fitView({
                              padding: 0.12,
                              duration: 300,
                            }),
                          100,
                        );
                      })
                    }
                  >
                    <GitMerge size={14} />
                    Integrate branches
                  </button>
                )}
                <button
                  className="button small secondary"
                  disabled={!graph.id || assistantBusy || !!busy}
                  onClick={() =>
                    act("Checking dependencies", async () =>
                      setOffline(await rpc("workflow.check", { graph })),
                    )
                  }
                >
                  <ShieldCheck size={14} /> Check
                </button>
                <button
                  className="button small secondary"
                  disabled={!graph.id || assistantBusy || !!busy}
                  onClick={() =>
                    act("Previewing changes", async () => {
                      const result = await rpc("workflow.preview", { graph });
                      setPreview(result);
                      setModal("preview");
                    })
                  }
                >
                  Preview changes
                </button>
                <button
                  className="button small secondary"
                  disabled={
                    assistantBusy || !graph.id || !dirty || !!stack.length
                  }
                  onClick={apply}
                >
                  Apply
                </button>
                <button
                  className="button small workflow-run-control"
                  data-mode={showStopWorkflow ? "stop" : "start"}
                  aria-live="polite"
                  title={
                    stoppingWorkflow
                      ? "Waiting for the current node to stop. Completed outputs are kept."
                      : showStopWorkflow
                        ? "Stop this workflow and keep completed outputs"
                        : "Start this workflow"
                  }
                  disabled={
                    showStopWorkflow
                      ? startingWorkflow || stoppingWorkflow
                      : !graph.id ||
                        !graph.nodes.length ||
                        assistantBusy ||
                        !!busy ||
                        dirty ||
                        !!stack.length
                  }
                  onClick={() =>
                    workflowRun ? void stopWorkflow(workflowRun) : start()
                  }
                >
                  {startingWorkflow || stoppingWorkflow ? (
                    <Loader2 size={14} className="spin" />
                  ) : showStopWorkflow ? (
                    <Square size={14} fill="currentColor" />
                  ) : (
                    <Play size={14} fill="currentColor" />
                  )}
                  {startingWorkflow
                    ? "Starting…"
                    : stoppingWorkflow
                      ? "Stopping…"
                      : showStopWorkflow
                        ? "Stop workflow"
                        : "Run workflow"}
                </button>
              </div>
            </div>
            {!!stack.length && (
              <div className="composite-breadcrumb">
                <Layers size={14} /> Editing composite internals{" "}
                <ChevronRight size={13} />
                <strong>{graph.title}</strong>
                <button
                  className="text-button"
                  onClick={() => {
                    const parent = stack[stack.length - 1];
                    const changed = {
                      ...parent.graph,
                      nodes: parent.graph.nodes.map((n) =>
                        n.id === parent.nodeId
                          ? {
                              ...n,
                              config: {
                                ...n.config,
                                definition: {
                                  ...n.config.definition,
                                  nodes: graph.nodes,
                                  edges: graph.edges,
                                },
                              },
                            }
                          : n,
                      ),
                    };
                    setGraph(changed);
                    setStack(stack.slice(0, -1));
                    setSelected([]);
                    setDirty(true);
                  }}
                >
                  Back to parent workflow <ArrowRight size={13} />
                </button>
              </div>
            )}
            <div className="workbench-split" ref={splitSize.ref}>
              <div
                className={`canvas-body ${assistantBusy ? "ai-is-working" : ""}`}
              >
                {sideOpen && (
                  <>
                    <aside
                      id="node-library"
                      className="capability-library"
                      style={{ width: libraryWidth }}
                    >
                      <CapabilityLibrary
                        types={types}
                        capabilities={snapshot.capabilities || []}
                        community={snapshot.community || []}
                        providers={snapshot.ai?.providers || []}
                        mode={snapshot.ai?.mode || "api"}
                        onAdd={addNode}
                        onChanged={refresh}
                        onClose={() => setSideOpen(false)}
                      />
                    </aside>
                    <ResizeDivider
                      label="Resize node library"
                      controls="node-library"
                      value={libraryWidth}
                      min={220}
                      max={Math.min(640, mainWidth - 246)}
                      onChange={(value) => layout.setPanel("library", value)}
                      onReset={() => layout.resetPanel("library")}
                    />
                  </>
                )}
                <div className="flow-area" ref={canvasArea}>
                  <button
                    className="icon-button canvas-overview-toggle"
                    aria-label={
                      showMinimap
                        ? "Hide workflow overview"
                        : "Show workflow overview"
                    }
                    title={
                      showMinimap
                        ? "Hide workflow overview"
                        : "Show workflow overview"
                    }
                    onClick={() => setShowMinimap(!showMinimap)}
                  >
                    <LayoutGrid size={15} />
                  </button>
                  <span className="canvas-region-label">Workflow canvas</span>
                  <ReactFlow
                    nodes={flowNodes}
                    onInit={(instance) => {
                      flowRef.current = instance;
                    }}
                    edges={flowEdges}
                    nodeTypes={nodeTypes}
                    onNodesChange={onNodesChange}
                    onEdgesChange={(changes) => {
                      for (const change of changes)
                        if (change.type === "select")
                          setSelected((old) =>
                            change.selected
                              ? [...new Set([...old, change.id])]
                              : old.filter((id) => id !== change.id),
                          );
                      const removed = changes
                        .filter((c) => c.type === "remove")
                        .map((c) => c.id);
                      if (removed.length)
                        change((g) => ({
                          ...g,
                          edges: g.edges.filter((e) => !removed.includes(e.id)),
                        }));
                    }}
                    onConnect={connect}
                    onEdgeDoubleClick={(_event, edge) =>
                      change((g) => ({
                        ...g,
                        edges: g.edges.filter((e) => e.id !== edge.id),
                      }))
                    }
                    onNodeDragStop={() => {
                      if (!stack.length)
                        rpc("workflow.layout", {
                          id: graph.id,
                          positions: Object.fromEntries(
                            [...graph.nodes, ...(graph.annotations || [])].map(
                              (n) => [n.id, n.position],
                            ),
                          ),
                        }).catch((e) => setToast(e.message));
                    }}
                    fitView
                    fitViewOptions={{ padding: 0.08 }}
                    minZoom={0.15}
                    maxZoom={1.8}
                    selectionOnDrag
                    selectionKeyCode="Shift"
                    multiSelectionKeyCode="Shift"
                    panOnDrag={[1, 2]}
                    deleteKeyCode={null}
                    proOptions={{ hideAttribution: true }}
                  >
                    <Background color="var(--canvas-dot)" gap={22} />
                    <Controls
                      showInteractive={false}
                      fitViewOptions={{ padding: 0.2 }}
                    />
                    {showMinimap && (
                      <MiniMap
                        style={{ width: 200, height: 120 }}
                        ariaLabel="Workflow overview. Drag to pan; scroll to zoom."
                        pannable
                        zoomable
                        offsetScale={8}
                        nodeColor={(node) =>
                          capabilityColor((node.data as Any).definition)
                        }
                        bgColor="var(--surface)"
                        maskColor="var(--minimap-mask)"
                        maskStrokeColor="var(--accent)"
                        maskStrokeWidth={1.5}
                      />
                    )}
                  </ReactFlow>
                  {!graph.nodes.length && (
                    <div className="canvas-empty">
                      <GitBranch size={32} />
                      <h3>Start with a conversation.</h3>
                      <p>
                        Describe your task in the panel on the right, or choose
                        Add node to build manually.
                      </p>
                    </div>
                  )}
                  <div className="canvas-bottom-bar">
                    <span>
                      {graph.nodes.length} nodes <b>·</b> {graph.edges.length}{" "}
                      connections
                    </span>
                    {execution.matched ? (
                      <div className="execution-progress" aria-live="polite">
                        <span>
                          {execution.completed} / {execution.total} completed
                        </span>
                        <progress
                          value={execution.completed}
                          max={execution.total || 1}
                          aria-label="Completed nodes in this canvas"
                        />
                      </div>
                    ) : (
                      <span>
                        Shift to select multiple <b>·</b> Middle mouse to pan{" "}
                        <b>·</b> Double-click a connection to remove it
                      </span>
                    )}
                  </div>
                  <details className="capability-legend">
                    <summary>Canvas legend</summary>
                    <strong>Capability colors</strong>
                    <div>
                      {Object.entries(CAPABILITIES).map(([key, value]) => (
                        <span key={key} title={value.description}>
                          <i style={{ background: `var(--cap-${key})` }} />
                          {value.label}
                        </span>
                      ))}
                    </div>
                    <strong>Execution status</strong>
                    <div className="execution-legend">
                      {[
                        "queued",
                        "running",
                        "succeeded",
                        "failed",
                        "waiting_input",
                        "paused",
                        "cancelled",
                      ].map((state) => (
                        <span key={state}>
                          <ExecutionLight state={state} />
                          {executionFeedback(state).label}
                        </span>
                      ))}
                    </div>
                  </details>
                  {!!selected.length && (
                    <div className="selection-toolbar">
                      <strong>{selected.length} selected</strong>
                      <button
                        disabled={selected.some(
                          (id) => !graph.nodes.some((n) => n.id === id),
                        )}
                        onClick={() =>
                          act("Combining nodes", async () => {
                            const result = await rpc("workflow.wrap", {
                              graph,
                              node_ids: selected,
                            });
                            setGraph(result.graph);
                            setDirty(true);
                            setSelected([]);
                          })
                        }
                      >
                        <Layers size={14} /> Combine
                      </button>
                      <button onClick={remove}>
                        <Trash2 size={14} /> Delete
                      </button>
                      <button
                        onClick={() => setSelected([])}
                        aria-label="Clear selection"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  )}
                </div>
                {inspectorVisible && (
                  <ResizeDivider
                    label="Resize node details"
                    controls="node-details"
                    direction={-1}
                    value={inspectorWidth}
                    min={220}
                    max={Math.min(760, mainWidth - 246)}
                    onChange={(value) => layout.setPanel("inspector", value)}
                    onReset={() => layout.resetPanel("inspector")}
                  />
                )}
                <aside
                  id="node-details"
                  style={{ width: inspectorWidth }}
                  className={`inspector ${!inspectorVisible ? "inspector-collapsed" : ""}`}
                >
                  <div className="panel-title">
                    <h3>
                      {selectedNode ? "Node details" : "Workflow details"}
                    </h3>
                    <SlidersHorizontal size={15} />
                  </div>
                  {selectedNode ? (
                    <>
                      <div className="inspector-section">
                        <span className="eyebrow">
                          {selectedDefinition?.category}
                        </span>
                        <h2>{selectedDefinition?.title}</h2>
                        <p>{selectedDefinition?.description}</p>
                        <label>
                          Node name
                          <input
                            value={selectedNode.title}
                            onChange={(e) =>
                              updateNode(selectedNode.id, {
                                title: e.target.value,
                              })
                            }
                          />
                        </label>
                        <div className="property-pair">
                          <span>Execution</span>
                          <strong>
                            {selectedDefinition?.network === "required"
                              ? "Internet required"
                              : "On this computer"}
                          </strong>
                        </div>
                        <div className="property-pair">
                          <span>Type version</span>
                          <strong>{selectedNode.type_version || "1"}</strong>
                        </div>
                      </div>
                      {!stack.length &&
                        selectedDefinition.outputs.length > 0 && (
                          <div className="inspector-section">
                            <button
                              className="button secondary full"
                              onClick={() =>
                                change((g) => ({
                                  ...g,
                                  final_outputs: [selectedNode.id],
                                }))
                              }
                            >
                              <Check size={14} />
                              Use as final output
                            </button>
                          </div>
                        )}
                      <div className="inspector-section">
                        <h4>Configuration</h4>
                        {selectedNode.type_id === "workflow.composite" ? (
                          <>
                            <CapabilitySource
                              source={selectedNode.config.capability_source}
                            />
                            {selectedNode.config.definition.library_version && (
                              <p>
                                Library v
                                {selectedNode.config.definition.library_version}{" "}
                                · Independent instance
                              </p>
                            )}
                            <p>
                              {selectedNode.config.definition.nodes.length}{" "}
                              internal nodes ·{" "}
                              {selectedDefinition.inputs.length} inputs ·{" "}
                              {selectedDefinition.outputs.length} outputs
                            </p>
                            <button
                              className="button secondary full"
                              onClick={() => {
                                setStack([
                                  ...stack,
                                  {
                                    graph: structuredClone(graph),
                                    nodeId: selectedNode.id,
                                  },
                                ]);
                                setGraph({
                                  ...structuredClone(
                                    selectedNode.config.definition,
                                  ),
                                  title: selectedNode.title,
                                });
                                setSelected([]);
                              }}
                            >
                              Edit internal workflow <ArrowRight size={14} />
                            </button>
                            <button
                              className="button secondary full"
                              onClick={() => setModal("save-capability")}
                            >
                              <BookOpen size={14} /> Save to capability library
                            </button>
                            <button
                              className="button secondary full"
                              onClick={() =>
                                act("Unpacking composite", async () => {
                                  const result = await rpc("workflow.unwrap", {
                                    graph,
                                    node_id: selectedNode.id,
                                  });
                                  setGraph(result.graph);
                                  setSelected([]);
                                  setDirty(true);
                                })
                              }
                            >
                              Unpack composite
                            </button>
                          </>
                        ) : (
                          Object.entries(selectedNode.config).map(
                            ([key, value]) => (
                              <label key={key}>
                                {key.replaceAll("_", " ")}
                                {key === "provider_id" ? (
                                  <select
                                    value={String(value || "")}
                                    onChange={(e) =>
                                      updateNode(selectedNode.id, {
                                        config: {
                                          ...selectedNode.config,
                                          provider_id: e.target.value,
                                        },
                                      })
                                    }
                                  >
                                    <option value="">
                                      {selectedNode.type_id.startsWith(
                                        "network.",
                                      )
                                        ? "Web research connection"
                                        : "Default AI connection"}
                                    </option>
                                    {ai.providers
                                      ?.filter(
                                        (provider: Any) =>
                                          !selectedNode.type_id.startsWith(
                                            "network.",
                                          ) ||
                                          ai.web?.providers?.some(
                                            (p: Any) => p.id === provider.id,
                                          ),
                                      )
                                      .map((provider: Any) => (
                                        <option
                                          key={provider.id}
                                          value={provider.id}
                                        >
                                          {provider.name} · {provider.model}
                                        </option>
                                      ))}
                                  </select>
                                ) : key === "asset_id" ||
                                  key === "image_asset_id" ? (
                                  <select
                                    value={String(value)}
                                    onChange={(e) =>
                                      updateNode(selectedNode.id, {
                                        config: {
                                          ...selectedNode.config,
                                          [key]: e.target.value,
                                        },
                                      })
                                    }
                                  >
                                    <option value="">
                                      Choose a local file
                                    </option>
                                    {snapshot.assets.map((asset: Any) => (
                                      <option key={asset.id} value={asset.id}>
                                        {asset.name} · {asset.parsed.status}
                                      </option>
                                    ))}
                                  </select>
                                ) : typeof value === "boolean" ? (
                                  <select
                                    value={String(value)}
                                    onChange={(e) =>
                                      updateNode(selectedNode.id, {
                                        config: {
                                          ...selectedNode.config,
                                          [key]: e.target.value === "true",
                                        },
                                      })
                                    }
                                  >
                                    <option value="true">Enabled</option>
                                    <option value="false">Disabled</option>
                                  </select>
                                ) : typeof value === "number" ? (
                                  <input
                                    type="number"
                                    value={value}
                                    onChange={(e) =>
                                      updateNode(selectedNode.id, {
                                        config: {
                                          ...selectedNode.config,
                                          [key]: Number(e.target.value),
                                        },
                                      })
                                    }
                                  />
                                ) : (
                                  <textarea
                                    rows={
                                      [
                                        "text",
                                        "csv",
                                        "instruction",
                                        "template",
                                      ].includes(key)
                                        ? 5
                                        : 2
                                    }
                                    value={String(value ?? "")}
                                    onChange={(e) =>
                                      updateNode(selectedNode.id, {
                                        config: {
                                          ...selectedNode.config,
                                          [key]: e.target.value,
                                        },
                                      })
                                    }
                                  />
                                )}
                              </label>
                            ),
                          )
                        )}
                      </div>
                      {selectedOutput && (
                        <div className="inspector-section">
                          <div className="panel-title">
                            <h4>Latest output</h4>
                            <Badge state={selectedOutput.state} />
                          </div>
                          {selectedOutput.message && (
                            <p className="warning-text">
                              {selectedOutput.message}
                            </p>
                          )}
                          {selectedOutput.partial_text && (
                            <details className="unfinished-draft">
                              <summary>
                                Unfinished draft · not passed downstream
                              </summary>
                              <pre className="document-output">
                                {selectedOutput.partial_text}
                              </pre>
                            </details>
                          )}
                          {Object.entries(selectedOutput.outputs || {}).map(
                            ([port, data]) => (
                              <div key={port}>
                                <small className="output-port">{port}</small>
                                <Output data={data as Any} />
                                <WebSources sources={(data as Any).sources} />
                              </div>
                            ),
                          )}
                          {selectedOutput.outputs?.text && (
                            <div className="inspector-output-actions">
                              <button
                                className="text-button"
                                onClick={() => {
                                  setEditText(
                                    selectedOutput.outputs?.text?.value || "",
                                  );
                                  setModal("edit-output");
                                }}
                              >
                                Edit text
                              </button>
                              <button
                                className="text-button"
                                onClick={() =>
                                  act("Archiving locally", () =>
                                    rpc("knowledge.archive", {
                                      run_id: runId,
                                      node_id: selectedNode.id,
                                    }),
                                  )
                                }
                              >
                                Archive locally
                              </button>
                            </div>
                          )}
                        </div>
                      )}
                    </>
                  ) : (
                    <>
                      <div className="inspector-section">
                        <span className="eyebrow">EDITABLE WORKFLOW</span>
                        <h2>{graph.title}</h2>
                        <p>
                          Select a node to edit its configuration, inspect its
                          ports and read its output.
                        </p>
                        <div className="property-pair">
                          <span>Version</span>
                          <strong>{graph.version}</strong>
                        </div>
                        <div className="property-pair">
                          <span>Storage</span>
                          <strong>This computer</strong>
                        </div>
                        <button
                          className="button secondary full"
                          onClick={() =>
                            act("Reading version history", async () => {
                              setHistory(
                                await rpc("workflow.history", { id: graph.id }),
                              );
                              setModal("history");
                            })
                          }
                        >
                          <Clock3 size={14} /> Version history
                        </button>
                        <button
                          className="button secondary full"
                          disabled={dirty || !!stack.length}
                          onClick={() =>
                            act("Saving reusable template", async () => {
                              await rpc("workflow.publish", { id: graph.id });
                              setToast(
                                "Template saved without file references, private inputs or old outputs.",
                              );
                            })
                          }
                        >
                          <Layers size={14} /> Save as template
                        </button>
                      </div>
                      {offline && (
                        <div className="inspector-section">
                          <h4>Result flow</h4>
                          <p>
                            {offline.delivery?.final_outputs?.length || 0} final
                            output nodes
                          </p>
                          {offline.delivery?.warnings?.map(
                            (issue: Any, i: number) => (
                              <p key={i} className="warning-text">
                                {issue.node_id ? `${issue.node_id}: ` : ""}
                                {issue.message}
                              </p>
                            ),
                          )}
                          <h4>Offline availability</h4>
                          <Badge state={offline.status}>{offline.label}</Badge>
                          {offline.validation?.map((issue: Any, i: number) => (
                            <p className="warning-text" key={i}>
                              {issue.node_id ? `${issue.node_id}: ` : ""}
                              {issue.message}
                            </p>
                          ))}
                          {offline.reasons?.map((issue: Any, i: number) => (
                            <p className="warning-text" key={i}>
                              {issue.node_id}: {issue.message}
                            </p>
                          ))}
                          {offline.status === "ready" && (
                            <p>
                              All checked nodes and dependencies are available
                              on this computer.
                            </p>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </aside>
              </div>
              {graph.id && (
                <>
                  <ResizeDivider
                    label="Resize results and run panel"
                    controls="workflow-results"
                    axis="y"
                    direction={-1}
                    value={runHeight}
                    min={120}
                    max={maxRunHeight}
                    onChange={(value) => {
                      setExpandedAnswer(false);
                      layout.setPanel(runSizeName, value);
                    }}
                    onReset={() => {
                      setExpandedAnswer(false);
                      layout.resetPanel(runSizeName);
                    }}
                  />
                  <section
                    id="workflow-results"
                    style={{ height: runHeight }}
                    className={`run-panel ${panel === "answer" ? "showing-answer" : ""} ${expandedAnswer && panel === "answer" ? "expanded-answer" : ""}`}
                  >
                    <header>
                      <div className="run-tabs">
                        {["answer", "runs", "outputs", "events"].map((tab) => (
                          <button
                            className={panel === tab ? "active" : ""}
                            key={tab}
                            onClick={() => setPanel(tab)}
                          >
                            {tab === "runs" ? (
                              <Play size={13} />
                            ) : tab === "outputs" ? (
                              <FileText size={13} />
                            ) : (
                              <Clock3 size={13} />
                            )}{" "}
                            {tab === "answer"
                              ? "Final answer"
                              : tab[0].toUpperCase() + tab.slice(1)}
                          </button>
                        ))}
                      </div>
                      <div className="run-panel-actions">
                        {activeRun && (
                          <>
                            <Badge state={activeRun.state} />
                            <span className="api-cost">
                              {detail?.nodes.some(
                                (n: Any) =>
                                  n.type_id === "model.api" ||
                                  n.type_id.startsWith("network."),
                              )
                                ? "API usage recorded · estimate unavailable"
                                : "API cost: 0 · local execution"}
                            </span>
                            {["running", "pausing"].includes(
                              activeRun.state,
                            ) ? (
                              <button
                                className="icon-button"
                                aria-label="Pause run"
                                onClick={() =>
                                  act("Pausing", () =>
                                    rpc("run.control", {
                                      run_id: runId,
                                      action: "pause",
                                    }),
                                  )
                                }
                              >
                                <Pause size={15} />
                              </button>
                            ) : (
                              ![
                                "completed",
                                "partial",
                                "failed",
                                "cancelled",
                              ].includes(activeRun.state) && (
                                <button
                                  className="text-button"
                                  onClick={() => start(false, true)}
                                >
                                  Continue
                                </button>
                              )
                            )}
                            <button
                              className="text-button"
                              disabled={
                                dirty ||
                                !!launchingRun ||
                                !!stoppingRunId ||
                                ["running", "pausing", "cancelling"].includes(
                                  activeRun.state,
                                )
                              }
                              onClick={() => start(true, true)}
                            >
                              Run one node
                            </button>
                            <button
                              className="text-button"
                              onClick={() =>
                                act("Exporting local results", async () => {
                                  const result = await rpc("run.export", {
                                    run_id: runId,
                                  });
                                  await download(
                                    result.filename,
                                    result.content,
                                  );
                                })
                              }
                            >
                              <Download size={13} /> Export
                            </button>
                          </>
                        )}
                      </div>
                    </header>
                    <div className="run-panel-content">
                      {panel === "answer" ? (
                        <FinalAnswerPanel
                          detail={detail}
                          draft={dirty}
                          expanded={expandedAnswer}
                          onExpand={() => setExpandedAnswer(!expandedAnswer)}
                          onFocus={(id) => {
                            setSelected([id]);
                            flowRef.current?.fitView({
                              nodes: [{ id }],
                              padding: 0.3,
                              duration: 300,
                            });
                          }}
                          renderOutput={(data) => <Output data={data} />}
                        />
                      ) : !detail ? (
                        <div className="run-empty">
                          <Play size={16} />
                          <span>
                            No runs yet. Apply your workflow and run it to see
                            real results here.
                          </span>
                        </div>
                      ) : panel === "events" ? (
                        detail.events.map((event: Any) => (
                          <div className="event-row" key={event.seq}>
                            <small>{date(event.created_at)}</small>
                            <code>{event.type}</code>
                            <span>{event.node_id || event.message || ""}</span>
                          </div>
                        ))
                      ) : panel === "outputs" ? (
                        <div className="outputs-grid">
                          {detail.nodes
                            .filter((n: Any) => n.state === "succeeded")
                            .map((n: Any) => (
                              <div className="output-card" key={n.id}>
                                <strong>{n.title}</strong>
                                {Object.entries(n.outputs || {}).map(
                                  ([port, data]) => (
                                    <Output key={port} data={data as Any} />
                                  ),
                                )}
                              </div>
                            ))}
                        </div>
                      ) : (
                        <>
                          <div className="run-summary">
                            <strong>{activeRun?.title}</strong>
                            <span>Version {activeRun?.version}</span>
                            <span>{activeRun?.message}</span>
                          </div>
                          {detail.nodes.map((n: Any) => (
                            <div className="execution-row" key={n.id}>
                              <ExecutionLight state={n.state} />
                              <button onClick={() => setSelected([n.node_id])}>
                                {n.title}
                              </button>
                              <small>{n.node_id}</small>
                              <Badge state={n.state} />
                              {n.reused && (
                                <span className="reuse-label">Reused</span>
                              )}
                              {n.message && (
                                <span className="warning-text">
                                  {n.message}
                                </span>
                              )}
                            </div>
                          ))}
                        </>
                      )}
                    </div>
                  </section>
                </>
              )}
            </div>
          </div>
        )}

        {page === "knowledge" && (
          <div className="page-content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">YOUR INFORMATION, ON YOUR DEVICE</div>
                <h1>Knowledge</h1>
                <p>Read, search and reuse your local source material.</p>
              </div>
              <div className="button-row">
                <button
                  className="button secondary"
                  onClick={() =>
                    act("Classifying knowledge locally", async () => {
                      for (const item of snapshot.knowledge)
                        await rpc("knowledge.classify", { id: item.id });
                      setToast(
                        "Local classification finished. Review each source status.",
                      );
                    })
                  }
                >
                  <Sparkles size={15} />
                  AI organize
                </button>
                <button
                  className="button secondary"
                  onClick={() => {
                    setNote({ title: "", content: "" });
                    setModal("note");
                  }}
                >
                  <Plus size={16} /> New note
                </button>
                <button
                  className="button primary"
                  onClick={() => fileInput.current?.click()}
                >
                  <Upload size={16} /> Import files
                </button>
              </div>
            </div>
            <div className="knowledge-search">
              <Search size={20} />
              <input
                placeholder="Search your local knowledge…"
                value={knowledgeQuery}
                onChange={(e) => {
                  setKnowledgeQuery(e.target.value);
                  if (!e.target.value) setKnowledgeHits(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter")
                    act("Searching locally", async () =>
                      setKnowledgeHits(
                        await rpc("knowledge.search", {
                          query: knowledgeQuery,
                        }),
                      ),
                    );
                }}
              />
              <button
                className="button secondary"
                onClick={() =>
                  act("Searching locally", async () =>
                    setKnowledgeHits(
                      await rpc("knowledge.search", { query: knowledgeQuery }),
                    ),
                  )
                }
              >
                Search
              </button>
            </div>
            <div className="local-info">
              <ShieldCheck size={16} />
              <p>
                Files are copied into this workspace and read on your computer.
                Your original files are preserved. Parsing issues are shown for
                each file.
              </p>
            </div>
            <div className="section-title">
              <h2>
                {knowledgeHits ? "Search results" : "Your knowledge"}{" "}
                <span>{(knowledgeHits || snapshot.knowledge).length}</span>
              </h2>
            </div>
            {(knowledgeHits || snapshot.knowledge).length ? (
              <div className="knowledge-grid">
                {(knowledgeHits || snapshot.knowledge).map((item: Any) => {
                  const asset = snapshot.assets.find(
                    (a: Any) => a.id === item.asset_id,
                  );
                  return (
                    <article className="knowledge-card" key={item.id}>
                      <div className="knowledge-icon">
                        <FileText size={20} />
                      </div>
                      <Badge state={asset?.parsed.status || "ready"}>
                        {asset ? asset.parsed.status : "Local note"}
                      </Badge>
                      <h3>{item.title}</h3>
                      {item.category_status === "classified" && (
                        <div className="knowledge-classification">
                          <span>
                            <Sparkles size={11} />
                            {item.category}
                          </span>
                          {item.tags?.slice(0, 3).map((tag: string) => (
                            <small key={tag}>{tag}</small>
                          ))}
                        </div>
                      )}
                      {item.category_status === "classifying" && (
                        <small className="classification-progress">
                          <Loader2 size={11} className="spin" />
                          Classifying locally…
                        </small>
                      )}
                      {item.category_status === "needs_local_model" && (
                        <small className="classification-pending">
                          Local AI needed for private classification
                        </small>
                      )}
                      <p>
                        {item.content.slice(0, 200)}
                        {item.content.length > 200 ? "…" : ""}
                      </p>
                      <footer>
                        <small>
                          {asset
                            ? `${bytes(asset.size)} · ${asset.parsed.parts.length} source locations`
                            : date(item.updated_at)}
                        </small>
                        <button
                          className="text-button"
                          onClick={() => {
                            rpc("knowledge.get", { id: item.id })
                              .then((value) => {
                                setNote(value);
                                setModal("note");
                              })
                              .catch((e) => setToast(e.message));
                          }}
                        >
                          Open <ArrowRight size={13} />
                        </button>
                      </footer>
                      {asset?.parsed.issues.map((issue: string, i: number) => (
                        <p className="warning-text" key={i}>
                          {issue}
                        </p>
                      ))}
                    </article>
                  );
                })}
              </div>
            ) : (
              <Empty
                title={
                  knowledgeHits
                    ? "No matching sources"
                    : "Make your knowledge useful"
                }
                text="Import local files or write a note. Your content stays in this workspace."
              />
            )}
            {snapshot.assets.some((a: Any) => !a.parsed.text) && (
              <>
                <div className="section-title">
                  <h2>Files needing attention</h2>
                </div>
                {snapshot.assets
                  .filter((a: Any) => !a.parsed.text)
                  .map((asset: Any) => (
                    <article className="attention-card" key={asset.id}>
                      <FileText size={20} />
                      <div>
                        <strong>{asset.name}</strong>
                        <p>
                          {asset.parsed.issues.join(" ") ||
                            "No readable content was extracted."}
                        </p>
                      </div>
                      <Badge state={asset.parsed.status} />
                    </article>
                  ))}
              </>
            )}
          </div>
        )}

        {page === "resources" && (
          <div className="page-content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">PREPARE ONCE · WORK OFFLINE</div>
                <h1>Optional local models</h1>
                <p>
                  Download the capabilities your workflows need. Nothing is
                  installed until you choose it.
                </p>
              </div>
              <span className="outlined-label">
                <HardDrive size={15} /> On-device execution
              </span>
            </div>
            <div className="resource-grid">
              {snapshot.resources.map((resource: Any) => (
                <article className="resource-card" key={resource.id}>
                  <div className="resource-card-top">
                    <span className="resource-icon">
                      {resource.id === "qwen3vl-4b" ? (
                        <Sparkles size={27} />
                      ) : (
                        <Zap size={27} />
                      )}
                    </span>
                    <Badge state={resource.status} />
                  </div>
                  <h2>{resource.title}</h2>
                  <p>
                    {resource.description ||
                      (resource.id === "qwen3vl-4b"
                        ? "A local text and vision model, prepared for offline work."
                        : "CPU execution on this computer.")}
                  </p>
                  <div className="resource-facts">
                    <div>
                      <span>Download</span>
                      <strong>{bytes(resource.download_size)}</strong>
                    </div>
                    <div>
                      <span>Execution</span>
                      <strong>Local CPU</strong>
                    </div>
                    <div>
                      <span>Version</span>
                      <strong title={resource.version}>
                        {resource.version?.slice(0, 12)}
                      </strong>
                    </div>
                  </div>
                  {resource.status === "downloading" && (
                    <div className="download-progress">
                      <div>
                        <i
                          style={{
                            width: `${Math.min(100, (resource.downloaded_bytes / resource.download_size) * 100)}%`,
                          }}
                        />
                      </div>
                      <small>
                        {bytes(resource.downloaded_bytes)} of{" "}
                        {bytes(resource.download_size)}
                      </small>
                    </div>
                  )}
                  {resource.message && (
                    <p className="warning-text">{resource.message}</p>
                  )}
                  <div className="resource-actions">
                    {resource.status === "ready" ? (
                      <button
                        className="button secondary"
                        onClick={() =>
                          act("Verifying local resource", () =>
                            rpc("resource.verify", { id: resource.id }),
                          )
                        }
                      >
                        <ShieldCheck size={15} /> Verify files
                      </button>
                    ) : resource.status === "downloading" ? (
                      <button
                        className="button secondary"
                        onClick={() =>
                          act("Cancelling download", () =>
                            rpc("resource.cancel", { id: resource.id }),
                          )
                        }
                      >
                        Cancel download
                      </button>
                    ) : (
                      <button
                        className="button primary"
                        onClick={() =>
                          act(`Downloading ${resource.title}`, async () => {
                            await rpc("resource.install", { id: resource.id });
                            setToast(
                              "Resource downloaded and SHA-256 verified.",
                            );
                          })
                        }
                      >
                        <Download size={16} /> Download & verify
                      </button>
                    )}
                    <small>
                      {resource.id === "qwen3vl-4b"
                        ? "Apache 2.0 · Qwen official weights"
                        : "llama.cpp official CPU release"}
                    </small>
                  </div>
                </article>
              ))}
            </div>
            <div className="local-info">
              <ShieldCheck size={18} />
              <p>
                Downloads use fixed official sources and checked SHA-256 values.
                Missing resources never trigger a cloud fallback. A workflow is
                offline ready only after its dependencies are available.
              </p>
            </div>
            <div className="optional-resource-footer">
              <p>
                API AI is the primary assistant. These models are optional for
                private and offline work.
              </p>
              <button className="button secondary" onClick={configureAI}>
                AI connections <ArrowRight size={14} />
              </button>
            </div>
          </div>
        )}

        {page === "settings" && (
          <div className="page-content settings-page">
            <div className="page-heading">
              <div>
                <div className="eyebrow">MAKE THIS WORKSPACE YOURS</div>
                <h1>Settings</h1>
                <p>Your preferences are saved on this computer.</p>
              </div>
              <button
                className="button primary"
                onClick={() =>
                  act("Saving settings", async () => {
                    await rpc("workspace.settings", {
                      values: {
                        workspace_name: settings.workspace_name,
                        theme: settings.theme,
                        output_language: settings.output_language,
                      },
                    });
                    setToast("Settings saved locally.");
                  })
                }
              >
                <Check size={16} /> Save changes
              </button>
            </div>
            <AISettings
              ai={ai}
              usage={snapshot.usage || []}
              refresh={refresh}
              onLocalResources={() => setPage("resources")}
            />
            <section className="settings-section">
              <div>
                <h3>Workspace</h3>
                <p>A name for your local space.</p>
              </div>
              <label>
                Workspace name
                <input
                  value={settings.workspace_name || ""}
                  onChange={(e) =>
                    setSettings({ ...settings, workspace_name: e.target.value })
                  }
                />
              </label>
            </section>
            <section className="settings-section">
              <div>
                <h3>Appearance</h3>
                <p>A comfortable place to focus.</p>
              </div>
              <div>
                <label>
                  Theme
                  <select
                    value={settings.theme || "dark"}
                    onChange={(e) =>
                      setSettings({ ...settings, theme: e.target.value })
                    }
                  >
                    <option value="dark">Dark</option>
                    <option value="light">Light</option>
                  </select>
                </label>
                <div className="setting-detail">
                  <span>Application font</span>
                  <strong>System UI · Segoe UI on Windows</strong>
                </div>
              </div>
            </section>
            <section className="settings-section">
              <div>
                <h3>Language</h3>
                <p>Interface and generated content are separate.</p>
              </div>
              <div>
                <label>
                  Interface language
                  <input value="English" readOnly />
                </label>
                <label>
                  Default output language
                  <select
                    value={settings.output_language || "en"}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        output_language: e.target.value,
                      })
                    }
                  >
                    <option value="en">English</option>
                    <option value="zh">Chinese</option>
                    <option value="follow_input">Follow task input</option>
                  </select>
                </label>
              </div>
            </section>
            <section className="settings-section">
              <div>
                <h3>Privacy & execution</h3>
                <p>Core boundaries apply to every node.</p>
              </div>
              <div className="privacy-list">
                <p>
                  <ShieldCheck size={16} /> Private content stays on this
                  computer.
                </p>
                <p>
                  <ShieldCheck size={16} /> No cloud fallback for local
                  resources.
                </p>
                <p>
                  <ShieldCheck size={16} /> No task budget that interrupts
                  execution.
                </p>
                <p>
                  <ShieldCheck size={16} /> Local work does not require cloud
                  sign-in.
                </p>
              </div>
            </section>
            <section className="settings-section">
              <div>
                <h3>Reset preferences</h3>
                <p>Reset appearance and language defaults.</p>
              </div>
              <button
                className="button secondary"
                onClick={() =>
                  act("Resetting preferences", () =>
                    rpc("workspace.settings", { reset: true }),
                  )
                }
              >
                Reset preferences
              </button>
            </section>
          </div>
        )}

        {page === "diagnostics" && (
          <div className="page-content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">KNOW WHAT IS RUNNING</div>
                <h1>Workspace diagnostics</h1>
                <p>Inspect local execution and keep a recovery copy.</p>
              </div>
              <button
                className="button secondary"
                onClick={() =>
                  act("Refreshing diagnostics", async () =>
                    setDiagnostics(await rpc("workspace.diagnostics")),
                  )
                }
              >
                Refresh
              </button>
            </div>
            <div className="diagnostic-grid">
              <article>
                <ShieldCheck size={22} />
                <small>Execution boundary</small>
                <h3>Windows AppContainer</h3>
                <p>
                  Private operations run without internet or loopback
                  capabilities.
                </p>
              </article>
              <article>
                <Database size={22} />
                <small>Local storage</small>
                <h3>SQLite workspace</h3>
                <p>
                  Workflows, outputs and event history stay on this computer.
                </p>
              </article>
              <article>
                <HardDrive size={22} />
                <small>Account requirement</small>
                <h3>None for local work</h3>
                <p>
                  Your files and workflows remain available without online
                  sign-in.
                </p>
              </article>
            </div>
            <div className="section-title">
              <div>
                <h2>Local backups</h2>
                <p>
                  Includes the workspace database, imported copies and exported
                  files. Models and credentials are separate.
                </p>
              </div>
              <button
                className="button primary"
                onClick={() =>
                  act("Creating a local backup", async () => {
                    const result = await rpc("workspace.backup");
                    setDiagnostics(await rpc("workspace.diagnostics"));
                    setToast(`Backup saved: ${result.name}`);
                  })
                }
              >
                <ArrowDownToLine size={16} /> Create backup
              </button>
            </div>
            {diagnostics?.backups?.length ? (
              <div className="workflow-list">
                {diagnostics.backups.map((backup: Any) => (
                  <div className="workflow-row" key={backup.id}>
                    <span className="workflow-symbol">
                      <Database size={20} />
                    </span>
                    <div className="backup-description">
                      <strong>{backup.name}</strong>
                      <small>
                        {date(backup.created_at)} · {bytes(backup.size)}
                      </small>
                    </div>
                    <button
                      className="button secondary small"
                      onClick={() => {
                        if (
                          window.confirm(
                            "Restore this backup? Current workspace records will be replaced.",
                          )
                        )
                          act("Restoring workspace", async () => {
                            await rpc("workspace.restore", {
                              id: backup.id,
                              confirm: true,
                            });
                            setDiagnostics(await rpc("workspace.diagnostics"));
                            setGraph(emptyCanvas());
                            rememberConversation("");
                            setBlankSession((value) => value + 1);
                            setStack([]);
                            setSelected([]);
                            setDirty(false);
                            setOffline(null);
                            setRunId("");
                            setToast(
                              "Workspace restored. Check local resource availability before running.",
                            );
                          });
                      }}
                    >
                      Restore
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <Empty
                title="Keep a recovery copy"
                text="Create a local backup before upgrades or substantial changes."
                icon={Database}
              />
            )}
            <div className="local-info">
              <CircleHelp size={17} />
              <p>
                This is the desktop foundation build. Model benchmarks, all 16
                offline acceptance scenarios and full nine-module acceptance
                remain tracked separately; this page does not claim they have
                all passed.
              </p>
            </div>
          </div>
        )}
      </main>
      <ResizeDivider
        label="Resize assistant"
        controls="workflow-assistant"
        direction={-1}
        value={assistantWidth}
        min={280}
        max={Math.min(760, shellSize.width - sidebarWidth - 512)}
        onChange={(value) => layout.setPanel("assistant", value)}
        onReset={() => layout.resetPanel("assistant")}
      />
      <aside
        className="conversation-dock right-chat"
        id="workflow-assistant"
        style={{ width: assistantWidth }}
        aria-label="Conversation panel"
      >
        <AssistantPanel {...assistantProps} scope={page} />
      </aside>

      <input
        ref={fileInput}
        type="file"
        multiple
        hidden
        onChange={(e) => importFiles(e.target.files)}
      />
      {toast && (
        <div className="toast" role="status">
          <span>{toast}</span>
          <button
            aria-label="Dismiss notification"
            onClick={() => setToast("")}
          >
            <X size={15} />
          </button>
        </div>
      )}
      {modal === "save-capability" &&
        selectedNode?.type_id === "workflow.composite" && (
          <Modal title="Save to capability library" close={() => setModal("")}>
            <SaveCapabilityForm
              node={selectedNode}
              onSaved={async () => {
                await refresh();
                setModal("");
                setToast(
                  "Capability saved. Add independent instances from the node library.",
                );
              }}
            />
          </Modal>
        )}
      {modal === "create" && (
        <Modal title="Create a workflow" close={() => setModal("")}>
          <p className="modal-description">
            Start with a blank canvas or an editable local example.
          </p>
          <label>
            Workflow name
            <input
              autoFocus
              placeholder="What are you working on?"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
            />
          </label>
          <label>
            Starting point
            <select
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
            >
              <option value="blank">Blank canvas</option>
              <option value="text">
                Text transformation · no model needed
              </option>
              <option value="table">Table analysis · sample CSV data</option>
              <option value="ai">API AI · your configured connection</option>
              <option value="local_ai">
                Local AI · optional downloaded model
              </option>
            </select>
          </label>
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setModal("")}>
              Cancel
            </button>
            <button className="button primary" onClick={create}>
              <Plus size={15} /> Create workflow
            </button>
          </div>
        </Modal>
      )}
      {modal === "plan" && (
        <Modal title="Create with local AI" close={() => setModal("")}>
          <p className="modal-description">
            Describe your goal. The downloaded local model will propose a typed
            graph and start it after validation. Your prompt stays on this
            computer.
          </p>
          <label>
            Your goal
            <textarea
              autoFocus
              rows={6}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="Describe the information, transformation and result you need…"
            />
          </label>
          <p className="fine-print">
            Requires the CPU runtime and local model in Resources.
          </p>
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() => {
                setModal("");
                setPage("resources");
              }}
            >
              Prepare resources
            </button>
            <button
              className="button primary"
              onClick={() =>
                act("Planning with local AI", async () => {
                  const result = await rpc("workflow.ai_plan", { goal });
                  setModal("");
                  open(result.workflow);
                  setRunId(result.run.id);
                })
              }
            >
              <Sparkles size={15} /> Generate & run
            </button>
          </div>
        </Modal>
      )}
      {modal === "preview" && preview && (
        <Modal title="Preview workflow changes" close={() => setModal("")}>
          <p className="modal-description">
            This preview is read-only. No nodes are run and no model calls are
            made.
          </p>
          <div className="preview-metrics">
            {["changed", "affected", "reused"].map((key) => (
              <div key={key}>
                <strong>{preview.impact[key].length}</strong>
                <span>{key}</span>
              </div>
            ))}
          </div>
          {preview.check.validation.map((issue: Any, i: number) => (
            <p className="warning-text" key={i}>
              {issue.node_id}: {issue.message}
            </p>
          ))}
          <Badge state={preview.check.status}>{preview.check.label}</Badge>
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setModal("")}>
              Keep editing
            </button>
            <button
              className="button primary"
              disabled={!!stack.length}
              onClick={apply}
            >
              Apply changes
            </button>
          </div>
        </Modal>
      )}
      {modal === "note" && (
        <Modal
          title={note.id ? "Local knowledge" : "New local note"}
          close={() => setModal("")}
          wide
        >
          <label>
            Title
            <input
              value={note.title}
              onChange={(e) => setNote({ ...note, title: e.target.value })}
            />
          </label>
          <label>
            Content
            <textarea
              rows={16}
              value={note.content}
              onChange={(e) => setNote({ ...note, content: e.target.value })}
            />
          </label>
          <div className="modal-actions">
            {note.id && (
              <button
                className="button danger"
                onClick={() => {
                  if (
                    window.confirm(
                      "Delete this knowledge record? Original files are preserved.",
                    )
                  )
                    act("Removing knowledge record", async () => {
                      await rpc("knowledge.delete", { id: note.id });
                      setModal("");
                      setKnowledgeHits(null);
                    });
                }}
              >
                Delete record
              </button>
            )}
            <button className="button secondary" onClick={() => setModal("")}>
              Close
            </button>
            <button
              className="button primary"
              onClick={() =>
                act("Saving local knowledge", async () => {
                  const saved = await rpc("knowledge.save", note);
                  if (snapshot.ai?.auto_classify !== false)
                    rpc("knowledge.classify", { id: saved.id })
                      .then(refresh)
                      .catch(() => {});
                  setModal("");
                  setKnowledgeHits(null);
                })
              }
            >
              Save locally
            </button>
          </div>
        </Modal>
      )}
      {modal === "edit-output" && selectedNode && (
        <Modal title="Edit this output" close={() => setModal("")} wide>
          <p className="modal-description">
            Your edit is kept locally. A later run will reuse it for this
            unchanged node and recalculate dependent outputs.
          </p>
          <textarea
            rows={18}
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
          />
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setModal("")}>
              Cancel
            </button>
            <button
              className="button primary"
              onClick={() =>
                act("Saving edited output", async () => {
                  await rpc("run.edit", {
                    run_id: runId,
                    node_id: selectedNode.id,
                    text: editText,
                  });
                  setDetail(await rpc("run.detail", { id: runId }));
                  setModal("");
                })
              }
            >
              Save edit
            </button>
          </div>
        </Modal>
      )}
      {modal === "history" && (
        <Modal title="Workflow version history" close={() => setModal("")} wide>
          <p className="modal-description">
            Saved definitions are kept as separate snapshots. Opening this
            history does not run a model.
          </p>
          {history.map((item) => (
            <div className="history-row" key={item.id}>
              <Clock3 size={16} />
              <div>
                <strong>Version {item.version}</strong>
                <p>
                  {item.nodes.length} nodes · {item.edges.length} connections
                </p>
              </div>
              <span>{date(item.updated_at)}</span>
            </div>
          ))}
        </Modal>
      )}
      {modal === "notifications" && (
        <Modal title="Notifications" close={() => setModal("")}>
          {snapshot.notifications.length ? (
            snapshot.notifications.map((notice: Any) => (
              <div className="notice-row" key={notice.id}>
                <Badge state={notice.read ? "read" : "ready"}>
                  {notice.read ? "Read" : "New"}
                </Badge>
                <div>
                  <strong>{notice.title}</strong>
                  <p>{notice.message}</p>
                  <small>{date(notice.created_at)}</small>
                </div>
              </div>
            ))
          ) : (
            <Empty
              title="You are all caught up"
              text="Real run updates will appear here."
              icon={Bell}
            />
          )}
          <div className="modal-actions">
            <button
              className="button secondary"
              onClick={() =>
                act("Marking notifications read", () =>
                  rpc("notification.read"),
                )
              }
            >
              Mark all read
            </button>
          </div>
        </Modal>
      )}
      {modal === "help" && (
        <Modal title="Getting started with Combine" close={() => setModal("")}>
          <div className="help-step">
            <span>1</span>
            <div>
              <h3>Connect your AI</h3>
              <p>
                Open Settings → AI connections. Save your API key and model ID,
                then test the actual connection.
              </p>
            </div>
          </div>
          <div className="help-step">
            <span>2</span>
            <div>
              <h3>Describe your task</h3>
              <p>
                Tell the assistant what you want. AI can answer directly or
                build and run an editable workflow.
              </p>
            </div>
          </div>
          <div className="help-step">
            <span>3</span>
            <div>
              <h3>Refine through conversation</h3>
              <p>
                Ask AI to add, remove, connect or change nodes and notes. Select
                a node to edit manually, or hold Shift to select several.
              </p>
            </div>
          </div>
          <div className="help-step">
            <span>4</span>
            <div>
              <h3>Keep making it yours</h3>
              <p>
                Read results, edit text, save templates or select nodes and
                combine them into a reusable capability.
              </p>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
