"""谱系图：逆向原料分摊与正向库存影响。

来源占比来自转换事件登记的 ``input_allocations[*].fraction``（混批占比之和为 1）；
原料折合数量逐段乘以出品率（投入合计 / 产出合计），把成品数量还原为原料数量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .ledger import Journal
from .timeutil import parse


@dataclass(frozen=True)
class RawShare:
    lot_id: str
    supplier_id: str | None
    share: float
    raw_equivalent_quantity: float


@dataclass(frozen=True)
class PathStep:
    batch_id: str
    step: str
    producer_event_id: str


@dataclass
class Genealogy:
    start_node: str
    paths: list[list[PathStep]] = field(default_factory=list)
    raw_shares: list[RawShare] = field(default_factory=list)
    tainted: bool = False


def _producer_inputs(journal: Journal, batch_id: str) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]], float, float]:
    event = journal.batch_producer.get(batch_id)
    if event is None:
        return None, [], 0.0, 0.0
    inputs = event["payload"]["input_allocations"]
    input_total = sum(float(item["quantity"]) for item in inputs)
    output_total = sum(float(item["quantity"]) for item in event["payload"]["outputs"])
    return event, inputs, input_total, output_total


def _walk_back(journal: Journal, node_id: str, path: list[PathStep],
               yield_factor: float, share: float,
               raw_accumulator: dict[str, float], share_accumulator: dict[str, float],
               paths: list[list[PathStep]]) -> None:
    if node_id in journal.lots:
        raw_accumulator[node_id] = raw_accumulator.get(node_id, 0.0) + yield_factor * share
        share_accumulator[node_id] = share_accumulator.get(node_id, 0.0) + share
        paths.append(list(path))
        return
    event, inputs, input_total, output_total = _producer_inputs(journal, node_id)
    if event is None:
        paths.append(list(path))  # 断点：来源不明
        return
    step_yield = input_total / output_total if output_total else 1.0
    current = PathStep(node_id, event["payload"]["step"], event["event_id"])
    for item in inputs:
        fraction = float(item["fraction"])
        _walk_back(journal, item["source_batch_id"], path + [current],
                   yield_factor * step_yield, share * fraction,
                   raw_accumulator, share_accumulator, paths)


def trace_back(journal: Journal, node_id: str, finished_quantity: float = 1.0) -> Genealogy:
    """从成品批次/库存所属批次反查原料分摊。

    ``finished_quantity`` 为成品数量；``raw_equivalent_quantity`` 是逐段还原到
    原料批次的折合数量，``share`` 是不考虑损耗的来源占比（各原料之和为 1）。
    """
    raw_accumulator: dict[str, float] = {}
    share_accumulator: dict[str, float] = {}
    paths: list[list[PathStep]] = []
    _walk_back(journal, node_id, [], 1.0, 1.0, raw_accumulator, share_accumulator, paths)
    shares = [
        RawShare(lot_id=lot_id, supplier_id=journal.lot_supplier(lot_id),
                 share=share_accumulator[lot_id],
                 raw_equivalent_quantity=finished_quantity * raw_accumulator[lot_id])
        for lot_id in raw_accumulator
    ]
    shares.sort(key=lambda item: (-item.share, item.lot_id))
    return Genealogy(start_node=node_id, paths=paths, raw_shares=shares,
                     tainted=journal.is_tainted(node_id))


def trace_stock(journal: Journal, stock_unit_id: str) -> Genealogy | None:
    unit = journal.stock_units.get(stock_unit_id)
    if unit is None:
        return None
    return trace_back(journal, unit.batch_id, unit.quantity)


def downstream_batches(journal: Journal, lot_id: str) -> set[str]:
    """沿转换边正向收集使用了某原料（或在制批次）的全部批次。"""
    affected: set[str] = set()
    stack = [lot_id]
    while stack:
        source = stack.pop()
        for batch_id, event in journal.batch_producer.items():
            sources = {item["source_batch_id"] for item in event["payload"]["input_allocations"]}
            if source in sources and batch_id not in affected:
                affected.add(batch_id)
                stack.append(batch_id)
    return affected


def stock_current_location(journal: Journal, stock_unit_id: str) -> str:
    unit = journal.stock_units[stock_unit_id]
    location = unit.location
    movements = sorted(journal.stock_movements.get(stock_unit_id, []),
                       key=lambda event: parse(event["payload"]["moved_at"]))
    for event in movements:
        location = event["payload"]["to_location"]
    return location


def stock_impact(journal: Journal, batch_ids: set[str]) -> list[dict[str, Any]]:
    """给定受影响批次集合，给出真正流出的库存单元及其当前位置/冻结状态。"""
    impact: list[dict[str, Any]] = []
    held_by: dict[str, str] = {}
    for case_id, hold in journal.holds.items():
        for stock_id in hold["payload"].get("scope", {}).get("stock_unit_ids", []):
            held_by[stock_id] = case_id
    for stock_id, unit in journal.stock_units.items():
        if unit.batch_id not in batch_ids:
            continue
        impact.append({
            "stock_unit_id": stock_id,
            "product": unit.product,
            "quantity": unit.quantity,
            "unit": unit.unit,
            "current_location": stock_current_location(journal, stock_id),
            "batch_id": unit.batch_id,
            "frozen_by_case": held_by.get(stock_id),
        })
    impact.sort(key=lambda item: item["stock_unit_id"])
    return impact
