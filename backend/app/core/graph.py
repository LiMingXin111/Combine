"""任务图（DAG）纯函数：校验、拓扑序、依赖闭包、传递约简、向上秩、列表调度模拟。

这些函数不访问数据库、不调用模型，结果完全确定（INV-16），便于单元测试。
节点用 dict 表示，至少包含 node_key 与 depends_on。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable

from ..util import ApiError


def index(nodes: list[dict]) -> dict[str, dict]:
    return {n["node_key"]: n for n in nodes}


def children_map(nodes: list[dict]) -> dict[str, list[str]]:
    ch: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        for d in n["depends_on"]:
            ch[d].append(n["node_key"])
    return ch


def topo_order(nodes: list[dict]) -> list[str]:
    """Kahn 算法；存在环时抛出 PLAN_CYCLE。同层按原始顺序，保证确定性。"""
    idx = index(nodes)
    for n in nodes:
        for d in n["depends_on"]:
            if d not in idx:
                raise ApiError(400, "PLAN_UNKNOWN_DEP", f"步骤「{n['title']}」依赖了不存在的步骤 {d}")
            if d == n["node_key"]:
                raise ApiError(400, "PLAN_CYCLE", f"步骤「{n['title']}」不能依赖自己")
    indeg = {n["node_key"]: len(set(n["depends_on"])) for n in nodes}
    ch = children_map(nodes)
    order_pos = {n["node_key"]: i for i, n in enumerate(nodes)}
    ready = sorted([k for k, v in indeg.items() if v == 0], key=order_pos.get)
    out: list[str] = []
    while ready:
        k = ready.pop(0)
        out.append(k)
        for c in sorted(set(ch[k]), key=order_pos.get):
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
        ready.sort(key=order_pos.get)
    if len(out) != len(nodes):
        raise ApiError(400, "PLAN_CYCLE", "计划中存在循环依赖")
    return out


def would_cycle(nodes: list[dict], frm: str, to: str) -> bool:
    """新增依赖 frm→to（to 依赖 frm）是否成环：即 frm 是否已可从 to 到达。"""
    return frm == to or frm in descendants(nodes, {to})


def descendants(nodes: list[dict], keys: set[str]) -> set[str]:
    ch = children_map(nodes)
    seen: set[str] = set()
    stack = list(keys)
    while stack:
        k = stack.pop()
        for c in ch.get(k, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def ancestors(nodes: list[dict], key: str) -> set[str]:
    idx = index(nodes)
    seen: set[str] = set()
    stack = list(idx[key]["depends_on"])
    while stack:
        k = stack.pop()
        if k not in seen:
            seen.add(k)
            stack.extend(idx[k]["depends_on"])
    return seen


def redundant_edges(nodes: list[dict]) -> set[tuple[str, str]]:
    """传递约简：若 u→v 可经由其他路径推出，则 (u,v) 冗余。只影响显示，执行语义不变（R30.2-1）。"""
    idx = index(nodes)
    out: set[tuple[str, str]] = set()
    for n in nodes:
        v = n["node_key"]
        deps = set(n["depends_on"])
        for u in deps:
            others = deps - {u}
            reach: set[str] = set()
            for o in others:
                reach |= ancestors(nodes, o) | {o}
            if u in reach:
                out.add((u, v))
    _ = idx
    return out


def upward_rank(nodes: list[dict], dur: Callable[[dict], float]) -> dict[str, float]:
    """向上秩 rank(n) = dur(n) + max(rank(后继))（HLFET），用于关键路径优先调度（R30.4）。"""
    ch = children_map(nodes)
    idx = index(nodes)
    rank: dict[str, float] = {}
    for k in reversed(topo_order(nodes)):
        rank[k] = dur(idx[k]) + max((rank[c] for c in ch.get(k, [])), default=0.0)
    return rank


def critical_path(nodes: list[dict], rank: dict[str, float]) -> list[str]:
    if not nodes:
        return []
    ch = children_map(nodes)
    roots = [n["node_key"] for n in nodes if not n["depends_on"]]
    cur = max(roots, key=lambda k: rank[k])
    path = [cur]
    while ch.get(cur):
        cur = max(ch[cur], key=lambda k: rank[k])
        path.append(cur)
    return path


def priority_key(nodes: list[dict], rank: dict[str, float]):
    """派发优先级：rank 降序 → 解锁后继多者优先 → 拓扑序。"""
    ch = children_map(nodes)
    topo = {k: i for i, k in enumerate(topo_order(nodes))}
    return lambda k: (-round(rank.get(k, 0), 6), -len(ch.get(k, [])), topo[k])


def simulate(nodes: list[dict], dur: Callable[[dict], float], limit: int) -> dict:
    """与正式调度同优先级的列表调度模拟，得到预计总时长与每个节点的预计起止（R30.9 / R31.7）。"""
    if not nodes:
        return {"makespan_s": 0, "serial_s": 0, "parallel_saving": 0, "critical_path": [], "timeline": []}
    idx = index(nodes)
    rank = upward_rank(nodes, dur)
    key = priority_key(nodes, rank)
    ch = children_map(nodes)
    done: set[str] = set()
    ready = [n["node_key"] for n in nodes if not n["depends_on"]]
    lanes: list[tuple[float, str, int]] = []       # (结束时刻, key, 泳道号)
    free_lanes = list(range(max(1, limit)))
    t = 0.0
    tl: dict[str, dict] = {}
    while ready or lanes:
        ready.sort(key=key)
        while ready and free_lanes:
            k = ready.pop(0)
            lane = free_lanes.pop(0)
            d = dur(idx[k])
            tl[k] = {"node_key": k, "est_start_s": round(t, 2), "est_end_s": round(t + d, 2), "lane": lane}
            lanes.append((t + d, k, lane))
        lanes.sort()
        t, k, lane = lanes.pop(0)
        free_lanes.append(lane)
        free_lanes.sort()
        done.add(k)
        for c in ch.get(k, []):
            if c not in done and c not in ready and c not in tl and all(p in done for p in idx[c]["depends_on"]):
                ready.append(c)
    serial = sum(dur(n) for n in nodes)
    makespan = max(v["est_end_s"] for v in tl.values())
    return {"makespan_s": round(makespan, 1), "serial_s": round(serial, 1),
            "parallel_saving": round(1 - makespan / serial, 3) if serial else 0,
            "critical_path": critical_path(nodes, rank), "rank": rank,
            "timeline": [tl[k] for k in topo_order(nodes)]}
