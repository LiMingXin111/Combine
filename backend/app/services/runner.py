"""调度器与执行器（F08 / F10 / F30）。

关键规则：
- INV-01 只有依赖全部成功的节点才会派发；上游失败的节点将被标记为「已跳过」；
- INV-02 并发 ≤ min(任务上限, 服务商许可, 全局剩余)；就绪节点按关键路径优先（rank 降序）派发；
- INV-03 流程不等人：任务自输入起自动执行到最终结果；失败自动重试，耗尽后标记该步骤、
  不受影响的分支继续跑，收官时连问题清单一起交付（不因失败或问题暂停）；
  唯一可能停顿的位置是用户自己开启的安全防护（危险操作先问我，见 core/policy.py）；
- INV-04 暂停只阻止新的派发，进行中的调用允许完成；
- INV-05 输入哈希相同的成功执行可直接复用；旧版本的结果只写回旧版本节点；
- INV-15 重启时把未完成的执行标记为 orphaned，并让节点重新排队；
- 预执行（原 INV-21）：仅当安全防护开启、某步骤因危险操作等待批准时，空闲配额内可预执行
  只读下游；结果经输入哈希校验，不一致即作废（安全但不省调用）；
- 成本只做展示：任务预算不拦调用、不暂停任务（唯一硬约束是平台内置模型月度额度）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from .. import config, db, events
from ..core import policy
from ..llm import gateway
from ..util import ApiError, clip, mask_sensitive, new_id, now_ms, stable_hash
from . import agents, artifacts, background, inputs, kb, plans, store, tool_args, tool_ops, websearch
from . import settings as settings_svc

FINAL_FORMAT = (
    "Return JSON only: {\"summary\": one sentence, \"content\": markdown body of this step, "
    "\"claims\": [{\"text\": str, \"citations\": [chunk_id]}], \"questions\": [questions you need the user to answer], "
    "\"confidence\": 0-1, \"chart\": optional chart object, \"map\": optional map object}. "
    "The optional \"chart\" is {\"type\": \"bar\"|\"pie\"|\"line\", \"title\": str, \"labels\": [str], \"series\": [{\"name\": str, \"values\": [number]}]}. "
    "Provide \"chart\" ONLY when THIS step's own result is genuinely about comparable numbers (budget split, scores, time allocation, priorities): "
    "use the actual figures from your content, 2-8 labels, one series unless a real comparison needs more. Omit \"chart\" otherwise. "
    "The optional \"map\" is {\"title\": str, \"city\": str, \"markers\": [{\"name\": str, \"lat\": number, \"lng\": number, \"day\": 1-7, \"note\": str}]}. "
    "Provide \"map\" when THIS step is an itinerary, walking/transit route or location-based plan (travel, city walks, store visits): "
    "list the places in visiting order (the order IS the route), use GCJ-02 coordinates (mainland China; call map.resolve to look them up), 2-24 markers, day = which day of the trip. Omit \"map\" otherwise. "
    "Cite knowledge-base material inline as [[c:<chunk_id>]] using ONLY chunk ids returned by tools. "
    "Do not invent citations. Write in {lang}.\n"
) + (
    # v3.0 M3/M4：可选成果形态（画布功能性节点直显，prd/08 §6）——只在产出物天然匹配时给出，不硬造
    "Optional extra result shapes — include ONLY the ones that naturally match THIS step's own output (omit the rest entirely):\n"
    "\"table\": {\"title\": str, \"columns\": [{\"key\": str, \"label\": str, \"align\": \"left\"|\"right\"}], "
    "\"rows\": [[cell, ...]], \"total_rows\": int, \"note\": str} — result sets / data grids (<=12 columns, <=50 rows in the payload; put the real full count in total_rows).\n"
    "\"metrics\": {\"title\": str, \"items\": [{\"label\": str, \"value\": str, \"delta\": str, \"dir\": \"up\"|\"down\"|\"flat\", \"status\": \"good\"|\"warn\"|\"bad\"}]} — a few headline numbers (<=6). "
    "Note: in Chinese usage up=red, down=green (the component decides colors; just set dir honestly).\n"
    "\"list\": {\"title\": str, \"mode\": \"checklist\"|\"kanban\", \"items\": [{\"text\": str, \"done\": bool, \"meta\": str, \"column\": str}]} — checklists / board items (<=50; column is for kanban mode).\n"
    "\"code\": {\"title\": str, \"language\": str, \"mode\": \"code\"|\"diff\", \"code\": str, \"path\": str, \"note\": str} — code or a unified diff (<=4000 chars).\n"
    "\"image\": {\"title\": str, \"path\": str, \"caption\": str} — ONLY for an image file that THIS step wrote into the workspace with file.write (png/jpg/jpeg/svg/webp/gif).\n"
    "\"timeline\": {\"title\": str, \"items\": [{\"time\": str, \"text\": str, \"note\": str}]} — chronological milestones (<=20).\n"
    "\"calendar\": {\"title\": str, \"week_start\": str, \"events\": [{\"day\": 1-7, \"start\": \"09:00\", \"end\": \"10:00\", \"title\": str, \"note\": str}]} — a week schedule (<=21 events; day = 1-7 = Mon-Sun).\n"
    "\"status\": {\"title\": str, \"items\": [{\"name\": str, \"state\": \"ok\"|\"warn\"|\"fail\", \"detail\": str}]} — health / CI / verification results (<=12).\n"
    "\"chat\": {\"title\": str, \"messages\": [{\"who\": str, \"role\": \"user\"|\"other\", \"text\": str, \"time\": str}]} — a conversation thread (<=30 messages).\n"
    "\"web\": {\"title\": str, \"url\": str, \"excerpt\": str} — a page snapshot: the URL plus a short readable excerpt (use ONLY URLs that tools returned this step)."
)

# 工作区文件引导：仅当该步骤真正拿到 file.write 工具时附加（避免所有节点都去写文件）
FILE_TASK_HINT = (
    "This step can write files into the task workspace with the file.write tool. When the step's job is to package "
    "deliverables, treat it as a mechanical copy task: act FIRST, with minimal reasoning — the very first tool call "
    "should be file.write, copying the upstream content VERBATIM (join sections under Markdown headings; do not "
    "re-analyze, rewrite or summarize). Use a clear relative path, for example 交付/清单.md or deliverables/checklist.md. "
    "After the tool returns, reply with the required JSON whose \"content\" briefly lists which files you wrote. "
    "Do not write files for steps whose job is not packaging a deliverable."
)

# 外网检索策略（v2.1）：仅当该步骤拿到 web.* 工具时附加——教模型把搜索用到「最强」
RESEARCH_HINT = (
    "Web research protocol (the web.* tools are LIVE — use them instead of guessing):\n"
    "- Go wide, then deep: skim with web.search first, then web.fetch the 2-3 most promising URLs and READ the full "
    "text before writing — never answer from snippet titles alone.\n"
    "- For hard or ambiguous topics pass 1-2 extra query variants in `queries` (other wording, or the other "
    "language): Chinese and English queries hit different engine sets, so giving both widens coverage. Variants are "
    "searched in parallel and fused.\n"
    "- Match the channel to the question: fresh events / releases -> web.news (set freshness); papers, evidence, "
    "state-of-the-art -> web.scholar; libraries, tools, error messages -> web.code.\n"
    "- Cross-check every key fact or number in at least two independent sources; when sources disagree, say so and "
    "give both figures.\n"
    "- Cite sources inline as Markdown links [title](url) using ONLY URLs returned by the tools. Quote figures "
    "exactly as found. Never invent URLs, numbers or quotations you did not retrieve."
)

# 空白回复的纠正提示：工具检索为空时模型可能直接放弃作答，此时不能把空输出当成成功交付
EMPTY_NUDGE = (
    "Your last reply contained no body text, which is not an acceptable deliverable. "
    "Produce the step's full result now as the required JSON with a non-empty \"content\" field written in {lang}. "
    "If tools returned nothing useful, still write the best possible content from your own knowledge, "
    "and state the uncertainty explicitly inside the text instead of leaving it blank. "
    "Do not ask the user for more material in place of writing the content."
)

# 撞输出上限的纠正提示（思考模型常把预算全部烧在推理上，正文还没开始就截断）：
# 预算已翻倍，同时明确要求「收敛推理、压缩输出」，避免再次撑爆
EMPTY_NUDGE_TRUNC = (
    "Your last reply was cut off by the output limit before any body text was produced, "
    "which is not an acceptable deliverable. The output budget has been raised. "
    "Now write the step's full result as the required JSON with a non-empty \"content\" field written in {lang}. "
    "Be efficient: keep your reasoning brief and compress long passages so the JSON is complete. "
    "If tools returned nothing useful, still write the best possible content from your own knowledge, "
    "and state the uncertainty explicitly inside the text instead of leaving it blank."
)

# 半截正文的纠正提示（JSON 被截断 = 无效成果，不能把残文当成功）
TRUNC_NUDGE = (
    "Your last reply was cut off by the output limit, so the required JSON is incomplete. "
    "The output budget has been raised. Re-issue the step's complete result now as valid JSON, "
    "with \"content\" written in {lang}. Be efficient: keep your reasoning brief and compress long passages, "
    "so the whole JSON fits in the budget."
)


def external_block_reason(n: dict, s: dict) -> str:
    """该节点要调外部 Agent 却被挡下的原因（空串 = 未挡下）。v3.0 M2 的执行期守卫（prd/08 §4.4）。

    - 本节点根本不用外部工具（既没显式点 mcp.*、也没规划期 attached）→ 空串（不产生任何提示）；
    - GLOBAL_OFF：外部 Agent 总开关关闭（N4 用户约束）；
    - KB_SHARE_OFF：数据出境守卫——用户关闭「知识库随外部调用共享」，而本节点会检索知识库，
      此时一律不挂外部工具（宁可少用，不可把私域内容送出去）。
    """
    uses_ext = any(str(t).startswith("mcp.") for t in (n.get("tools") or [])) or bool((n.get("agents") or {}).get("attached"))
    if not uses_ext:
        return ""
    if not s.get("agents.enabled", True):
        return "GLOBAL_OFF"
    if not s.get("agents.kb_share", True) and (n.get("private_context") or
            set(n.get("tools") or []) & {"kb.search", "reference.read"}):
        return "KB_SHARE_OFF"
    return ""


class Runner:
    def __init__(self) -> None:
        self.inflight: dict[str, asyncio.Task] = {}
        self.adopters: dict[str, set[str]] = {}          # execution_id → 共享该执行结果的节点（跨版本沿用）
        self.task_execs: dict[str, set[str]] = {}
        self.exec_meta: dict[str, dict] = {}
        self.step_budget: dict[str, int] = {}
        self.out_budget_hint: dict[str, int] = {}     # task_id|node_key → 已扩容的输出预算（截断后跨尝试沿用）
        self._pending: set[str] = set()
        self.global_running = 0
        self.waiting: dict[str, None] = {}
        self._wake_timer = None

    def wake_waiters(self) -> None:
        if self._wake_timer:
            self._wake_timer.cancel()
            self._wake_timer = None
        waiting = list(self.waiting)
        self.waiting.clear()
        for task_id in waiting:
            self.kick(task_id)

    def wait_for_capacity(self, task_id: str, limiter=None) -> None:
        self.waiting.setdefault(task_id, None)
        if limiter and limiter.cool_until > time.time():
            delay = max(0.05, limiter.cool_until - time.time())
            loop = asyncio.get_running_loop()
            if not self._wake_timer or self._wake_timer.when() > loop.time() + delay:
                if self._wake_timer:
                    self._wake_timer.cancel()
                self._wake_timer = loop.call_later(delay, self.wake_waiters)

    # ---------------- 调度 ----------------
    def kick(self, task_id: str) -> None:
        """事件驱动调度：同一轮事件循环内的多次触发合并为一次 schedule。"""
        if task_id in self._pending:
            return
        self._pending.add(task_id)
        asyncio.get_running_loop().call_soon(self._run_schedule, task_id)

    def _run_schedule(self, task_id: str) -> None:
        self._pending.discard(task_id)
        try:
            self.schedule(task_id)
        except Exception as e:     # noqa: BLE001  调度异常不能让事件循环崩溃
            events.emit(task_id, "scheduler.error", {"message": clip(str(e), 200)})

    def running_count(self, task_id: str) -> int:
        return len(self.task_execs.get(task_id, set()))

    @db.atomic
    def _settle(self, task, pv, nodes) -> bool:
        """收官判定（不停机的失败策略）。

        把「因上游失败/跳过而永远无法就绪」的 pending 步骤标记为 skipped（沿依赖传递），
        然后只要所有步骤都到了终态就照常收官——即使有失败，也用已完成部分产出结果，
        问题清单随成果一起给出，而不是把任务挂在那里等用户处理。
        """
        by = {n["node_key"]: n for n in nodes}
        changed = True
        while changed:
            changed = False
            for n in nodes:
                if n["status"] != "pending":
                    continue
                if any(by[d]["status"] in ("failed", "skipped", "cancelled") for d in n["depends_on"]):
                    store.set_node(n["id"], status="skipped", spec_state=None)
                    n["status"] = "skipped"
                    n["spec_state"] = None
                    events.emit(task["id"], "node.skipped", {"reason": "UPSTREAM_FAILED"}, n["node_key"], pv["version"])
                    changed = True
        if nodes and all(n["status"] in ("succeeded", "failed", "skipped", "cancelled") for n in nodes):
            self.finalize(task, pv, nodes)
            return True
        return False

    @db.atomic
    def schedule(self, task_id: str) -> None:
        self.waiting.pop(task_id, None)
        t = db.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not t or t["status"] != "running":
            return
        task = store.get_task(task_id)
        pv = store.active_pv(task)
        if not pv:
            return
        nodes = store.load_nodes(pv["id"])
        if nodes and not self.running_count(task_id) and self._settle(task, pv, nodes):
            return
        if task["paused"]:
            return
        s = settings_svc.get(task["user_id"])
        by = {n["node_key"]: n for n in nodes}
        ready = [n for n in nodes if n["status"] == "pending" and n.get("spec_state") != "running"
                 and all(by[d]["status"] == "succeeded" for d in n["depends_on"])]
        rank = {n["node_key"]: n["rank"] for n in nodes}
        from ..core.graph import priority_key
        pk = priority_key(nodes, rank)
        ready.sort(key=lambda n: pk(n["node_key"]))                              # R30.4 关键路径优先
        limit = task["max_concurrency"]
        step = self.step_budget.get(task_id)
        for n in ready:
            if self.running_count(task_id) >= limit:
                break
            if self.global_running >= config.GLOBAL_MAX_CONCURRENCY:
                self.wait_for_capacity(task_id)
                break
            if step is not None and step <= 0:
                break
            info, sub = gateway.resolve_for_node(task["user_id"], task, n, s)
            if info is None:
                store.set_node(n["id"], status="failed", error_code="NO_CAPABLE_MODEL",
                               error=f"没有满足能力要求的模型（{sub['requested']}：{sub['reason']}）")
                events.emit(task_id, "node.failed", {"code": "NO_CAPABLE_MODEL", **sub}, n["node_key"], pv["version"])
                self.kick(task_id)
                continue
            lim = gateway.limiter_for(info)
            if not lim.try_acquire():
                self.wait_for_capacity(task_id, lim)
                continue                                    # 该服务商满了，试下一个就绪节点
            if sub:
                events.emit(task_id, "model.substituted", sub, n["node_key"], pv["version"])
            self.dispatch(task, pv, nodes, n, info, speculative=False)
            if step is not None:
                step -= 1
                self.step_budget[task_id] = step
        if step is not None and step <= 0 and self.step_budget.get(task_id) is not None:
            self.step_budget.pop(task_id, None)
            db.update("tasks", {"id": task_id}, {"paused": 1})
            events.emit(task_id, "run.paused", {"reason": "step"})
            return
        # R30.6 预执行：只使用空闲配额
        if s.get("execution.speculative", True) and not self.waiting and self.running_count(task_id) < limit:
            for n, gate in self.speculative_candidates(nodes, by, s):
                if self.running_count(task_id) >= limit or self.global_running >= config.GLOBAL_MAX_CONCURRENCY:
                    break
                info, sub = gateway.resolve_for_node(task["user_id"], task, n, s)
                if info is None or not gateway.limiter_for(info).try_acquire():
                    continue
                self.dispatch(task, pv, nodes, n, info, speculative=True, gate=gate)

    def speculative_candidates(self, nodes, by, s):
        maxc = s.get("execution.speculative_max_cny", 0.10)
        for g in nodes:
            if g["status"] != "awaiting_approval":
                continue
            for v in nodes:
                if g["node_key"] not in v["depends_on"] or v["status"] != "pending" or v.get("spec_state"):
                    continue
                if any(by[p]["status"] != "succeeded" for p in v["depends_on"] if p != g["node_key"]):
                    continue
                if any(policy.risk_of(t) != "read" for t in v["tools"]):
                    continue                                    # 预执行永远不产生副作用：只读
                if plans.node_cost(v) > maxc:
                    continue
                yield v, g

    # ---------------- 派发与执行 ----------------
    def input_hash(self, task: dict, n: dict, by: dict, model_ref: str) -> str:
        ups = inputs.effective_upstream(task, inputs.from_nodes(n, by))
        # Completion timestamps decide edit precedence but are not semantic inputs.
        ups = [{k: v for k, v in u.items() if k != "finished_at"} for u in ups]
        s = settings_svc.get(task["user_id"])
        return stable_hash({"goal": task["goal"], "title": n["title"], "desc": n["description"], "acc": n["acceptance"],
                            "ins": n.get("revision_instructions"), "ups": ups, "model": model_ref, "tools": n["tools"],
                            "lang": task["output_language"], "agent": n.get("agent"), "attachments": task.get("attachments"),
                            "external": n.get("agents"), "settings": {k: v for k, v in s.items() if k.startswith(("tools.", "agents.", "knowledge."))},
                            "preferences": db.q("SELECT statement FROM preferences WHERE user_id=? AND status='active' ORDER BY id", (task["user_id"],))})

    @db.atomic
    def dispatch(self, task, pv, nodes, n, info, *, speculative: bool, gate: dict | None = None) -> None:
        by = {x["node_key"]: x for x in nodes}
        ih = self.input_hash(task, n, by, info.ref)
        if not speculative:                                  # INV-05：输入哈希一致 → 复用，不再调用模型
            prev = db.q1("SELECT * FROM executions WHERE task_id=? AND node_key=? AND input_hash=? AND status='succeeded' "
                         "ORDER BY finished_at DESC LIMIT 1", (task["id"], n["node_key"], ih))
            if prev:
                gateway.limiter_for(info).release()
                out = db.j(prev["output"], {})
                store.set_node(n["id"], status="succeeded", output=out, execution_id=prev["id"], input_hash=ih,
                               finished_at=now_ms(), started_at=now_ms(), error=None, error_code=None)
                events.emit(task["id"], "node.reused", {"execution_id": prev["id"]}, n["node_key"], pv["version"])
                self.kick(task["id"])
                return
        attempt = n["attempts"] + 1
        eid = new_id("ex")
        db.insert("executions", {"id": eid, "task_id": task["id"], "node_key": n["node_key"], "plan_version": pv["version"],
                                 "attempt": attempt, "status": "running", "speculative": int(speculative), "input_hash": ih,
                                 "model_used": info.display, "started_at": now_ms()})
        if speculative:
            store.set_node(n["id"], spec_state="running")
            events.emit(task["id"], "node.speculative_started", {"gate": gate["node_key"], "model": info.display}, n["node_key"], pv["version"])
        else:
            store.set_node(n["id"], status="running", attempts=attempt, started_at=now_ms(), input_hash=ih, error=None, error_code=None)
            events.emit(task["id"], "node.started", {"attempt": attempt, "agent": n["agent"].get("name"), "model": info.display,
                                                     "model_source": info.source, "rank": n["rank"], "execution_id": eid},
                        n["node_key"], pv["version"])
        self.adopters[eid] = {n["id"]}
        self.task_execs.setdefault(task["id"], set()).add(eid)
        self.exec_meta[eid] = {"task_id": task["id"], "node_key": n["node_key"], "speculative": speculative, "info": info,
                               "gate": gate["node_key"] if gate else None, "version": pv["version"], "hash": ih}
        self.global_running += 1
        upstream = inputs.effective_upstream(task, inputs.from_nodes(n, by))
        self.inflight[eid] = asyncio.create_task(self._run(eid, task, pv["version"], dict(n), info, upstream, attempt))

    async def _run(self, eid, task, version, n, info, upstream, attempt) -> None:
        meta = self.exec_meta[eid]
        s = settings_svc.get(task["user_id"])
        retries = s.get("execution.max_retries", 1)
        tries = 0                                            # 本次执行内的自动重试次数（与历史手动重试无关）
        try:
            while True:
                try:
                    out, calls, cost = await self.execute(task, version, n, info, upstream, attempt, eid)
                    break
                except gateway.LLMError as e:
                    if tries < retries and e.code not in ("AUTH_FAILED", "MODEL_NOT_FOUND", "OPERATION_UNCERTAIN"):
                        tries += 1
                        events.emit(task["id"], "node.retrying", {"code": e.code, "message": e.message, "attempt": attempt},
                                    n["node_key"], version)
                        attempt += 1
                        db.update("executions", {"id": eid}, {"attempt": attempt})
                        await asyncio.sleep(0.5 * (config.FAKE_LATENCY_SCALE if config.FAKE_LLM else 2))
                        continue
                    raise
            background.ensure_active(task["id"])
            with db.Tx():
                db.update("executions", {"id": eid}, {"status": "succeeded", "output": out, "tool_calls": calls,
                                                      "cost_cny": cost, "finished_at": now_ms()})
                self._on_success(eid, task, version, n, out)
        except asyncio.CancelledError:
            db.update("executions", {"id": eid}, {"status": "cancelled", "finished_at": now_ms()})
            for nid in self.adopters.get(eid, ()):
                cur = store.node_by_id(nid)
                if not cur:
                    continue
                if meta.get("speculative"):
                    if cur.get("spec_state") == "running":
                        store.set_node(nid, spec_state=None)
                elif cur["status"] == "running":
                    store.set_node(nid, status="pending" if not meta.get("cancel_final") else "cancelled")
            raise
        except policy.ToolApprovalRequired as e:            # 安全闸门：危险操作未获批准 → 挂起，等用户决定
            if meta.get("speculative"):
                self._discard(eid, task["id"], n["node_key"], version, "tool_gate")
                return
            self._suspend_for_tool(task, version, e, eid)
        except gateway.BudgetExceeded as e:                 # 平台内置模型月度额度用完：节点失败并可见（任务不暂停）
            if meta.get("speculative"):                      # 预执行是「顺便」做的：作废即可
                self._discard(eid, task["id"], n["node_key"], version, "quota")
                return
            self.fail_execution(eid, task, version, n, e.code, e.message, quota=True)
        except gateway.LLMError as e:
            self.fail_execution(eid, task, version, n, e.code, e.message)
        except Exception as e:      # noqa: BLE001  未预期异常：记录并让节点可重试
            self.fail_execution(eid, task, version, n, "INTERNAL", clip(str(e), 300))
        finally:
            meta["info"] and gateway.limiter_for(meta["info"]).release()
            self.global_running = max(0, self.global_running - 1)
            self.inflight.pop(eid, None)
            self.task_execs.get(task["id"], set()).discard(eid)
            self.adopters.pop(eid, None)
            self.exec_meta.pop(eid, None)
            self.wake_waiters()
            if db.q1("SELECT id FROM tasks WHERE id=?", (task["id"],)):
                self.kick(task["id"])

    @db.atomic
    def fail_execution(self, eid, task, version, n, code, message, quota=False):
        db.update("executions", {"id": eid}, {"status": "failed", "error_code": code, "error": message, "finished_at": now_ms()})
        if quota:
            events.emit(task["id"], "budget.exceeded", {"code": code, "message": message}, n["node_key"], version)
        self._on_fail(eid, task, version, n, code, message)

    def messages_for(self, task, n, upstream) -> list[dict]:
        prefs = [p["statement"] for p in db.q("SELECT statement FROM preferences WHERE user_id=? AND status='active'", (task["user_id"],))]
        ins = [i["text"] for i in n.get("revision_instructions") or [] if isinstance(i, dict) and i.get("kind") != "rerun"]
        ups = []
        for u in inputs.effective_upstream(task, upstream):
            ups.append({"title": u["title"] + ("（用户已手工修改）" if u["user_edited"] else ""),
                        "content": u["content"], "data": u.get("data", {})})
        data = {"goal": task["goal"], "step": {"title": n["title"], "description": n["description"], "acceptance": n["acceptance"],
                                               "sections": n["produces_sections"]},
                "upstream": ups}
        msgs = [{"role": "system", "content": plans.SYSTEM_PREAMBLE},
                {"role": "system", "content": ((n.get("agent") or {}).get("system_prompt") or "") + "\n"
                 + FINAL_FORMAT.replace("{lang}", plans.LANG_NAME[task["output_language"]])},
                {"role": "user", "content": "<data>" + json.dumps(data, ensure_ascii=False) + "</data>"}]
        if "file.write" in (n.get("tools") or []):
            msgs.append({"role": "system", "content": FILE_TASK_HINT})
        if websearch.available() and any(str(t).startswith("web.") for t in (n.get("tools") or [])):
            msgs.append({"role": "system", "content": RESEARCH_HINT})
        if prefs:
            msgs.append({"role": "system", "content": "User's confirmed preferences: " + "; ".join(prefs)})
        if ins:
            msgs.append({"role": "system", "content": "The user asked to revise this step: " + " | ".join(ins)})
        return msgs

    async def execute(self, task, version, n, info, upstream, attempt, eid):
        background.ensure_active(task["id"])
        upstream = inputs.effective_upstream(task, upstream)
        private = inputs.private_context(task, n, upstream)
        n = {**n, "private_context": private}
        s = settings_svc.get(task["user_id"])
        # v2.1：web.* 检索工具按「设置开关 + 部署模式」提供；关闭时不入白名单，避免模型撞「工具被禁用」空转
        web_on = bool(s.get("tools.web_search", True)) and websearch.available()
        tools = [t for t in n["tools"] if not str(t).startswith("web.") or web_on]
        if not s.get("knowledge.auto_search", True):
            tools = [t for t in tools if t != "kb.search"]
        # v3.0 M2：外部工具挂载以 agents.node_tools 为准（两级暴露 ≤2 server / ≤24 工具 + 总开关 + 知识库外发闸门）；
        # 被闸门挡下时记原因（事件在 spec 定义后发出，避免预执行噪声）
        ext_reason = external_block_reason(n, s)
        tools = [t for t in tools if not str(t).startswith("mcp.")]
        tools = policy.allowed([*tools, *agents.node_tools(task["user_id"], n, s)], s, task["user_id"])
        # 能力开关（文件写入 / 删除 / 控制台）按用户设置过滤；v3.0 含外部 Agent 工具
        if s.get("agents.enabled", True):                    # v3.0 M2 按需发现：两个只读工具常驻，供执行中查找能力
            tools = list(dict.fromkeys([*tools, "agent.search", "agent.describe"]))
        msgs = self.messages_for(task, n, upstream)
        ins = [i["text"] for i in n.get("revision_instructions") or [] if isinstance(i, dict) and i.get("kind") != "rerun"]
        meta = {"goal": task["goal"], "lang": task["output_language"], "task_id": task["id"], "attempt": attempt,
                "version": version, "upstream": upstream, "instructions": ins,
                "node": {k: n[k] for k in ("node_key", "title", "description", "est_complexity", "model_tier")}}
        calls_log, chunk_ids, cost = [], {c["chunk_id"] for u in upstream for c in u.get("citations", []) if c.get("chunk_id")}, 0.0
        sources = {v["url"]: v for u in upstream for v in u.get("sources", []) if isinstance(v, dict) and v.get("url")}
        search_used = 0                                      # v3.0 M2：单节点 agent.search 限 2 次（防「找工具」循环）
        spec = self.exec_meta[eid]["speculative"]
        if ext_reason and not spec:
            events.emit(task["id"], "agent.external_blocked", {"reason": ext_reason}, n["node_key"], version)
        nudged = False
        length_retried = False
        # 思考模型：预算需覆盖推理 + 正文；打包步骤的文件内容走工具参数，需要更大预算。
        # 预算跨轮次可变：撞上限（finish_reason=length）后翻倍再试，并按节点记忆跨尝试沿用。
        hint_key = task["id"] + "|" + n["node_key"]
        node_max_out = 9000 if "file.write" in (n.get("tools") or []) else 6000
        node_max_out = max(node_max_out, self.out_budget_hint.get(hint_key, 0))
        # v3.0：外部 Agent 工具的「模型函数名 → 工具全名」映射（不做字符串还原，避免歧义）
        ext_map = agents.fn_map(task["user_id"]) if any(str(t).startswith("mcp.") for t in tools) else {}
        for rnd in range(4):
            offer = kb.tool_defs(tools, task["user_id"]) if tools and rnd < 3 else None
            res = await gateway.chat(user_id=task["user_id"], task_id=task["id"], node_key=n["node_key"], kind="node", info=info,
                                     messages=msgs, tools=offer, meta=meta, max_out=node_max_out)
            background.ensure_active(task["id"])
            cost += res["cost"]
            trunc = res.get("finish_reason") == "length"
            if trunc and node_max_out < 16000:                # 输出被截断：后续调用翻倍预算（思考模型常把预算烧在推理阶段）
                node_max_out = min(node_max_out * 2, 16000)
                self.out_budget_hint[hint_key] = node_max_out
            if not res["tool_calls"]:
                blank = _blank(res["content"])
                nudge, evt = None, None
                if trunc and not length_retried and rnd < 3:
                    # 撞上限的截断（含「推理烧光预算、正文全空」与「正文半截」两种）：加大预算 + 要求收敛重发
                    length_retried = True
                    nudge, evt = (EMPTY_NUDGE_TRUNC if blank else TRUNC_NUDGE), "node.length_retry"
                elif blank and not nudged and rnd < 3:
                    # 空白回复（多为工具检索为空后放弃作答）：明确要求补齐正文再给一次机会
                    nudged = True
                    nudge, evt = EMPTY_NUDGE, "node.empty_retry"
                if nudge:
                    msgs.append(_assistant_msg(res))
                    msgs.append({"role": "system", "content": nudge.format(lang=plans.LANG_NAME[task["output_language"]])})
                    if not spec:
                        events.emit(task["id"], evt, {"round": rnd + 1, "max_out": node_max_out}, n["node_key"], version)
                    continue
                break
            msgs.append(_assistant_msg(res))                  # 思考模式下带回 reasoning_content（多轮工具调用契约）
            batch = res["tool_calls"][:3]                    # R30.7：同轮只读工具并发执行（上限 3）
            async def invoke_tool(c):
                nonlocal search_used
                background.ensure_active(task["id"])
                name = ext_map.get(c["name"]) or (c["name"] or "").replace("_", ".", 1)
                risk = policy.risk_of(name, task["user_id"])
                schema = (kb.TOOL_SCHEMAS.get(name) or {}).get("parameters", {"type": "object"})
                argument_error = tool_args.validate(c["args"], schema)
                current_settings = settings_svc.get(task["user_id"])
                if name not in tools or not policy.enabled(name, current_settings, task["user_id"]):
                    out = {"error": "TOOL_DENIED", "message": f"工具 {name} 不在本步骤的白名单内"}
                    events.emit(task["id"], "tool.denied", {"tool": name}, n["node_key"], version)
                elif argument_error:
                    out = {"error": "ARGS_INVALID", "message": argument_error}
                    events.emit(task["id"], "tool.denied", {"tool": name, "reason": "ARGS_INVALID"}, n["node_key"], version)
                elif spec and risk != "read":
                    out = {"error": "TOOL_DENIED", "message": "预执行仅支持只读工具"}
                elif name.startswith("mcp.") and private and not settings_svc.get(task["user_id"]).get("agents.kb_share", True):
                    out = {"error": "KB_SHARE_OFF", "message": "当前上下文含私人资料，已禁止外发"}
                elif not spec and policy.approval_required(name, s, task["user_id"]) and not self._tool_approved(task["id"], n["node_key"], name, c["args"]):
                    # 安全闸门：删除 / 控制台等危险操作必须先得到用户批准（批准记录按「工具 + 参数」精确匹配）
                    raise policy.ToolApprovalRequired(name, c["args"])
                else:
                    if not spec:
                        events.emit(task["id"], "tool.called", {"tool": name, "args": c["args"]}, n["node_key"], version)
                    async def invoke():
                        return await self.invoke_tool(task, version, n, name, c["args"], info, current_settings)
                    if risk != "read":
                        out = await tool_ops.perform(task["id"], version, n["node_key"], name, c["args"], invoke)
                    elif name.startswith("web."):
                        # 外网检索要出网（单引擎 8s 超时 + 并发融合），放线程里执行避免阻塞事件循环
                        selected_mode = None if config.WEB_SEARCH_MODE_EXPLICIT else ("fake" if info.engine == "fake" else "auto")
                        out = await asyncio.to_thread(websearch.run, name, c["args"],
                                                      enabled=s.get("tools.web_search", True), mode_override=selected_mode)
                    elif name.startswith("mcp."):
                        if (private or chunk_ids) and not spec:
                            events.emit(task["id"], "agent.data_egress",
                                        {"tool": name, "reason": "该步骤已获得知识库内容，本次外部调用可能携带其上下文"},
                                        n["node_key"], version)
                        # v3.0 外部 Agent：MCP 调用是长阻塞（进程握手 / 网络往返），同样放线程；审计账本由 agents.call 落库
                        out = await agents.call_async(task["user_id"], name, c["args"], task["id"], n["node_key"])
                    elif name == "agent.search":
                        search_used += 1
                        if search_used > 2:              # 上限保护：模型不得陷入「找工具」循环
                            out = {"error": "SEARCH_LIMIT", "message": "本步骤的 agent.search 已用完（上限 2 次）：请基于已返回的候选继续"}
                        else:
                            out = kb.run_tool(name, c["args"], user_id=task["user_id"], attachments=task["attachments"],
                                              web_enabled=s.get("tools.web_search", True), task_id=task["id"], version=version)
                    else:
                        out = kb.run_tool(name, c["args"], user_id=task["user_id"], attachments=task["attachments"],
                                          web_enabled=s.get("tools.web_search", True), task_id=task["id"], version=version)
                    for it in out.get("items", []) if isinstance(out, dict) else []:
                        if isinstance(it, dict) and it.get("chunk_id"):      # 只有知识库片段才进引用集合
                            chunk_ids.add(it["chunk_id"])                    # （web.* / map.resolve 的结果没有 chunk_id）
                    if name.startswith("web.") and isinstance(out, dict) and not out.get("error"):
                        records = out.get("items", []) if name != "web.fetch" else [out]
                        for record in records:
                            if isinstance(record, dict) and str(record.get("url", "")).startswith(("http://", "https://")):
                                sources[record["url"]] = {"url": record["url"], "title": record.get("title", ""),
                                                          "excerpt": clip(record.get("content") or record.get("snippet") or "", 1200),
                                                          "retrieved_at": now_ms(), "mode": selected_mode or websearch.mode()}
                    if not spec:
                        events.emit(task["id"], "tool.result", {"tool": name, "items": len(out.get("items", [])) if isinstance(out, dict) else 0,
                                                                "error": out.get("error") if isinstance(out, dict) else None},
                                    n["node_key"], version)
                background.ensure_active(task["id"])
                if isinstance(out, dict) and out.get("error") == "OPERATION_UNCERTAIN":
                    raise gateway.LLMError("OPERATION_UNCERTAIN", out["message"])
                return name, out
            readonly = all(policy.risk_of(ext_map.get(c["name"]) or (c["name"] or "").replace("_", ".", 1),
                                          task["user_id"]) == "read" for c in batch)
            if readonly:
                results = await asyncio.gather(*(invoke_tool(c) for c in batch))
            else:
                results = [await invoke_tool(c) for c in batch]
            for c, (name, out) in zip(batch, results):
                calls_log.append({"tool": name, "args": c["args"], "result": out})
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": kb.dumps(out)})
            for c in res["tool_calls"][3:]:
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": '{"error":"TOO_MANY_CALLS"}'})
        if res.get("finish_reason") == "length":
            raise gateway.LLMError("TRUNCATED_OUTPUT", "模型输出持续被截断，未形成完整成果")
        if res.get("tool_calls"):
            raise gateway.LLMError("INVALID_OUTPUT", "模型未返回最终成果")
        if _blank(res.get("content")):
            raise gateway.LLMError("EMPTY_OUTPUT", "模型返回空白正文")
        try:
            data = plans._json_from(res["content"])
        except ValueError:
            raise gateway.LLMError("INVALID_OUTPUT", "模型成果不是完整 JSON")
        if not isinstance(data.get("content"), str):
            raise gateway.LLMError("INVALID_OUTPUT", "模型成果缺少文本正文")
        content = data["content"]
        if _blank(content):
            # 空正文不得记为成功：交给 _run 的重试策略，重试后仍为空则节点失败并可见（不产出假成功）
            raise gateway.LLMError("EMPTY_OUTPUT", "模型返回空白正文，未产出可交付内容")
        # INV-11：只保留本次工具真实返回过的分块引用
        bad = [c for c in re.findall(r"\[\[c:([\w-]+)\]\]", content) if c not in chunk_ids]
        for b in bad:
            content = content.replace(f"[[c:{b}]]", "")
        content, hits = mask_sensitive(content)
        unverified_links = []
        for label, url in re.findall(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", content):
            if url not in sources:
                unverified_links.append(url)
                content = content.replace(f"[{label}]({url})", label + " [待核实 / unverified]")
        claims = []
        for c in _as_list(data.get("claims")):
            if isinstance(c, dict) and c.get("text"):
                claims.append({"text": clip(str(c["text"]), 200),
                               "citations": [x for x in _as_list(c.get("citations")) if isinstance(x, str) and (x in chunk_ids or x in sources)],
                               "verification": "source_only"})
        cited = sorted(set(re.findall(r"\[\[c:([\w-]+)\]\]", content)))
        out = {"summary": clip(str(data.get("summary") or content), 160), "content": content, "claims": claims,
               "questions": [clip(str(q), 200) for q in _as_list(data.get("questions")) if str(q).strip()][:3],
               "confidence": _as_float(data.get("confidence"), 0.7), "citations": list(kb.chunk_refs(cited, task["user_id"]).values()),
               "chart": artifacts.clean_chart(data.get("chart")),   # 图表数据经校验后随成果交付（不合法则丢弃）
               "map": artifacts.clean_map(data.get("map")),         # 地图数据（v1.9，旅行/路线类，不合法则丢弃）
               # v3.0 M3/M4：十类功能性节点数据（画布直显，prd/08 §6；各自 clean 校验，不合法整块丢弃）
               "table": artifacts.clean_table(data.get("table")),
               "metrics": artifacts.clean_metrics(data.get("metrics")),
               "list": artifacts.clean_list(data.get("list")),
               "code": artifacts.clean_code(data.get("code")),
               "image": artifacts.clean_image(data.get("image"), task["user_id"], task["id"], version),
               "timeline": artifacts.clean_timeline(data.get("timeline")),
               "calendar": artifacts.clean_calendar(data.get("calendar")),
               "status": artifacts.clean_status(data.get("status")),
               "chat": artifacts.clean_chat(data.get("chat")),
               "web": artifacts.clean_web(data.get("web")),
               "private_context": private or bool(chunk_ids),
               "sources": list(sources.values()), "unverified_links": list(dict.fromkeys(unverified_links)),
               "demo": info.engine == "fake" or any(u.get("demo") for u in upstream)
                       or any(v.get("mode") == "fake" for v in sources.values()),
               "model": info.display, "model_source": info.source, "bad_citations": bad, "sensitive": hits}
        if cited and not spec:
            kb.mark_cited(cited)
        return out, calls_log, round(cost, 6)

    async def invoke_tool(self, task, version, n, name, args, info, settings):
        if name.startswith("mcp."):
            return await agents.call_async(task["user_id"], name, args, task["id"], n["node_key"])
        return kb.run_tool(name, args, user_id=task["user_id"], attachments=task["attachments"],
                           web_enabled=settings.get("tools.web_search", True), task_id=task["id"], version=version)

    # ---------------- 结果落地 ----------------
    @db.atomic
    def _on_success(self, eid, task, version, n, out) -> None:
        meta = self.exec_meta[eid]
        cur_task = store.get_task(task["id"])
        if meta["speculative"]:
            nid = next(iter(self.adopters.get(eid, {n["id"]})))
            node = store.node_by_id(nid)
            if not node or cur_task["active_version"] != version:
                self._discard(eid, task["id"], n["node_key"], version, "version_changed")
                return
            store.set_node(nid, spec_state="ready")
            events.emit(task["id"], "node.speculative_done", {"gate": meta["gate"], "execution_id": eid}, n["node_key"], version)
            pv = store.active_pv(cur_task)
            gate = store.node_by_key(pv["id"], meta["gate"])
            if gate and gate["status"] == "succeeded":       # 预执行期间已被批准 → 立即提交
                self.commit_speculative(cur_task, pv, gate)
            return
        for nid in self.adopters.get(eid, {n["id"]}):
            node = store.node_by_id(nid)
            if not node or node["status"] != "running":
                continue
            pvr = db.q1("SELECT * FROM plan_versions WHERE id=?", (node["plan_version_id"],))
            store.set_node(nid, status="succeeded", output=out, execution_id=eid, finished_at=now_ms())
            if pvr["version"] != cur_task["active_version"]:
                continue                                     # 旧版本节点：只记录，不推进、不发通知（INV-05）
            events.emit(task["id"], "node.succeeded",
                        {"execution_id": eid, "summary": out["summary"], "model": out["model"]}, n["node_key"], pvr["version"])
            self._attention_from(task, pvr["version"], node, out)
            # 正常完成：等待中的只读预执行结果现在可以提交（工具闸门被批准后重跑成功时走这条路径）
            self.commit_speculative(cur_task, store.active_pv(cur_task), store.node_by_id(nid))

    @db.atomic
    def _on_fail(self, eid, task, version, n, code, msg) -> None:
        meta = self.exec_meta.get(eid, {})
        if meta.get("speculative"):
            self._discard(eid, task["id"], n["node_key"], version, "failed")
            return
        cur = store.get_task(task["id"])
        for nid in self.adopters.get(eid, {n["id"]}):
            node = store.node_by_id(nid)
            if node and node["status"] == "running":
                store.set_node(nid, status="failed", error_code=code, error=msg, finished_at=now_ms())
                pvr = db.q1("SELECT version FROM plan_versions WHERE id=?", (node["plan_version_id"],))
                if pvr["version"] == cur["active_version"]:
                    events.emit(task["id"], "node.failed", {"code": code, "message": msg}, n["node_key"], pvr["version"])
        s = settings_svc.get(task["user_id"])
        if s.get("notifications.failed", True):
            events.notify(task["user_id"], "task_failed", f"步骤失败：{n['title']}（{code}）", task["id"])

    def _attention_from(self, task, version, node, out) -> None:
        def add(kind, sev, title, detail=""):
            db.insert("attention_items", {"id": new_id("att"), "task_id": task["id"], "node_key": node["node_key"],
                                          "plan_version": version, "kind": kind, "severity": sev, "title": clip(title, 160),
                                          "detail": clip(detail, 600), "created_at": now_ms()})
            events.emit(task["id"], "attention.created", {"kind": kind, "severity": sev, "title": title}, node["node_key"], version)
        weak = [c["text"] for c in out.get("claims", []) if not c.get("citations")]
        if weak and node["node_key"] != "review":
            add("unsupported_claim", "warn", f"{len(weak)} 条结论缺少出处", "\n".join(weak[:3]))
        for q in out.get("questions", []):
            add("open_question", "warn", q)                  # 不停机：问题随成果一起提示，用户可随时回答触发重算
        if out.get("bad_citations"):
            add("bad_citation", "warn", f"已移除 {len(out['bad_citations'])} 个无法追溯的引用")
        if out.get("unverified_links"):
            add("unsupported_source", "warn", "部分网络来源未被工具检索验证", "\n".join(out["unverified_links"]))
        if out.get("sensitive"):
            add("sensitive_info", "warn", "输出中的敏感信息已自动遮蔽", "、".join(sorted(set(out["sensitive"]))))
        if out.get("confidence", 1) < 0.5:
            add("low_confidence", "info", "这一步的把握度较低，建议重点检查")

    # ---------------- 安全防护（危险操作执行前批准，可选功能） ----------------
    def _tool_approved(self, task_id: str, node_key: str, tool: str, args) -> bool:
        """该「工具 + 参数」是否已被用户批准过。

        按 工具+参数哈希 精确匹配：批准一次 = 允许这一件具体的事；模型换成别的参数会再问一次。
        审批不随计划版本失效 —— 同一件被批准过的操作不会因为版本更新而要你再点一次。
        """
        return bool(db.q1("SELECT id FROM approvals WHERE task_id=? AND node_key=? AND kind='tool' AND tool=? AND args_hash=? "
                          "AND status='approved'", (task_id, node_key, tool, policy.args_hash(args))))

    @db.atomic
    def _suspend_for_tool(self, task, version, e: policy.ToolApprovalRequired, eid: str) -> None:
        """把即将执行危险操作的节点挂起为「等待你确认」，并登记待批准的具体操作。"""
        db.update("executions", {"id": eid}, {"status": "suspended", "finished_at": now_ms()})
        cur = store.get_task(task["id"])
        touched = False
        for nid in self.adopters.get(eid, ()):
            node = store.node_by_id(nid)
            if not node or node["status"] != "running":
                continue
            touched = True
            store.set_node(nid, status="awaiting_approval", approval_status="pending", error=None, error_code=None)
            pvr = db.q1("SELECT * FROM plan_versions WHERE id=?", (node["plan_version_id"],))
            if pvr["version"] != cur["active_version"]:
                continue                                     # 旧版本节点：只挂起，不登记审批、不发通知（INV-05）
            db.insert("approvals", {"id": new_id("apv"), "task_id": task["id"], "node_id": nid, "node_key": node["node_key"],
                                    "plan_version": pvr["version"], "status": "pending", "kind": "tool", "created_at": now_ms(),
                                    "tool": e.tool, "args": json.dumps(e.tool_args, ensure_ascii=False)[:1000], "args_hash": e.hash})
            summary = policy.summarize(e.tool, e.tool_args, task["user_id"])
            events.emit(task["id"], "tool.approval_required",
                        {"tool": e.tool, "risk": e.risk, "summary": summary, "args": e.tool_args}, node["node_key"], pvr["version"])
            events.notify(task["user_id"], "approval_required", f"需要你批准：{summary}", task["id"])
        if not touched:                                      # 兜底：找不到可挂起的节点时，至少留下可见记录
            events.emit(task["id"], "scheduler.error", {"message": f"安全闸门无法挂起节点（{e.tool}）"}, None, version)

    @db.atomic
    def deny_tool(self, task, approval: dict, user_id: str, reason: str = "") -> dict:
        """用户拒绝执行危险操作：该步骤失败并明确说明原因（不自动重跑、不静默改道）。"""
        node = store.node_by_id(approval["node_id"])
        db.update("approvals", {"id": approval["id"]}, {"status": "rejected", "reason": reason or "拒绝执行该操作",
                                                        "decided_by": user_id, "decided_at": now_ms()})
        if node and node["status"] == "awaiting_approval":
            store.set_node(node["id"], status="failed", approval_status="rejected", finished_at=now_ms(),
                           error_code="TOOL_REJECTED", error=f"你拒绝了这个操作：{policy.summarize(approval['tool'] or '', db.j(approval['args'], {}), task['user_id'])}")
        events.emit(task["id"], "tool.approval_denied", {"tool": approval["tool"], "reason": reason}, approval["node_key"],
                    approval["plan_version"])
        self.kick(task["id"])
        return {"status": "rejected"}

    # ---------------- 安全防护的批准（唯一可能停顿等人之处） ----------------
    @db.atomic
    def approve(self, approval_id: str, user_id: str) -> dict:
        """批准一次被安全防护拦下的危险操作：批准 → 该步骤重新排队执行（批准按「工具+参数」精确匹配）。"""
        a = db.q1("SELECT * FROM approvals WHERE id=?", (approval_id,))
        task = store.get_task(a["task_id"], user_id) if a else None
        if not a or a["status"] != "pending":
            raise ApiError(409, "STATE_CONFLICT", "该审批已处理或已失效")
        node = store.node_by_id(a["node_id"])
        if not node or node["status"] != "awaiting_approval" or a["plan_version"] != task["active_version"]:
            raise ApiError(409, "STATE_CONFLICT", "该审批属于旧版本，已失效")
        if a["kind"] != "tool":                              # 节点级审批已退出流程：只有安全防护会拦截
            raise ApiError(409, "STATE_CONFLICT", "该审批类型已退出流程，无需处理")
        db.update("approvals", {"id": approval_id}, {"status": "approved", "decided_by": user_id, "decided_at": now_ms()})
        store.set_node(node["id"], status="pending", approval_status="approved", error=None, error_code=None)
        pv = store.active_pv(task)
        events.emit(task["id"], "approval.approved", {"by": "user", "kind": "tool", "tool": a["tool"]}, node["node_key"], pv["version"])
        self.kick(task["id"])
        return {"status": "approved", "kind": "tool"}

    @db.atomic
    def commit_speculative(self, task, pv, gate) -> None:
        nodes = store.load_nodes(pv["id"])
        by = {n["node_key"]: n for n in nodes}
        for v in nodes:
            if gate["node_key"] not in v["depends_on"] or v.get("spec_state") != "ready":
                continue
            ex = db.q1("SELECT * FROM executions WHERE task_id=? AND node_key=? AND speculative=1 AND status='succeeded' "
                       "AND plan_version=? ORDER BY started_at DESC LIMIT 1", (task["id"], v["node_key"], pv["version"]))
            info, _ = gateway.resolve_for_node(task["user_id"], task, v, settings_svc.get(task["user_id"]))
            if ex and info and ex["input_hash"] == self.input_hash(task, v, by, info.ref) \
                    and all(by[p]["status"] == "succeeded" for p in v["depends_on"]):
                out = db.j(ex["output"], {})
                store.set_node(v["id"], status="succeeded", output=out, execution_id=ex["id"], input_hash=ex["input_hash"],
                               spec_state="committed", started_at=ex["started_at"], finished_at=now_ms())
                events.emit(task["id"], "node.speculative_committed", {"gate": gate["node_key"], "execution_id": ex["id"]},
                            v["node_key"], pv["version"])
                self._attention_from(task, pv["version"], v, out)
                if out.get("citations"):
                    kb.mark_cited([c["chunk_id"] for c in out["citations"]])
            else:
                if ex:
                    db.update("executions", {"id": ex["id"]}, {"status": "discarded"})
                store.set_node(v["id"], spec_state=None)
                events.emit(task["id"], "node.speculative_discarded", {"gate": gate["node_key"], "reason": "hash_mismatch"},
                            v["node_key"], pv["version"])

    def _discard(self, eid, task_id, node_key, version, reason) -> None:
        db.update("executions", {"id": eid}, {"status": "discarded"})
        for nid in self.adopters.get(eid, ()):
            store.set_node(nid, spec_state=None)
        events.emit(task_id, "node.speculative_discarded", {"reason": reason}, node_key, version)

    def discard_all_speculative(self, task_id: str, version: int) -> None:
        """版本切换 / 审批退回：预执行结果一律作废。"""
        for eid, m in list(self.exec_meta.items()):
            if m["task_id"] == task_id and m["speculative"]:
                self.inflight[eid].cancel()
        for ex in db.q("SELECT id, node_key FROM executions WHERE task_id=? AND plan_version=? AND speculative=1 AND status='succeeded'",
                       (task_id, version)):
            node = db.q1("SELECT n.spec_state FROM nodes n JOIN plan_versions p ON p.id=n.plan_version_id "
                         "WHERE p.task_id=? AND p.version=? AND n.node_key=?", (task_id, version, ex["node_key"]))
            if node and node["spec_state"] == "committed":
                continue
            db.update("executions", {"id": ex["id"]}, {"status": "discarded"})
            events.emit(task_id, "node.speculative_discarded", {"reason": "version_changed"}, ex["node_key"], version)

    # ---------------- 任务级控制 ----------------
    @db.atomic
    def pause(self, task_id):
        db.update("tasks", {"id": task_id}, {"paused": 1, "updated_at": now_ms()})
        self.step_budget.pop(task_id, None)
        events.emit(task_id, "run.paused", {"in_flight": self.running_count(task_id)})

    @db.atomic
    def resume(self, task_id):
        db.update("tasks", {"id": task_id}, {"paused": 0, "updated_at": now_ms()})
        events.emit(task_id, "run.resumed", {})
        self.kick(task_id)

    @db.atomic
    def step(self, task_id):
        self.step_budget[task_id] = 1
        db.update("tasks", {"id": task_id}, {"paused": 0})
        events.emit(task_id, "run.step", {})
        self.kick(task_id)

    @db.atomic
    def cancel(self, task_id):
        background.cancel(task_id)
        self.waiting.pop(task_id, None)
        for eid in list(self.task_execs.get(task_id, ())):
            m = self.exec_meta.get(eid)
            if m:
                m["cancel_final"] = True
            if eid in self.inflight:
                self.inflight[eid].cancel()
        db.update("tasks", {"id": task_id}, {"status": "cancelled", "updated_at": now_ms()})
        db.ex("UPDATE nodes SET status='cancelled' WHERE task_id=? AND status IN ('pending','running','awaiting_approval')", (task_id,))
        db.ex("UPDATE approvals SET status='cancelled' WHERE task_id=? AND status='pending'", (task_id,))
        events.emit(task_id, "task.cancelled", {})

    def cancel_executions_for(self, node_ids: set[str]) -> None:
        """修订时：受影响节点的进行中执行如果不再被「活动版本」中的任何节点沿用，就取消。
        多次修订后只剩旧版本节点沿用的执行，结果不会再被使用，继续跑只会占用并发与预算。"""
        for eid, ad in list(self.adopters.items()):
            ad -= node_ids
            if eid not in self.inflight or self.exec_meta.get(eid, {}).get("speculative"):
                continue                                     # 预执行由 discard_all_speculative 统一处理
            live = bool(ad) and db.q1(
                "SELECT 1 FROM nodes n JOIN plan_versions p ON p.id=n.plan_version_id "
                f"WHERE p.status='active' AND n.id IN ({','.join('?' * len(ad))}) LIMIT 1", list(ad))
            if not live:
                self.inflight[eid].cancel()

    def attach_adopter(self, eid: str, node_id: str) -> None:
        if eid in self.adopters:
            self.adopters[eid].add(node_id)

    # ---------------- 完成 / 快速回答 ----------------
    @db.atomic
    def finalize(self, task, pv, nodes) -> None:
        """收官：即使有失败或跳过的步骤，也用已完成部分产出成果，问题清单随成果一并交付。"""
        art = artifacts.build(task, pv, nodes)
        issues = sum(1 for n in nodes if n["status"] in ("failed", "skipped"))
        started = task.get("run_started_at") or task["created_at"]
        db.update("tasks", {"id": task["id"]}, {"status": "completed", "completed_at": now_ms(), "updated_at": now_ms(),
                                                "completion_quality": art["quality"]["level"],
                                                "actual_makespan_s": round((now_ms() - started) / 1000, 1)})
        events.emit(task["id"], "task.completed", {"artifact_id": art["id"], "version": pv["version"], "issues": issues, "quality": art["quality"]},
                    plan_version=pv["version"])
        if settings_svc.get(task["user_id"]).get("notifications.completed", True):
            label = {"complete": "完整交付", "partial": "部分交付", "empty": "未形成有效交付"}[art["quality"]["level"]]
            text = f"任务完成 · {label}：{task['title']}"
            events.notify(task["user_id"], "task_completed", text, task["id"])

    async def quick_answer(self, task_id: str) -> None:
        background.ensure_active(task_id)
        task = store.get_task(task_id)
        db.update("tasks", {"id": task_id}, {"status": "running", "lane": "quick"})
        s = settings_svc.get(task["user_id"])
        info = plans.stage_model(task, "fast")
        events.emit(task_id, "quick.started", {"model": info.display})
        res = await gateway.chat(user_id=task["user_id"], task_id=task_id, node_key=None, kind="quick", info=info, json_mode=False,
                                 messages=[{"role": "system", "content": plans.SYSTEM_PREAMBLE},
                                           {"role": "system", "content": f"Answer concisely in {plans.LANG_NAME[task['output_language']]}."},
                                           {"role": "user", "content": task["goal"]}],
                                 meta={"goal": task["goal"], "lang": task["output_language"], "task_id": task_id}, max_out=4000)
        background.ensure_active(task_id)
        if _blank(res["content"]) or res.get("finish_reason") == "length":
            raise gateway.LLMError("INVALID_OUTPUT", "快速回答为空或被截断")
        with db.Tx():
            artifacts.build_quick(task, res["content"], info.display, demo=info.engine == "fake")
            db.update("tasks", {"id": task_id}, {"status": "completed", "completion_quality": "complete", "completed_at": now_ms(), "updated_at": now_ms()})
            events.emit(task_id, "task.completed", {"lane": "quick"})

    # ---------------- 重启恢复（INV-15） ----------------
    def recover(self) -> None:
        db.ex("UPDATE executions SET status='orphaned', finished_at=? WHERE status='running'", (now_ms(),))
        db.ex("UPDATE nodes SET status='pending' WHERE status='running'")
        db.ex("UPDATE nodes SET spec_state=NULL WHERE spec_state='running'")
        for t in db.q("SELECT id, status FROM tasks WHERE status IN ('running','triaging','planning','plan_review')"):
            if t["status"] == "running":
                events.emit(t["id"], "run.recovered", {})
                full = store.get_task(t["id"])
                if full["lane"] == "quick" and not store.active_pv(full):
                    background.start(t["id"], plans.triage_and_plan(t["id"], self))
                else:
                    self.kick(t["id"])
            elif t["status"] == "plan_review":               # 自动确认前一刻重启：把已生成的计划直接接上开跑
                full = store.get_task(t["id"])
                try:
                    plans.confirm(t["id"], full["user_id"], auto=True)
                    events.emit(t["id"], "run.recovered", {})
                    self.kick(t["id"])
                except Exception:    # noqa: BLE001  计划不完整则退回失败，可重新规划
                    db.update("tasks", {"id": t["id"]}, {"status": "failed", "error_code": "INTERRUPTED", "updated_at": now_ms()})
                    events.emit(t["id"], "task.failed", {"code": "INTERRUPTED", "message": "服务重启中断了规划，请重新规划"})
            else:
                db.update("tasks", {"id": t["id"]}, {"status": "failed", "error_code": "INTERRUPTED", "updated_at": now_ms()})
                events.emit(t["id"], "task.failed", {"code": "INTERRUPTED", "message": "服务重启中断了规划，请重新规划"})


def _as_list(v) -> list:
    """模型输出容错：None → []；单个值 → [值]（避免把字符串逐字符当成多条问题）。"""
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def _as_float(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, f)) if f == f else default          # NaN → 默认值


def _blank(s) -> bool:
    """正文是否为空白（纯空格/换行也视为空白，避免空输出被当成有效交付）。"""
    return not str(s or "").strip()


def _assistant_msg(res: dict) -> dict:
    """构造回传模型的 assistant 消息；思考模式需带回 reasoning_content（DeepSeek 多轮工具调用契约）。

    无工具调用时 `content` 必须是字符串（可为空串），不能是 `null`——部分服务商（DeepSeek）
    校验「content 或 tool_calls 必须存在」，`content=None` 且无 tool_calls 会整个请求 400，
    空白回复的补齐重试正是这条路径。
    """
    if res["tool_calls"]:
        msg: dict = {"role": "assistant", "content": res["content"] or None}
        msg["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
            for c in res["tool_calls"]]
    else:
        msg = {"role": "assistant", "content": res["content"] or ""}
    if res.get("reasoning"):
        msg["reasoning_content"] = res["reasoning"]
    return msg


RUNNER = Runner()
