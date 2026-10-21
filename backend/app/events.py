"""事件总线：先持久化、再广播（INV-07）；按任务递增 seq；SSE 订阅者与用户通知流。

事件只来自真实发生的执行（INV-13）——调度器、执行器与用户操作是唯一的事件来源。
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from . import config, db
from .util import new_id, now_ms, redact_obj

_task_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
_user_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
_lagging: set[int] = set()                                  # 队列已满的订阅者（id(queue)）


def _offer(qu: asyncio.Queue, item) -> None:
    try:
        qu.put_nowait(item)
    except asyncio.QueueFull:                                # 客户端读得太慢：丢弃并标记，由读取方从数据库补齐
        _lagging.add(id(qu))


def lagging(qu: asyncio.Queue) -> bool:
    """读取方调用：返回是否落后，并清除标记。"""
    if id(qu) in _lagging:
        _lagging.discard(id(qu))
        return True
    return False


@db.atomic
def emit(task_id: str, type_: str, payload: dict | None = None, node_key: str | None = None,
         plan_version: int | None = None) -> dict:
    payload = redact_obj(payload or {})                      # INV-18：事件中不得出现任何 Key
    row = db.q1("SELECT COALESCE(MAX(seq),0)+1 AS s FROM events WHERE task_id=?", (task_id,))
    seq = row["s"]
    ev = {"task_id": task_id, "seq": seq, "type": type_, "node_key": node_key,
          "plan_version": plan_version, "payload": payload, "created_at": now_ms()}
    db.insert("events", ev)                                  # 先落库
    def broadcast():
        for qu in list(_task_subs.get(task_id, ())):
            _offer(qu, ev)
    db.after_commit(broadcast)
    return ev


def subscribe(task_id: str) -> asyncio.Queue:
    qu: asyncio.Queue = asyncio.Queue(maxsize=config.SSE_QUEUE_MAX)
    _task_subs[task_id].add(qu)
    return qu


def unsubscribe(task_id: str, qu: asyncio.Queue) -> None:
    subs = _task_subs.get(task_id)
    if subs is not None:
        subs.discard(qu)
        if not subs:
            _task_subs.pop(task_id, None)                     # 不为已断开的任务保留空集合
    _lagging.discard(id(qu))


def prune_empty() -> None:
    for d in (_task_subs, _user_subs):
        for k in [k for k, v in d.items() if not v]:
            d.pop(k, None)


def sub_count(task_id: str) -> int:
    return len(_task_subs.get(task_id, ()))


def history(task_id: str, since_seq: int = 0, limit: int = 5000) -> list[dict]:
    rows = db.q("SELECT task_id,seq,type,node_key,plan_version,payload,created_at FROM events "
                "WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?", (task_id, since_seq, limit))
    for r in rows:
        r["payload"] = db.j(r["payload"], {})
    return rows


# ---------- 通知 ----------
@db.atomic
def notify(user_id: str, kind: str, title: str, task_id: str | None = None) -> dict:
    n = {"id": new_id("ntf"), "user_id": user_id, "kind": kind, "task_id": task_id,
         "title": title, "read": 0, "created_at": now_ms()}
    db.insert("notifications", n)
    def broadcast():
        for qu in list(_user_subs.get(user_id, ())):
            _offer(qu, n)
    db.after_commit(broadcast)
    return n


def subscribe_user(user_id: str) -> asyncio.Queue:
    qu: asyncio.Queue = asyncio.Queue(maxsize=200)
    _user_subs[user_id].add(qu)
    return qu


def user_sub_count(user_id: str) -> int:
    return len(_user_subs.get(user_id, ()))


def unsubscribe_user(user_id: str, qu: asyncio.Queue) -> None:
    subs = _user_subs.get(user_id)
    if subs is not None:
        subs.discard(qu)
        if not subs:
            _user_subs.pop(user_id, None)
    _lagging.discard(id(qu))
