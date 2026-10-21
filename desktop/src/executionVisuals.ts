// Execution feedback is separate from capability colors and never implies success
// from a workflow's overall state. Only a matching, applied run supplies results.
type NodeRecord = {
  node_id: string;
  state: string;
  outputs?: Record<string, any>;
  message?: string;
  partial_text?: string;
};
type Graph = { id?: string; version?: number; nodes: Record<string, any>[] };
export type ExecutionDetail = {
  run: {
    id: string;
    workflow_id: string;
    version: number;
    state: string;
    title?: string;
    message?: string;
  };
  nodes: NodeRecord[];
};
export type ExecutionTone =
  | "idle"
  | "pending"
  | "running"
  | "success"
  | "error"
  | "waiting"
  | "paused"
  | "cancelled";
export function executionFeedback(state = "idle") {
  const known: Record<string, [ExecutionTone, string, string]> = {
    idle: ["idle", "Not started", "Apply and run this workflow to begin."],
    queued: ["pending", "Pending", "Waiting for its turn or upstream outputs."],
    running: [
      "running",
      "Running",
      "This node is executing. Incoming data flows along the animated connections.",
    ],
    succeeded: [
      "success",
      "Completed",
      "Execution succeeded. The output is available.",
    ],
    failed: [
      "error",
      "Failed",
      "Execution failed. Open the node for the actual error.",
    ],
    skipped: [
      "error",
      "Blocked",
      "This node was skipped; check its upstream nodes or disabled setting.",
    ],
    paused: [
      "paused",
      "Paused",
      "Execution is paused. Completed outputs are preserved.",
    ],
    cancelled: [
      "cancelled",
      "Cancelled",
      "Execution was cancelled. This node has no completed result.",
    ],
    interrupted: [
      "error",
      "Interrupted",
      "Execution stopped before completion. Resume to retry.",
    ],
    not_run: ["pending", "Not run", "This node did not execute in this run."],
    waiting_input: [
      "waiting",
      "Needs input",
      "Provide the requested input to continue.",
    ],
    waiting_upstream: [
      "waiting",
      "Waiting upstream",
      "An upstream node needs attention before this node can run.",
    ],
    waiting_resource: [
      "waiting",
      "Needs resources",
      "Prepare the missing local resource to continue.",
    ],
    waiting_connection: [
      "waiting",
      "Needs connection",
      "Configure the required connection to continue.",
    ],
    waiting_network: [
      "waiting",
      "Needs internet",
      "Restore the required network connection to continue.",
    ],
    waiting_privacy: [
      "waiting",
      "Privacy blocked",
      "Private content cannot be sent to this network node.",
    ],
  };
  const [tone, label, description] = known[state] || [
    "waiting",
    "Status unknown",
    "Check the run details before continuing.",
  ];
  return { tone, label, description };
}

export function executionView(
  graph: Graph | null,
  root: Graph | null,
  detail: ExecutionDetail | null,
  runId: string,
  prefix: string[] = [],
  dirty = false,
) {
  const matched = !!(
    !dirty &&
    root &&
    detail &&
    runId &&
    detail.run.id === runId &&
    detail.run.workflow_id === root.id &&
    detail.run.version === root.version
  );
  const runState = matched ? detail!.run.state : "";
  // A pause/cancel request lets the in-flight executor finish before stopping.
  const active = ["running", "pausing", "cancelling"].includes(runState);
  const records = new Map(
    matched ? detail!.nodes.map((n) => [n.node_id, n]) : [],
  );
  const nodes = new Map(
    (graph?.nodes || []).map((node) => {
      const path = [...prefix, node.id].join("/");
      const record = records.get(path);
      let state = record?.state;
      if (state === "running" && !active) {
        state =
          runState === "paused" || runState === "cancelled"
            ? runState
            : "interrupted";
      }
      if (!state)
        state = !matched
          ? "idle"
          : active || runState.startsWith("waiting_")
            ? "queued"
            : runState === "paused" || runState === "cancelled"
              ? runState
              : "not_run";
      return [
        String(node.id),
        { path, record, state, ...executionFeedback(state) },
      ] as const;
    }),
  );
  const completed = [...nodes.values()].filter(
    (n) => n.state === "succeeded",
  ).length;
  return { matched, nodes, completed, total: nodes.size, runState, active };
}

export type ExecutionView = ReturnType<typeof executionView>;
export function edgeFeedback(
  view: ExecutionView,
  source: string,
  target: string,
) {
  const from = view.nodes.get(source),
    to = view.nodes.get(target);
  if (!view.matched || !from || !to) return "idle";
  if (from.state === "succeeded" && to.state === "succeeded")
    return "completed";
  if (from.tone === "error" || to.tone === "error") return "blocked";
  if (view.active && from.state === "succeeded" && to.state === "running")
    return "active";
  if (from.tone === "cancelled" || to.tone === "cancelled") return "cancelled";
  if (from.tone === "paused" || to.tone === "paused") return "paused";
  if (from.tone === "waiting" || to.tone === "waiting") return "waiting";
  return "pending";
}
