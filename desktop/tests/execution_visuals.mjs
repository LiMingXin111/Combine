import test from "node:test";
import assert from "node:assert/strict";
import {
  executionView,
  edgeFeedback,
  executionFeedback,
} from "../src/executionVisuals.ts";

const graph = {
  id: "fixture",
  version: 2,
  nodes: ["input", "analysis", "report"].map((id) => ({ id })),
};
const record = (node_id, state) => ({
  node_id,
  state,
  outputs:
    state === "succeeded"
      ? { text: { value: "Public test output" } }
      : undefined,
});
function view(states = {}, runState = "running") {
  const detail = {
    run: { id: "run", workflow_id: graph.id, version: 2, state: runState },
    nodes: Object.entries(states).map(([id, state]) => record(id, state)),
  };
  return executionView(graph, graph, detail, "run");
}

test("Unstarted nodes queue during a run; only actual successes count", () => {
  const v = view({ input: "succeeded", analysis: "running" });
  assert.equal(v.nodes.get("report").state, "queued");
  assert.equal(v.completed, 1);
  assert.equal(v.total, 3);
  assert.equal(edgeFeedback(v, "input", "analysis"), "active");
  assert.equal(edgeFeedback(v, "analysis", "report"), "pending");
});
test("A running target cannot imply data flowing from an unfinished source", () => {
  assert.equal(
    edgeFeedback(
      view({ input: "running", analysis: "running" }),
      "input",
      "analysis",
    ),
    "pending",
  );
});
test("Connections turn static green only after both endpoints succeed", () => {
  const v = view({
    input: "succeeded",
    analysis: "succeeded",
    report: "running",
  });
  assert.equal(edgeFeedback(v, "input", "analysis"), "completed");
  assert.equal(edgeFeedback(v, "analysis", "report"), "active");
  assert.equal(v.completed, 2);
});
test("Pause/cancel requests retain in-flight motion until the executor stops", () => {
  for (const state of ["pausing", "cancelling"])
    assert.equal(
      edgeFeedback(
        view({ input: "succeeded", analysis: "running" }, state),
        "input",
        "analysis",
      ),
      "active",
    );
  for (const state of ["paused", "cancelled"]) {
    const v = view({ input: "succeeded", analysis: "running" }, state);
    assert.equal(v.nodes.get("analysis").state, state);
    assert.equal(v.nodes.get("report").state, state);
    assert.equal(edgeFeedback(v, "input", "analysis"), state);
    assert.equal(v.completed, 1);
  }
});
test("Failed, blocked and interrupted nodes never count as completed", () => {
  const v = view(
    { input: "succeeded", analysis: "failed", report: "skipped" },
    "partial",
  );
  assert.equal(v.completed, 1);
  assert.equal(edgeFeedback(v, "analysis", "report"), "blocked");
  const interrupted = view({ analysis: "running" }, "completed");
  assert.equal(interrupted.nodes.get("analysis").state, "interrupted");
  assert.equal(interrupted.nodes.get("report").state, "not_run");
  assert.equal(interrupted.completed, 0);
});
test("Missing resources/input/network stay distinct from active execution", () => {
  for (const state of [
    "waiting_resource",
    "waiting_input",
    "waiting_network",
    "waiting_privacy",
    "waiting_connection",
    "waiting_upstream",
  ]) {
    const v = view({ input: "succeeded", analysis: state }, state);
    assert.equal(edgeFeedback(v, "input", "analysis"), "waiting");
    assert.equal(v.nodes.get("analysis").tone, "waiting");
    assert.equal(v.nodes.get("report").state, "queued");
  }
});
test("Drafts and mismatched run/workflow/version cannot reuse status or output", () => {
  const detail = {
    run: { id: "run", workflow_id: graph.id, version: 2, state: "running" },
    nodes: [record("input", "succeeded"), record("analysis", "running")],
  };
  const cases = [
    executionView(graph, graph, detail, "run", [], true),
    executionView(graph, graph, detail, "other"),
    executionView(graph, { ...graph, id: "other" }, detail, "run"),
    executionView(graph, { ...graph, version: 3 }, detail, "run"),
    executionView(graph, graph, null, "run"),
  ];
  for (const v of cases) {
    assert.equal(v.matched, false);
    assert.equal(v.nodes.get("input").record, undefined);
    assert.equal(v.nodes.get("analysis").state, "idle");
    assert.equal(edgeFeedback(v, "input", "analysis"), "idle");
  }
});
test("Nested composite instances resolve full paths without sharing status", () => {
  const detail = {
    run: { id: "run", workflow_id: graph.id, version: 2, state: "running" },
    nodes: [
      record("first/nested/input", "succeeded"),
      record("first/nested/analysis", "running"),
      record("second/nested/input", "failed"),
    ],
  };
  const first = executionView(graph, graph, detail, "run", ["first", "nested"]);
  const second = executionView(graph, graph, detail, "run", [
    "second",
    "nested",
  ]);
  assert.equal(edgeFeedback(first, "input", "analysis"), "active");
  assert.equal(second.nodes.get("input").state, "failed");
  assert.equal(edgeFeedback(second, "input", "analysis"), "blocked");
});
test("Every displayed state has readable English feedback independent of color", () => {
  for (const state of [
    undefined,
    "queued",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "paused",
    "cancelled",
    "interrupted",
    "not_run",
    "new_unknown_state",
  ]) {
    const f = executionFeedback(state);
    assert.ok(f.label && f.description);
    assert.doesNotMatch(f.label + f.description, /[\u3400-\u9fff]/);
  }
});
