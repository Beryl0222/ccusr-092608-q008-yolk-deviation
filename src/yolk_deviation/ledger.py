"""事件重放：库存账本、守恒闸门与质量决定校验。

事件是只追加事实；本模块把事件流重放成当前状态，并在重放过程中收集所有
阻断性问题（数量不守恒、超领、自批豁免、阈值错版等），不抛出、不改写输入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import validate_event

ABS_TOLERANCE = 1e-9
REL_TOLERANCE = 1e-6

PRODUCT = "product"
SAMPLE = "sample"
SCRAP = "scrap"
LOSS = "loss"
NON_PRODUCT_ROLES = (SAMPLE, SCRAP, LOSS)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def quantities_equal(left: float, right: float) -> bool:
    return abs(left - right) <= ABS_TOLERANCE + REL_TOLERANCE * max(abs(left), abs(right), 1.0)


@dataclass(frozen=True)
class Violation:
    event_id: str
    code: str
    message: str


@dataclass
class BatchState:
    batch_id: str
    unit: str
    available: float = 0.0
    origins: dict[str, float] = field(default_factory=dict)
    blocked: bool = False
    producing_event: str | None = None
    stage: str | None = None
    produced: float = 0.0


@dataclass
class TransformRecord:
    event_id: str
    step_id: str
    stage: str
    unit: str
    inputs: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    occurred_at: datetime
    equipment_program: str | None
    params: Mapping[str, Any]


@dataclass
class ObservationRecord:
    event_id: str
    aggregate_type: str
    aggregate_id: str
    metric: str
    method_version: str
    result: Any
    occurred_at: datetime
    step_id: str | None


@dataclass
class ShipmentRecord:
    event_id: str
    store_id: str
    dispatched_at: datetime
    sell_by: datetime
    allocations: list[dict[str, Any]]


@dataclass
class Ledger:
    batches: dict[str, BatchState] = field(default_factory=dict)
    raw_meta: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    thresholds: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    transforms: dict[str, TransformRecord] = field(default_factory=dict)
    observations: list[ObservationRecord] = field(default_factory=list)
    releases: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    exemptions: list[Mapping[str, Any]] = field(default_factory=list)
    shipments: list[ShipmentRecord] = field(default_factory=list)
    complaints: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    holds: list[Mapping[str, Any]] = field(default_factory=list)
    dispositions: list[Mapping[str, Any]] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    invalid_exemption_events: set[str] = field(default_factory=set)
    step_sample_remaining: dict[str, float] = field(default_factory=dict)
    step_unit: dict[str, str] = field(default_factory=dict)
    shipped_by_batch: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def _issue(self, event_id: str, code: str, message: str) -> None:
        self.violations.append(Violation(event_id, code, message))

    # -- 阈值 -------------------------------------------------------------

    def pinned_threshold(self, metric: str, version: int) -> Mapping[str, Any] | None:
        for item in self.thresholds.get(metric, []):
            if item["version"] == version:
                return item
        return None

    def latest_effective_threshold(self, metric: str, at: datetime) -> Mapping[str, Any] | None:
        candidates = [t for t in self.thresholds.get(metric, []) if t["effective_from"] <= at]
        return max(candidates, key=lambda t: t["version"], default=None)

    def latest_observation(self, target_type: str, target_id: str, metric: str, at: datetime) -> ObservationRecord | None:
        rows = [
            o
            for o in self.observations
            if o.aggregate_type == target_type
            and o.aggregate_id == target_id
            and o.metric == metric
            and o.occurred_at <= at
        ]
        return max(rows, key=lambda o: o.occurred_at, default=None)

    def result_conforms(self, result: Any, limits: Mapping[str, Any]) -> bool:
        if not isinstance(result, (int, float)) or isinstance(result, bool):
            return True
        if "min" in limits and result < limits["min"]:
            return False
        if "max" in limits and result > limits["max"]:
            return False
        return True

    def upstream_batches(self, batch_id: str) -> list[str]:
        """一次生产路径上直接上游的批次编号。"""
        for record in self.transforms.values():
            if any(o.get("batch_id") == batch_id and o.get("role", PRODUCT) == PRODUCT for o in record.outputs):
                return [item["source_batch"] for item in record.inputs]
        return []

    def producing_transform(self, batch_id: str) -> TransformRecord | None:
        for record in self.transforms.values():
            if any(o.get("batch_id") == batch_id and o.get("role", PRODUCT) == PRODUCT for o in record.outputs):
                return record
        return None


# -- 重放 -------------------------------------------------------------------


def replay(events: Sequence[Mapping[str, Any]], schema: Mapping[str, Any]) -> Ledger:
    ledger = Ledger()
    ordered = sorted(enumerate(events), key=lambda pair: (parse_ts(pair[1]["occurred_at"]), pair[0]))
    seen_event_ids: set[str] = set()
    seen_versions: dict[tuple[str, str], int] = {}

    for _, event in ordered:
        event_id = event["event_id"]
        if event_id in seen_event_ids:
            ledger._issue(event_id, "duplicate_event", "事件标识重复")
            continue
        seen_event_ids.add(event_id)

        for issue in validate_event(event, schema):
            ledger._issue(event_id, f"contract.{issue.code}", issue.message)

        agg_key = (event["aggregate_type"], event["aggregate_id"])
        expected = seen_versions.get(agg_key, 0) + 1
        if event["version"] != expected:
            ledger._issue(
                event_id,
                "version_gap",
                f"聚合版本必须从 1 连续递增，期望 {expected}，实际 {event['version']}",
            )
        seen_versions[agg_key] = max(seen_versions.get(agg_key, 0), event["version"])

        handler = _HANDLERS.get(event["event_type"])
        if handler is not None:
            handler(ledger, event)

    return ledger


def _on_lot_accepted(ledger: Ledger, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    quantity = body["quantity"]
    lot_id = event["aggregate_id"]
    value, unit = float(quantity["value"]), quantity["unit"]
    if value <= 0:
        ledger._issue(event["event_id"], "non_positive_quantity", "原料批数量必须为正")
    if lot_id in ledger.batches:
        ledger._issue(event["event_id"], "duplicate_lot", "原料批重复登记")
        return
    ledger.batches[lot_id] = BatchState(
        batch_id=lot_id,
        unit=unit,
        available=value,
        origins={lot_id: 1.0},
        stage="raw",
    )
    ledger.raw_meta[lot_id] = body


def _on_threshold_published(ledger: Ledger, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    metric = body["metric"]
    entry = {
        "version": event["version"],
        "limits": body["limits"],
        "effective_from": parse_ts(body["effective_from"]),
        "method_version": body.get("method_version"),
        "event_id": event["event_id"],
    }
    ledger.thresholds.setdefault(metric, []).append(entry)


def _on_transformed(ledger: Ledger, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    event_id = event["event_id"]
    step_id = body["step_id"]
    unit = body["unit"]
    inputs = body["input_allocations"]
    outputs = body["outputs"]
    blocked = False

    if step_id in ledger.transforms:
        ledger._issue(event_id, "duplicate_step", "工序步骤重复记账")
        return

    input_total = 0.0
    weighted_origins: dict[str, float] = {}
    for item in inputs:
        source_id = item["source_batch"]
        qty = float(item["quantity"])
        input_total += qty
        source = ledger.batches.get(source_id)
        if source is None:
            ledger._issue(event_id, "unknown_source", f"来源批次 {source_id} 不存在")
            blocked = True
            continue
        if source.unit != unit:
            ledger._issue(event_id, "unit_mismatch", f"来源 {source_id} 单位 {source.unit} 与步骤单位 {unit} 不一致")
            blocked = True
        if qty <= 0:
            ledger._issue(event_id, "non_positive_quantity", "输入数量必须为正")
        if qty > source.available + ABS_TOLERANCE:
            ledger._issue(
                event_id,
                "insufficient_stock",
                f"来源 {source_id} 可用 {source.available}{source.unit}，不足领取 {qty}{unit}",
            )
            blocked = True
        if source.blocked:
            ledger._issue(event_id, "blocked_source", f"来源 {source_id} 的上游存在未闭合阻断，不得继续投产")
            blocked = True
        if input_total and source.origins and not blocked:
            for lot_id, share in source.origins.items():
                weighted_origins[lot_id] = weighted_origins.get(lot_id, 0.0) + share * qty
        source.available = max(0.0, source.available - qty)

    output_total = 0.0
    product_total = 0.0
    product_outputs: list[tuple[str, float]] = []
    sample_quantity = 0.0
    for item in outputs:
        role = item.get("role", PRODUCT)
        qty = float(item["quantity"])
        if qty < 0:
            ledger._issue(event_id, "negative_output", "输出数量不能为负")
        output_total += qty
        if role == PRODUCT:
            batch_id = item.get("batch_id")
            if not batch_id:
                ledger._issue(event_id, "output_without_batch", "产品输出必须给出批次编号")
                blocked = True
                continue
            if batch_id in ledger.batches:
                ledger._issue(event_id, "duplicate_output", f"批次 {batch_id} 已由其他步骤产出")
                blocked = True
            product_total += qty
            product_outputs.append((batch_id, qty))
        elif role in NON_PRODUCT_ROLES:
            if role == SAMPLE:
                sample_quantity += qty
        else:
            ledger._issue(event_id, "unknown_output_role", f"未知输出去向 {role}")

    if not quantities_equal(input_total, output_total):
        ledger._issue(
            event_id,
            "quantity_not_conserved",
            f"进出数量不守恒：输入合计 {input_total}{unit}，去向合计 {output_total}{unit}",
        )
        blocked = True
    if not product_outputs and not any(o.get("role") in NON_PRODUCT_ROLES for o in outputs):
        ledger._issue(event_id, "no_outputs", "转换事件没有任何登记去向")
        blocked = True

    for batch_id, qty in product_outputs:
        origins = (
            {lot_id: amount / input_total for lot_id, amount in weighted_origins.items()}
            if input_total and not blocked
            else {}
        )
        state = BatchState(
            batch_id=batch_id,
            unit=unit,
            available=qty,
            origins=origins,
            blocked=blocked,
            producing_event=event_id,
            stage=body.get("stage"),
            produced=qty,
        )
        ledger.batches[batch_id] = state

    ledger.step_unit[step_id] = unit
    ledger.step_sample_remaining[step_id] = sample_quantity
    ledger.transforms[step_id] = TransformRecord(
        event_id=event_id,
        step_id=step_id,
        stage=body.get("stage"),
        unit=unit,
        inputs=inputs,
        outputs=outputs,
        occurred_at=parse_ts(event["occurred_at"]),
        equipment_program=body.get("equipment_program"),
        params=body.get("params", {}),
    )


def _on_observation(ledger: Ledger, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    target = body["target"]
    target_type, target_id = target["aggregate_type"], target["aggregate_id"]
    if target_id not in ledger.batches:
        ledger._issue(event["event_id"], "unknown_target", f"观察对象 {target_id} 不存在")
    consumed = body.get("sample_consumed")
    step_id = None
    if consumed:
        step_id = consumed["step_id"]
        qty = float(consumed["quantity"])
        if step_id not in ledger.step_sample_remaining:
            ledger._issue(event["event_id"], "unknown_sample_step", f"抽样来源步骤 {step_id} 不存在")
        else:
            if ledger.step_unit[step_id] != consumed.get("unit", ledger.step_unit[step_id]):
                ledger._issue(event["event_id"], "unit_mismatch", "抽样消耗单位与步骤单位不一致")
            remaining = ledger.step_sample_remaining[step_id]
            if qty > remaining + ABS_TOLERANCE:
                ledger._issue(
                    event["event_id"],
                    "sample_over_consumed",
                    f"步骤 {step_id} 留样剩余 {remaining}，不足以消耗 {qty}",
                )
            else:
                ledger.step_sample_remaining[step_id] = remaining - qty
    ledger.observations.append(
        ObservationRecord(
            event_id=event["event_id"],
            aggregate_type=target_type,
            aggregate_id=target_id,
            metric=body["metric"],
            method_version=body["method_version"],
            result=body["result"],
            occurred_at=parse_ts(event["occurred_at"]),
            step_id=step_id,
        )
    )


def _on_release(ledger: Ledger, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    event_id = event["event_id"]
    batch_id = body["batch_id"]
    batch = ledger.batches.get(batch_id)
    if batch is None:
        ledger._issue(event_id, "unknown_batch", f"放行对象 {batch_id} 不存在")
        return
    if batch.blocked:
        ledger._issue(event_id, "blocked_release", "批次谱系上存在未闭合阻断，不得放行")

    at = parse_ts(event["occurred_at"])
    pinned = body.get("threshold_versions", {})

    closure = {batch_id}
    stack = [batch_id]
    while stack:
        for source_id in ledger.upstream_batches(stack.pop()):
            if source_id not in closure:
                closure.add(source_id)
                stack.append(source_id)

    failing_metrics: list[str] = []
    for metric, version in pinned.items():
        threshold = ledger.pinned_threshold(metric, version)
        if threshold is None:
            ledger._issue(event_id, "unknown_threshold_version", f"指标 {metric} 的阈值版本 {version} 不存在")
            continue
        if threshold["effective_from"] > at:
            ledger._issue(event_id, "threshold_not_effective", f"指标 {metric} 阈值在放行时刻尚未生效")
        current = ledger.latest_effective_threshold(metric, at)
        if current is None or current["version"] != version:
            ledger._issue(
                event_id,
                "threshold_not_current",
                f"指标 {metric} 放行时应钉版 {current['version'] if current else '无'}，实际 {version}",
            )
        observation = None
        for candidate in ledger.observations:
            if (
                candidate.aggregate_id in closure
                and candidate.metric == metric
                and candidate.occurred_at <= at
            ):
                if observation is None or candidate.occurred_at > observation.occurred_at:
                    observation = candidate
        if observation is None:
            ledger._issue(event_id, "missing_measurement", f"生产路径上缺少放行所需 {metric} 测量")
            continue
        if not ledger.result_conforms(observation.result, threshold["limits"]):
            failing_metrics.append(metric)

    for metric in failing_metrics:
        covering = [
            e
            for e in ledger.exemptions
            if e["batch_id"] in closure
            and e["metric"] == metric
            and parse_ts(e["granted_at"]) <= at
            and e["event_id"] not in ledger.invalid_exemption_events
        ]
        if not covering:
            ledger._issue(
                event_id,
                "release_without_exemption",
                f"指标 {metric} 超出当日阈值且没有在先偏差豁免",
            )

    ledger.releases.setdefault(batch_id, []).append(
        {
            "event_id": event_id,
            "at": at,
            "threshold_versions": dict(pinned),
            "decided_by": body["decided_by"],
            "conclusion": body.get("conclusion", "released"),
        }
    )


def _on_exemption(ledger: Ledger, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    if body["granted_by"] == body["responsible"]:
        ledger._issue(
            event["event_id"],
            "self_approval",
            f"{body['granted_by']} 不能批准自己负责工序 {body['step_id']} 的偏差豁免",
        )
        ledger.invalid_exemption_events.add(event["event_id"])
    if body["batch_id"] not in ledger.batches:
        ledger._issue(event["event_id"], "unknown_batch", f"豁免对象 {body['batch_id']} 不存在")
    ledger.exemptions.append(
        {
            "event_id": event["event_id"],
            "batch_id": body["batch_id"],
            "step_id": body["step_id"],
            "metric": body["metric"],
            "responsible": body["responsible"],
            "granted_by": body["granted_by"],
            "reason": body["reason"],
            "granted_at": event["occurred_at"],
        }
    )


def _on_shipment(ledger: Ledger, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    event_id = event["event_id"]
    allocations = []
    for item in body["allocations"]:
        batch_id = item["batch_id"]
        qty = float(item["quantity"])
        batch = ledger.batches.get(batch_id)
        if batch is None:
            ledger._issue(event_id, "unknown_batch", f"发运批次 {batch_id} 不存在")
            continue
        if batch.unit != item.get("unit", batch.unit):
            ledger._issue(event_id, "unit_mismatch", f"发运批次 {batch_id} 单位不一致")
        if batch.blocked:
            ledger._issue(event_id, "blocked_shipment", f"批次 {batch_id} 存在阻断，不得发运")
        if qty > batch.available + ABS_TOLERANCE:
            ledger._issue(event_id, "insufficient_stock", f"批次 {batch_id} 可用 {batch.available}，不足发运 {qty}")
        batch.available = max(0.0, batch.available - qty)
        allocations.append({"batch_id": batch_id, "quantity": qty})
        ledger.shipped_by_batch.setdefault(batch_id, []).append(
            {"store_id": body["store_id"], "quantity": qty, "event_id": event_id}
        )
    ledger.shipments.append(
        ShipmentRecord(
            event_id=event_id,
            store_id=body["store_id"],
            dispatched_at=parse_ts(event["occurred_at"]),
            sell_by=parse_ts(body["sell_by"]),
            allocations=allocations,
        )
    )


def _on_complaint(ledger: Ledger, event: Mapping[str, Any]) -> None:
    ledger.complaints[event["aggregate_id"]] = {
        "event_id": event["event_id"],
        "store_id": event["payload"]["store_id"],
        "purchased_at": parse_ts(event["payload"]["purchased_at"]),
        "symptoms": list(event["payload"].get("symptoms", [])),
        "sample_received": event["payload"].get("sample_received"),
        "sample_batch_id": event["payload"].get("sample_batch_id"),
    }


def _on_hold(ledger: Ledger, event: Mapping[str, Any]) -> None:
    ledger.holds.append(
        {
            "event_id": event["event_id"],
            "scope": event["payload"]["scope"],
            "reason": event["payload"]["reason"],
            "held_by": event["payload"]["held_by"],
            "at": parse_ts(event["occurred_at"]),
            "case_id": event["payload"].get("case_id"),
        }
    )


def _on_disposition(ledger: Ledger, event: Mapping[str, Any]) -> None:
    ledger.dispositions.append(
        {
            "event_id": event["event_id"],
            "case_id": event["aggregate_id"],
            "scope": event["payload"]["scope"],
            "decision": event["payload"]["decision"],
            "approved_by": event["payload"]["approved_by"],
            "at": parse_ts(event["occurred_at"]),
            "complaint_id": event["payload"].get("complaint_id"),
        }
    )


_HANDLERS = {
    "LOT_ACCEPTED": _on_lot_accepted,
    "THRESHOLD_PUBLISHED": _on_threshold_published,
    "BATCH_TRANSFORMED": _on_transformed,
    "OBSERVATION_RECORDED": _on_observation,
    "BATCH_RELEASED": _on_release,
    "DEVIATION_EXEMPTION_GRANTED": _on_exemption,
    "SHIPMENT_DISPATCHED": _on_shipment,
    "COMPLAINT_FILED": _on_complaint,
    "STOCK_HELD": _on_hold,
    "DISPOSITION_APPROVED": _on_disposition,
}
