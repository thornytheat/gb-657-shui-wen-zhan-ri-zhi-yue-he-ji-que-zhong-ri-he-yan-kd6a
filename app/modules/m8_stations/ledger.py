"""合并决定台账：别名链解析、循环检测、撤销顺序。

台账只追加不覆盖：每条「把 source 并入 target」的决定带一个全局 revision，
撤销只把该条决定标成 undone，绝不改写历史决定。有效别名图由 status=applied
的决定按 seq 顺序折叠得到，因此撤销后自然回到合并前的分组。

硬约束：
- 一个站号只能有一条出边（source 已是别名时拒绝再合并，先撤销）；
- 合并两组时不允许形成环（A→B 之后再 B→A）；
- 撤销只允许撤销「最近一条 applied 决定」，保证链不会断在中间。
"""
from __future__ import annotations

VERDICT_OK = "ok"
VERDICT_SELF = "self"
VERDICT_ALREADY_ALIAS = "already_alias"
VERDICT_CYCLE = "cycle"
VERDICT_SAME_GROUP = "same_group"
VERDICT_PENDING = "pending"


def applied(decisions: list[dict]) -> list[dict]:
    return sorted((row for row in decisions if row["status"] == "applied"),
                  key=lambda row: int(row["seq"]))


def effective_edges(decisions: list[dict]) -> dict[str, str]:
    """source → target，后出现的决定覆盖先出现的（正常流程下不会覆盖）。"""
    edges: dict[str, str] = {}
    for row in applied(decisions):
        edges[str(row["source"])] = str(row["target"])
    return edges


def resolve(station: str, edges: dict[str, str]) -> tuple[str, list[str]]:
    """返回 (根站号, 从该站到根的链)。链含自身与根，便于回读来源。"""
    chain = [station]
    seen = {station}
    cursor = station
    while cursor in edges:
        cursor = edges[cursor]
        if cursor in seen:
            raise ValueError(f"别名图存在环：{chain + [cursor]}")
        chain.append(cursor)
        seen.add(cursor)
    return cursor, chain


def root_map(decisions: list[dict], stations: list[str] | None = None) -> dict[str, str]:
    edges = effective_edges(decisions)
    nodes = set(stations or ())
    nodes.update(edges)
    nodes.update(edges.values())
    return {node: resolve(node, edges)[0] for node in sorted(nodes)}


def check_merge(decisions: list[dict], source: str, target: str,
                *, pending_before: int | None = None) -> str:
    """判定一次合并是否可落账；不写库，只返回结论。

    先判环再判「已是别名」：B 已是 A 的别名时再提 B→A，真正的问题是成环。
    `pending_before` 是本次决定之前未完成决定的序号：源站若被更早的未完成
    决定占用，必须等那条决定先恢复或失败，不能越序落一条新的。
    """
    if source == target:
        return VERDICT_SELF
    edges = effective_edges(decisions)
    _, chain_s = resolve(source, edges)
    _, chain_t = resolve(target, edges)
    if source in chain_t or target in chain_s:
        return VERDICT_CYCLE
    if source in edges:
        return VERDICT_ALREADY_ALIAS
    if pending_before is not None:
        for row in decisions:
            if row.get("status") != "pending":
                continue
            if int(row["seq"]) < int(pending_before) and str(row["source"]) == source:
                return VERDICT_PENDING
    if chain_s[-1] == chain_t[-1]:
        return VERDICT_SAME_GROUP
    return VERDICT_OK


def latest_applied_seq(decisions: list[dict]) -> int | None:
    rows = applied(decisions)
    return int(rows[-1]["seq"]) if rows else None
