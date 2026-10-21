import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowUp,
  Check,
  ChevronRight,
  Loader2,
  MessageSquare,
  Plus,
  ShieldCheck,
  Sparkles,
  Undo2,
} from "lucide-react";
import { rpc } from "./api";
import ModelPicker from "./ModelSelection";
import WebSources from "./WebSources";

type Any = Record<string, any>;
export function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        img: ({ alt }) => (
          <span className="remote-image-label">
            {alt || "Image reference"} · external images are not loaded
            automatically
          </span>
        ),
        a: ({ children, href }) => (
          <a href={href} target="_blank" rel="noreferrer noopener">
            {children}
          </a>
        ),
      }}
    >
      {text}
    </ReactMarkdown>
  );
}

const verbs: Record<string, string> = {
  add_node: "Node added",
  update_node: "Node updated",
  remove_node: "Node removed",
  add_edge: "Connected",
  update_edge: "Connection updated",
  remove_edge: "Connection removed",
  move_node: "Repositioned",
  add_note: "Note added",
  update_note: "Note updated",
  remove_note: "Note removed",
  wrap_nodes: "Combined",
  unwrap_node: "Expanded",
  register_capability: "New capability",
  add_capability: "Capability added",
  set_title: "Renamed",
  set_final_outputs: "Final output selected",
  highlight: "Selected",
  classify_knowledge: "Local classification",
};

export default function AssistantPanel({
  graph,
  selected,
  runId,
  ai,
  conversationId,
  onConversation,
  onApplied,
  onConfigure,
  onLocalResources,
  onBusy,
  refresh,
  home = false,
  scope = "canvas",
  blankSession = 0,
  adoptConversation = "",
}: {
  graph: Any | null;
  selected: string[];
  runId: string;
  ai: Any;
  conversationId: string;
  onConversation: (id: string) => void;
  onApplied: (result: Any) => void | Promise<void>;
  onConfigure: () => void;
  onLocalResources?: () => void;
  onBusy?: (value: boolean) => void;
  refresh: () => Promise<unknown>;
  home?: boolean;
  scope?: string;
  blankSession?: number;
  adoptConversation?: string;
}) {
  const [messages, setMessages] = useState<Any[]>([]),
    [input, updateInput] = useState(""),
    [pending, setPending] = useState(false),
    [retryable, setRetryable] = useState(false),
    [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null),
    textarea = useRef<HTMLTextAreaElement>(null),
    sendingConversation = useRef(""),
    sending = useRef(false);
  const drafts = useRef(new Map<string, string>());
  const draftKey =
    conversationId ||
    (graph?.id ? `workflow:${graph.id}` : `blank:${blankSession}`);
  const activeDraftKey = useRef(draftKey);
  activeDraftKey.current = draftKey;
  const setInput = (value: string) => {
    drafts.current.set(activeDraftKey.current, value);
    updateInput(value);
  };
  const active = ai.providers?.find((p: Any) => p.id === ai.provider_id),
    ready = ai.mode === "local" ? ai.local?.ready === true : active?.has_key;
  useEffect(() => {
    const initiatedHere =
      conversationId && conversationId === sendingConversation.current;
    sendingConversation.current = "";
    if (initiatedHere) return;
    if (sending.current) return;
    setError("");
    setRetryable(false);
    if (
      conversationId &&
      conversationId === adoptConversation &&
      !drafts.current.has(draftKey)
    )
      drafts.current.set(
        draftKey,
        drafts.current.get(`blank:${blankSession}`) || "",
      );
    updateInput(drafts.current.get(draftKey) || "");
    setMessages([]);
    if (!conversationId) {
      setMessages([]);
      return;
    }
    let alive = true;
    rpc("ai.conversation", { id: conversationId })
      .then((c) => {
        if (!alive) return;
        const loaded = c.messages || [];
        setMessages(loaded);
        const last = loaded.at(-1);
        const previous = [...loaded]
          .reverse()
          .find((m: Any) => m.role === "user");
        const legacyFormatFailure =
          last?.content ===
          "The model did not return a valid canvas command. No changes were applied.";
        // Only restore a previously shared API message. Local-only history must
        // never be moved into an API composer by an automatic recovery action.
        if (
          last?.error &&
          (last.retryable || legacyFormatFailure) &&
          previous?.scope === "api" &&
          !textarea.current?.value
        ) {
          setInput(previous.content);
          setRetryable(true);
          setError(
            "Your last message is ready to retry. Your saved canvas is preserved.",
          );
        }
      })
      .catch((e) => setError(e.message));
    return () => {
      alive = false;
    };
  }, [conversationId, blankSession, draftKey]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages, pending]);
  async function send() {
    const message = input.trim();
    if (!message || sending.current) return;
    if (!ready) {
      if (ai.mode === "local") {
        setError(
          "Local AI is not ready. Open the Local AI model menu to prepare the model and runtime. Your draft stays here; no API request was made.",
        );
        return;
      }
      onConfigure();
      return;
    }
    sending.current = true;
    setPending(true);
    onBusy?.(true);
    setError("");
    setRetryable(false);
    setInput("");
    const cid =
      conversationId ||
      "conversation_" + crypto.randomUUID().replaceAll("-", "").slice(0, 16);
    if (cid !== conversationId) sendingConversation.current = cid;
    onConversation(cid);
    setMessages((old) => [
      ...old,
      { id: "pending", role: "user", content: message },
    ]);
    try {
      const result = await rpc("ai.chat", {
        conversation_id: cid,
        request_id: "turn_" + crypto.randomUUID().replaceAll("-", ""),
        message,
        graph: graph || undefined,
        workflow_id: graph?.id,
        selected,
        run_id: runId || undefined,
        scope,
      });
      const conversation = await rpc("ai.conversation", { id: cid });
      setMessages(conversation.messages);
      if (!result.failed) await onApplied(result);
      else {
        setInput(message);
        setRetryable(Boolean(result.retryable));
        setError(
          ai.mode === "local"
            ? "Your message is restored below. Retry with Local AI; your private content stays on this computer."
            : "Your message is restored below. Try again or choose another model; your saved canvas is preserved.",
        );
      }
    } catch (e) {
      setError((e as Error).message);
      setInput(message);
    } finally {
      sending.current = false;
      setPending(false);
      onBusy?.(false);
      textarea.current?.focus();
    }
  }
  return (
    <section
      className={`assistant-panel ${home ? "assistant-home" : ""}`}
      aria-label="AI conversation"
    >
      {!home && (
        <header className="assistant-header">
          <span className="assistant-mark">
            <Sparkles size={17} />
          </span>
          <div>
            <strong>Combine AI</strong>
            <small>Workflow conversation</small>
          </div>
          <span
            className="assistant-live-dot"
            title={
              ai.mode === "local"
                ? `${ai.local?.model || "Local model"} · ${ready ? "ready on this computer" : "preparation required"}`
                : "Actual configured API model"
            }
          />
        </header>
      )}
      <div className="conversation-scroll">
        {!messages.length && (
          <div className={`conversation-welcome ${home ? "large" : ""}`}>
            <span className="welcome-orbit">
              <Sparkles size={home ? 30 : 23} />
            </span>
            <div className="eyebrow">THINK IT. SHAPE IT. MAKE IT.</div>
            <h1>
              {home ? "Your ideas, in motion." : "What are we working on?"}
            </h1>
            <p>
              {home
                ? "Tell Combine what you want to achieve. Build, refine and run a workflow through conversation."
                : graph
                  ? "Ask me to create a workflow, adjust any node, or turn a useful process into a new capability."
                  : "Describe your task. I can answer your questions or build an editable workflow on this canvas."}
            </p>
            <div className="conversation-starters">
              {(home
                ? [
                    [
                      "Write something",
                      "Help me turn an idea into a clear project proposal. Ask me what you need before drafting it.",
                    ],
                    [
                      "Build a workflow",
                      "Help me design a reusable workflow for my research project. Start by asking about my topic and desired output.",
                    ],
                    [
                      "Organize knowledge",
                      "Help me organize my local knowledge library using private on-device classification.",
                    ],
                  ]
                : !graph
                  ? [
                      [
                        "Build a workflow",
                        "Help me turn my task into a useful workflow. Start by asking what I want to achieve.",
                      ],
                      [
                        "Ask a question",
                        "I have a question. Help me explore it before deciding whether it needs a workflow.",
                      ],
                    ]
                  : [
                      [
                        "Refine this canvas",
                        "Review the current workflow and suggest a useful improvement before making changes.",
                      ],
                      [
                        "Make a capability",
                        "Help me turn part of this workflow into a reusable capability.",
                      ],
                    ]
              ).map(([title, text]) => (
                <button
                  key={title}
                  onClick={() => {
                    setInput(text);
                    textarea.current?.focus();
                  }}
                >
                  <MessageSquare size={15} />
                  <span>{title}</span>
                  <ChevronRight size={14} />
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((message) => (
          <article
            className={`chat-message ${message.role} ${message.error ? "failed" : ""}`}
            key={message.id}
          >
            {message.role === "assistant" && (
              <div className="message-author">
                <Sparkles size={13} />
                <span>Combine</span>
                <small>
                  {message.model ||
                    (message.error ? "Connection or validation issue" : "")}
                </small>
              </div>
            )}
            <div className="message-body">
              <Markdown text={message.content} />
            </div>
            {!!message.web_activity?.length && (
              <div className="web-activity">
                {message.web_activity.map((item: Any, index: number) => (
                  <span key={index}>
                    {item.tool === "web_search" ? "Web search" : "Page fetch"} ·{" "}
                    {item.state === "succeeded"
                      ? `${item.source_count} ${item.source_count === 1 ? "source" : "sources"}`
                      : "Failed"}
                  </span>
                ))}
              </div>
            )}
            <WebSources sources={message.sources} />
            {!!message.changes?.length && (
              <div className="change-receipt">
                {[...new Set<string>(message.changes)].map((op) => (
                  <span key={op}>
                    <Check size={11} />
                    {verbs[op] || op}
                  </span>
                ))}
                {message.workflow_version && <b>v{message.workflow_version}</b>}
              </div>
            )}
          </article>
        ))}
        {pending && (
          <div className="assistant-thinking" role="status">
            <Loader2 size={15} className="spin" />
            <span>
              {ai.mode === "local"
                ? "Working with Local AI…"
                : `Thinking with ${active?.name || "your model"}…`}
            </span>
          </div>
        )}
        <div ref={bottom} />
      </div>
      <div className="composer-area">
        {error && (
          <div className="chat-error" role="alert">
            <p>{error}</p>
            {retryable && (
              <button
                type="button"
                className="text-button"
                disabled={pending || !input.trim()}
                onClick={send}
              >
                Retry message
              </button>
            )}
          </div>
        )}
        {graph && (
          <div className="canvas-context">
            <span>
              <span className="context-dot" />
              {graph.nodes.length} nodes on canvas
            </span>
            <button
              disabled={pending || graph.version < 2}
              title="Restore the previous workflow version"
              onClick={async () => {
                try {
                  const result = await rpc("workflow.undo", {
                    workflow_id: graph.id,
                  });
                  onApplied({ workflow: result.workflow, should_run: false });
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              <Undo2 size={12} />
              Undo edit
            </button>
          </div>
        )}
        <div className="chat-composer">
          <textarea
            ref={textarea}
            aria-label="Message Combine AI"
            placeholder={
              home
                ? "Describe what you want to create…"
                : "Ask, add, connect, change anything…"
            }
            value={input}
            rows={home ? 3 : 2}
            disabled={pending}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                send();
              }
            }}
          />
          <div className="composer-bottom">
            <ModelPicker
              ai={ai}
              disabled={pending}
              onConfigure={onConfigure}
              onLocalResources={onLocalResources}
              refresh={refresh}
            />
            <button
              className="send-message"
              aria-label="Send message"
              onClick={send}
              disabled={pending || !input.trim()}
            >
              {pending ? (
                <Loader2 size={18} className="spin" />
              ) : (
                <ArrowUp size={20} />
              )}
            </button>
          </div>
        </div>
        <div className="composer-privacy">
          <ShieldCheck size={11} />
          <span>
            {ai.mode === "local"
              ? "On-device AI · no API request"
              : "Your API model · local files stay private"}
          </span>
        </div>
      </div>
    </section>
  );
}
