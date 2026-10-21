"""Regression coverage for the 2026-10-15 system review. Offline and isolated."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.mkdtemp(prefix="combine-reliability-")
os.environ.update(DATA_DIR=TEMP, DB_PATH=str(Path(TEMP) / "combine.db"), FAKE_LLM="1",
                  COMBINE_IGNORE_DOTENV="1", WEB_SEARCH_MODE="fake", WECHAT_MODE="off", DEPLOY_MODE="self_hosted")
sys.path.insert(0, str(ROOT / "backend"))
from app import config, db, events
from app.core import matching
from app.llm import gateway
from app.services import agents, artifacts, background, inputs, kb, plans, store, tool_ops, websearch, workspace
from app.services.process_env import clean_env
from app.services.runner import Runner, external_block_reason
from app.util import new_id, now_ms


def tearDownModule():
    db.reset_connection()
    shutil.rmtree(TEMP, ignore_errors=True)


class Reliability(unittest.TestCase):
    def setUp(self):
        self.uid = new_id("usr")
        db.insert("users", dict(id=self.uid, email=self.uid + "@test.local", password_hash="unused",
                                display_name="audit", quota_monthly_cny=50, created_at=now_ms()))
        matching.seed_capabilities()
        self.runner = Runner()

    def tearDown(self):
        db.ex("DELETE FROM users WHERE id=?", (self.uid,))

    def task(self, lane="team", plan=True):
        tid = new_id("tsk")
        db.insert("tasks", dict(id=tid, user_id=self.uid, title="Same title", goal="audit", mode=lane,
                                lane=lane, status="running", budget_cny=2, created_at=now_ms(), updated_at=now_ms()))
        if plan:
            pv = dict(id=new_id("pv"), task_id=tid, version=1, status="active", reason="test", created_at=now_ms())
            db.insert("plan_versions", pv)
            pv = store.get_pv(tid, 1)
            db.update("tasks", {"id": tid}, {"active_version": 1})
            n = dict(node_key="a", title="A", description="d", acceptance=[], depends_on=[], input_refs=[],
                     produces_sections=["A"], capability_requirements={}, tools=[], agent={}, agents=None,
                     revision_instructions=[], model_tier="fast", est_complexity=1, rank=1)
            store.insert_node(pv, n, new_id("nd"))
            n = store.load_nodes(pv["id"])[0]
        else:
            pv, n = None, None
        return store.get_task(tid), pv, n

    async def execute(self, task, n, responses):
        self.runner.exec_meta["test"] = {"speculative": False}
        with patch.object(gateway, "chat", new=AsyncMock(side_effect=responses)):
            return await self.runner.execute(task, 1, n, gateway.builtin_info("fast"), [], 1, "test")

    def test_json_strings_with_braces_and_escapes(self):
        for text in ['a } b', 'example { body', 'quote " and \\ and {', '```python\nx = {"a": 1}\n```']:
            raw = json.dumps({"content": text})
            self.assertEqual(plans._json_from("```json\n" + raw + "\n```"), {"content": text})

    def test_repeated_truncation_is_not_a_deliverable(self):
        task, _, n = self.task()
        response = dict(content='{"content":"unfinished', tool_calls=[], cost=0, finish_reason="length")
        with self.assertRaises(gateway.LLMError) as err:
            asyncio.run(self.execute(task, n, [response] * 4))
        self.assertEqual(err.exception.code, "TRUNCATED_OUTPUT")

    def test_invalid_content_type_is_rejected(self):
        task, _, n = self.task()
        with self.assertRaises(gateway.LLMError) as err:
            asyncio.run(self.execute(task, n, [dict(content='{"content": {"wrong": 1}}', tool_calls=[], cost=0)]))
        self.assertEqual(err.exception.code, "INVALID_OUTPUT")

    def test_cancelled_quick_response_cannot_commit(self):
        task, _, _ = self.task("quick", False)
        async def check():
            entered, release = asyncio.Event(), asyncio.Event()
            async def chat(**kwargs):
                entered.set()
                await release.wait()
                return {"content": "late answer"}
            with patch.object(gateway, "chat", side_effect=chat):
                job = asyncio.create_task(self.runner.quick_answer(task["id"]))
                await entered.wait()
                self.runner.cancel(task["id"])
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await job
        asyncio.run(check())
        self.assertEqual(store.get_task(task["id"])["status"], "cancelled")
        self.assertIsNone(artifacts.latest(task["id"]))

    def test_cancel_owns_planning_background(self):
        task, _, _ = self.task("auto", False)
        async def check():
            entered = asyncio.Event()
            async def chat(**kwargs):
                entered.set()
                await asyncio.Future()
            with patch.object(gateway, "chat", side_effect=chat):
                job = background.start(task["id"], plans.triage_and_plan(task["id"], self.runner))
                await entered.wait()
                self.runner.cancel(task["id"])
                await asyncio.gather(job, return_exceptions=True)
        asyncio.run(check())
        self.assertEqual(store.get_task(task["id"])["status"], "cancelled")
        self.assertFalse(db.q("SELECT id FROM plan_versions WHERE task_id=?", (task["id"],)))

    def test_waiting_task_wakes_when_another_releases_provider(self):
        a, _, _ = self.task()
        b, _, bn = self.task()
        info = gateway.builtin_info("fast")
        lim = gateway.Limiter(1, 1)
        async def check():
            entered, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
            async def execute(task, *args):
                if task["id"] == a["id"]:
                    entered.set()
                    await release.wait()
                else:
                    second.set()
                return dict(content="ok", summary="ok", model="fake"), [], 0
            with patch.object(gateway, "limiter_for", return_value=lim), patch.object(self.runner, "execute", side_effect=execute):
                self.runner.schedule(a["id"])
                await entered.wait()
                self.runner.schedule(b["id"])
                release.set()
                await asyncio.wait_for(second.wait(), 1)
                while self.runner.inflight:
                    await asyncio.gather(*list(self.runner.inflight.values()))
                    await asyncio.sleep(0)
        asyncio.run(check())
        self.assertEqual(store.node_by_id(bn["id"])["status"], "succeeded")

    def test_provider_cooldown_has_a_wakeup(self):
        task, _, _ = self.task()
        lim = gateway.Limiter(1, 1)
        lim.cool_until = time.time() + 0.03
        async def check():
            started = asyncio.Event()
            async def execute(*args):
                started.set()
                return dict(content="ok", summary="ok", model="fake"), [], 0
            with patch.object(gateway, "limiter_for", return_value=lim), patch.object(self.runner, "execute", side_effect=execute):
                self.runner.schedule(task["id"])
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.gather(*list(self.runner.inflight.values()))
                await asyncio.sleep(0)
        asyncio.run(check())

    def test_edited_upstream_changes_hash_and_keeps_long_tail(self):
        task, _, n = self.task()
        n["depends_on"] = ["parent"]
        content = "x" * 4000 + "END_MARKER"
        by = {"parent": {"title": "Parent", "output": {"content": content}, "finished_at": 1}}
        before = self.runner.input_hash(task, n, by, "builtin:fast")
        with patch.object(artifacts, "active_edits", return_value={"parent": {"content": "changed", "created_at": 2}}):
            after = self.runner.input_hash(task, n, by, "builtin:fast")
        self.assertNotEqual(before, after)
        messages = self.runner.messages_for(task, n, inputs.from_nodes(n, by))
        self.assertIn("END_MARKER", json.dumps(messages))

    def test_private_attachment_and_inherited_data_block_external(self):
        task, _, n = self.task()
        n["tools"] = ["mcp.demo.echo", "reference.read"]
        self.assertEqual(external_block_reason(n, {"agents.kb_share": False}), "KB_SHARE_OFF")
        n["tools"] = ["mcp.demo.echo"]
        self.assertTrue(inputs.private_context(task, n, [{"private_context": True}]))
        n["private_context"] = True
        self.assertEqual(agents.node_tools(self.uid, n, {"agents.kb_share": False}), [])

    def test_child_process_does_not_inherit_platform_secrets(self):
        with patch.dict(os.environ, {"PLATFORM_API_KEY": "audit-only", "SECRETS_MASTER_KEY": "audit-only",
                                     "WECHAT_SECRET": "audit-only"}):
            env = clean_env()
        self.assertFalse({"PLATFORM_API_KEY", "SECRETS_MASTER_KEY", "WECHAT_SECRET"} & env.keys())
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_shell_cannot_read_outside_or_launch_a_program(self):
        outside = Path(TEMP) / "sentinel.txt"
        outside.write_text("audit-only")
        for cmd in [f'type "{outside}"', 'python -c "print(1)"', 'echo okay & whoami', 'cat ../sentinel.txt']:
            self.assertIn("error", workspace.run_command(self.uid, cmd))
        self.assertEqual(workspace.run_command(self.uid, "echo okay")["stdout"].strip(), "okay")

    def test_same_title_and_edit_get_immutable_files(self):
        task, _, _ = self.task()
        other, _, _ = self.task()
        first = artifacts._pack_result(task, "Same", "first")
        second = artifacts._pack_result(other, "Same", "second")
        edited = artifacts._pack_result(task, "Same", "edited")
        self.assertEqual(len({first["path"], second["path"], edited["path"]}), 3)
        self.assertEqual(workspace.read(self.uid, first["path"])["content"], "first")

    def test_file_tools_are_scoped_to_task_and_version(self):
        task, _, _ = self.task()
        paths = []
        for version in (1, 2):
            out = kb.run_tool("file.write", {"path": "note.md", "content": str(version)}, user_id=self.uid,
                              attachments=[], web_enabled=False, task_id=task["id"], version=version)
            paths.append(out["written"])
        self.assertNotEqual(*paths)
        self.assertEqual(workspace.read(self.uid, paths[0])["content"], "1")

    def test_event_delivery_only_after_commit_and_no_delivery_on_rollback(self):
        task, _, _ = self.task()
        queue = events.subscribe(task["id"])
        with self.assertRaises(RuntimeError):
            with db.Tx():
                db.update("tasks", {"id": task["id"]}, {"title": "rolled back"})
                events.emit(task["id"], "test.rollback")
                self.assertTrue(queue.empty())
                raise RuntimeError("fault injection")
        self.assertEqual(store.get_task(task["id"])["title"], "Same title")
        self.assertEqual(events.history(task["id"]), [])
        with db.Tx():
            events.emit(task["id"], "test.commit")
            self.assertTrue(queue.empty())
        self.assertEqual(queue.get_nowait()["seq"], 1)
        events.unsubscribe(task["id"], queue)

    def test_database_rejects_worker_thread_access(self):
        async def check():
            with self.assertRaises(Exception) as err:
                await asyncio.to_thread(db.q1, "SELECT 1")
            self.assertIn("thread", str(err.exception).lower())
        asyncio.run(check())

    def test_side_effect_receipt_reused_and_uncertain_outcome_not_replayed(self):
        task, _, _ = self.task()
        async def check():
            invoke = AsyncMock(return_value={"created": "resource"})
            for _ in range(2):
                out = await tool_ops.perform(task["id"], 1, "a", "mcp.test.create", {"id": 1}, invoke)
                self.assertEqual(out["created"], "resource")
            self.assertEqual(invoke.await_count, 1)
            failed = AsyncMock(side_effect=asyncio.CancelledError())
            with self.assertRaises(asyncio.CancelledError):
                await tool_ops.perform(task["id"], 1, "a", "mcp.test.create", {"id": 2}, failed)
            out = await tool_ops.perform(task["id"], 1, "a", "mcp.test.create", {"id": 2}, failed)
            self.assertEqual(out["error"], "OPERATION_UNCERTAIN")
            self.assertEqual(failed.await_count, 1)
        asyncio.run(check())

    def test_real_personal_model_is_used_for_planning(self):
        task, _, _ = self.task()
        info = gateway.ModelInfo("byok:test", "byok", "Test", "test", "openai", {"json"})
        with patch.object(gateway, "resolve_for_node", return_value=(info, None)):
            self.assertIs(plans.stage_model(task, "standard"), info)

    def test_real_search_override_does_not_use_fixtures(self):
        with patch.object(websearch, "_run_search", return_value={"items": [{"url": "https://test.local"}]}) as real:
            result = websearch.run("web.search", {"query": "test"}, mode_override="auto")
        real.assert_called_once()
        self.assertNotIn("note", result)

    def test_personal_model_routes_triage_plan_and_execution(self):
        task, _, _ = self.task("auto", False)
        pid, mid = new_id("prv"), new_id("mdl")
        db.insert("provider_configs", dict(id=pid, user_id=self.uid, kind="openai_compatible", source="byok",
                                           display_name="Local test", base_url="https://unused.invalid/v1", status="ok", created_at=now_ms()))
        db.insert("provider_models", dict(id=mid, provider_id=pid, user_id=self.uid, model_name="test-real-route",
                                          display_name="Test", capabilities=["json", "tools"]))
        ref = "byok:" + mid
        db.update("tasks", {"id": task["id"]}, {"model_choice": ref})
        stages = []
        def openai(info, messages, tools, json_mode, max_out):
            stages.append(info.ref)
            instruction = messages[1]["content"]
            if "Decide whether" in instruction:
                content = '{"lane":"team"}'
            elif "Break the user's goal" in instruction:
                content = json.dumps({"title": "test", "nodes": [{"node_key": "a", "title": "write", "description": "write",
                                      "produces_sections": ["Result"], "capability_requirements": {"tags": ["synthesize"], "tools": []}}]})
            else:
                content = '{"content":"complete","summary":"done"}'
            return {"content": content, "tool_calls": []}, 100, 20
        async def check():
            with patch.object(gateway, "_openai_call", side_effect=openai), patch.object(gateway.fake, "respond", side_effect=AssertionError("Unexpected demo model")):
                await plans.triage_and_plan(task["id"], self.runner)
                for _ in range(100):
                    await asyncio.sleep(.005)
                    if store.get_task(task["id"])["status"] in ("completed", "failed"):
                        break
        asyncio.run(check())
        self.assertEqual(store.get_task(task["id"])["status"], "completed")
        self.assertEqual(stages, [ref, ref, ref])
        self.assertEqual({r["source"] for r in db.q("SELECT source FROM llm_calls WHERE task_id=?", (task["id"],))}, {"byok"})

    def test_same_round_readonly_tools_overlap(self):
        import threading
        task, _, n = self.task()
        n["tools"] = ["web.search", "web.news"]
        barrier = threading.Barrier(2)
        def tool(*args, **kwargs):
            barrier.wait(timeout=2)
            return {"items": []}
        first = dict(content="", cost=0, tool_calls=[dict(id="c1", name="web_search", args={"query": "audit"}),
                                                     dict(id="c2", name="web_news", args={"query": "audit"})])
        final = dict(content='{"content":"complete"}', cost=0, tool_calls=[])
        with patch.object(websearch, "run", side_effect=tool):
            out, calls, _ = asyncio.run(self.execute(task, n, [first, final]))
        self.assertEqual(out["content"], "complete")
        self.assertEqual([c["tool"] for c in calls], ["web.search", "web.news"])

    def test_failed_graph_has_empty_quality_and_cannot_archive(self):
        task, pv, n = self.task()
        store.set_node(n["id"], status="failed", error_code="TEST_FAILED")
        nodes = store.load_nodes(pv["id"])
        self.runner.finalize(task, pv, nodes)
        artifact = artifacts.latest(task["id"])
        self.assertEqual(artifact["quality"]["level"], "empty")
        self.assertIn("未形成有效交付", artifact["markdown"])
        self.assertEqual(store.get_task(task["id"])["completion_quality"], "empty")
        with self.assertRaises(Exception) as err:
            artifacts.archive(task, None)
        self.assertEqual(err.exception.code, "EMPTY_DELIVERABLE")

    def test_inherited_private_data_removes_live_external_tools(self):
        task, _, n = self.task()
        n["tools"] = ["mcp.demo.echo"]
        offered = []
        self.runner.exec_meta["test"] = {"speculative": False}
        async def chat(**kwargs):
            offered.extend([t["function"]["name"] for t in kwargs["tools"] or []])
            return dict(content='{"content":"done"}', cost=0, tool_calls=[])
        upstream = [dict(node_key="p", title="Parent", content="private marker", private_context=True)]
        async def check():
            with patch.object(gateway, "chat", side_effect=chat), patch.object(self.runner, "invoke_tool", side_effect=AssertionError("No external execution")), \
                 patch.object(agents, "tool_index", return_value={}), patch.object(agents, "fn_map", return_value={}), \
                 patch("app.services.runner.settings_svc.get", return_value={"agents.kb_share": False}):
                return await self.runner.execute(task, 1, n, gateway.builtin_info("fast"), upstream, 1, "test")
        output, _, _ = asyncio.run(check())
        self.assertFalse(any(name.startswith("mcp") for name in offered))
        self.assertTrue(output["private_context"])

    def test_quick_answer_recovers_without_an_active_plan(self):
        task, _, _ = self.task("quick", False)
        async def check():
            self.runner.recover()
            for _ in range(100):
                await asyncio.sleep(.005)
                if store.get_task(task["id"])["status"] == "completed":
                    return
            self.fail("quick answer did not recover")
        asyncio.run(check())
        self.assertEqual(store.get_task(task["id"])["status"], "completed")

    def test_unretrieved_source_is_flagged_instead_of_linked(self):
        task, _, n = self.task()
        response = dict(content=json.dumps({"content": "[invented](https://example.org/invented)"}), cost=0, tool_calls=[])
        out, _, _ = asyncio.run(self.execute(task, n, [response]))
        self.assertEqual(out["unverified_links"], ["https://example.org/invented"])
        self.assertNotIn("](https://", out["content"])

    def test_nested_transaction_rollback_drops_only_inner_event(self):
        task, _, _ = self.task()
        queue = events.subscribe(task["id"])
        with db.Tx():
            events.emit(task["id"], "outer")
            with self.assertRaises(RuntimeError):
                with db.Tx():
                    events.emit(task["id"], "inner")
                    raise RuntimeError("rollback savepoint")
            events.emit(task["id"], "outer2")
            self.assertTrue(queue.empty())
        self.assertEqual([e["seq"] for e in events.history(task["id"])], [1, 2])
        self.assertEqual([queue.get_nowait()["type"], queue.get_nowait()["type"]], ["outer", "outer2"])
        events.unsubscribe(task["id"], queue)

    def test_tool_schema_rejects_missing_fields_and_wrong_types(self):
        from app.services import tool_args
        schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer", "minimum": 1}}, "additionalProperties": False}
        for value in ({}, {"count": "1"}, {"count": True}, {"count": 0}, {"count": 1, "extra": 2}):
            self.assertTrue(tool_args.validate(value, schema))
        self.assertEqual(tool_args.validate({"count": 1}, schema), "")


if __name__ == "__main__":
    unittest.main()
