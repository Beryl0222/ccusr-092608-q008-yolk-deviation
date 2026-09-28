"""台账装载、跨事件不变量与数量守恒闸门。

只做事实核对，不做业务处置：任何无法守恒的数量都会把下游批次标记为
``tainted``，其放行事件判为阻断；证据缺口类判断在 investigation 层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import validate_event
from .rules import RuleBook, observation_metric, observation_stage, rule_at
from .timeutil import parse

# 烘烤失水等工艺损耗允许的相对误差；超过即判定无法守恒。
CONSERVATION_TOLERANCE = 0.005


@dataclass(frozen=True)
class LedgerIssue:
    code: str
    message: str
    event_id: str | None = None
    ref: str | None = None
    blocking: bool = True

    def as_dict(self) -> dict[str, str | bool | None]:
        return {"code": self.code, "message": self.message,
                "event_id": self.event_id, "ref": self.ref, "blocking": self.blocking}


@dataclass
class StockUnit:
    stock_unit_id: str
    product: str
    quantity: float
    unit: str
    location: str
    release_event_id: str
    batch_id: str


@dataclass
class Journal:
    events: list[Mapping[str, Any]]
    issues: list[LedgerIssue] = field(default_factory=list)
    lots: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    # batch_id -> 产出该批次的转换事件
    batch_producer: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    # batch_id -> 投入明细（来源批次、数量、占比），按事件顺序累积
    batch_inputs: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    batch_steps: dict[str, list[str]] = field(default_factory=dict)
    samples: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    observations: list[Mapping[str, Any]] = field(default_factory=list)
    releases: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    exemptions: list[Mapping[str, Any]] = field(default_factory=list)
    stock_units: dict[str, StockUnit] = field(default_factory=dict)
    stock_movements: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    holds: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    dispositions: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    complaints: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    # 批次/原料截至每个事件序号是否已被污染（欠量、错账、上游污染）
    _available: dict[str, float] = field(default_factory=dict)
    _released: dict[str, float] = field(default_factory=dict)
    _unit: dict[str, str] = field(default_factory=dict)
    _tainted: dict[str, bool] = field(default_factory=dict)

    def is_tainted(self, node_id: str) -> bool:
        return self._tainted.get(node_id, False)

    def node_kind(self, node_id: str) -> str | None:
        if node_id in self.lots:
            return "raw_yolk_lot"
        if node_id in self.batch_producer:
            return "process_batch"
        return None

    def lot_supplier(self, lot_id: str) -> str | None:
        event = self.lots.get(lot_id)
        return event["payload"].get("supplier_id") if event else None


def _event_sort_key(event: Mapping[str, Any]) -> tuple:
    return (parse(event["occurred_at"]), event["event_id"])


def load_journal(raw_events: Any, schema: Mapping[str, Any],
                 rule_book: RuleBook | None = None) -> Journal:
    """校验并装载整本台账；事件按发生时间排序，不修改输入。

    提供 ``rule_book`` 时，放行闸门按放行时点已生效的阈值复核越限观察。
    """
    issues: list[LedgerIssue] = []
    if not isinstance(raw_events, list):
        return Journal(events=[], issues=[LedgerIssue("journal_not_array",
                                                      "台账必须是事件数组")])
    seen_ids: set[str] = set()
    prepared: list[Mapping[str, Any]] = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, Mapping):
            issues.append(LedgerIssue("event_not_object", f"第 {index + 1} 条记录不是对象"))
            continue
        for issue in validate_event(event, schema):
            issues.append(LedgerIssue(issue.code, issue.message,
                                      event.get("event_id") if isinstance(event.get("event_id"), str) else None,
                                      ref=issue.field))
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen_ids:
                issues.append(LedgerIssue("duplicate_event_id", "事件标识重复", event_id))
            seen_ids.add(event_id)
        prepared.append(event)

    prepared.sort(key=_event_sort_key)
    journal = Journal(events=prepared, issues=issues)
    _check_version_chains(journal)
    _build(journal, rule_book)
    journal.issues.sort(key=lambda item: (item.event_id or "", item.code, item.ref or ""))
    return journal


def _ancestor_nodes(journal: Journal, batch_id: str) -> set[str]:
    nodes: set[str] = set()
    stack = [batch_id]
    while stack:
        node = stack.pop()
        event = journal.batch_producer.get(node)
        if event is None:
            continue
        for item in event["payload"]["input_allocations"]:
            source = item["source_batch_id"]
            if source not in nodes:
                nodes.add(source)
                stack.append(source)
    return nodes


def _add(journal: Journal, issue: LedgerIssue) -> None:
    journal.issues.append(issue)


def _check_version_chains(journal: Journal) -> None:
    chains: dict[str, list[Mapping[str, Any]]] = {}
    for event in journal.events:
        chains.setdefault(event["aggregate_id"], []).append(event)
    for aggregate_id, chain in chains.items():
        versions = [event["version"] for event in chain]
        expected = list(range(1, len(chain) + 1))
        if versions != expected:
            _add(journal, LedgerIssue(
                "version_chain_broken",
                f"聚合 {aggregate_id} 版本号必须从 1 连续递增，实际为 {versions}",
                chain[0]["event_id"], ref=aggregate_id))
        agg_type = chain[0]["aggregate_type"]
        # 复检只能追加新观察：每个观察聚合只允许一个事件。
        if agg_type == "quality_observation" and len(chain) != 1:
            _add(journal, LedgerIssue(
                "observation_must_be_append_only",
                "复检必须以新观察追加，禁止改写既有观察",
                chain[1]["event_id"], ref=aggregate_id))


def _node_exists(journal: Journal, node_id: str) -> bool:
    return node_id in journal.lots or node_id in journal.batch_producer


def _consume(journal: Journal, node_id: str, quantity: float, unit: str,
             event: Mapping[str, Any], what: str) -> bool:
    """登记某节点的数量流出；超额即欠量并污染该节点，返回是否发生欠量。"""
    if not _node_exists(journal, node_id):
        return False  # 引用问题已在别处登记
    expected_unit = journal._unit.get(node_id)
    if expected_unit and unit and unit != expected_unit:
        _add(journal, LedgerIssue(
            "unit_mismatch", f"{what}计量单位 {unit} 与节点 {node_id} 的 {expected_unit} 不一致",
            event["event_id"], ref=node_id))
    available = journal._available.get(node_id, 0.0)
    used = available - quantity
    overdrawn = used < -CONSERVATION_TOLERANCE * max(available, quantity, 1.0)
    if overdrawn:
        journal._tainted[node_id] = True
        _add(journal, LedgerIssue(
            "quantity_not_conserved",
            f"节点 {node_id} 流出 {quantity}{unit or ''} 超过可用 {available:.3f}，数量无法守恒，"
            "禁止继续向下游放行",
            event["event_id"], ref=node_id))
    journal._available[node_id] = max(used, 0.0)
    return overdrawn


def _build(journal: Journal, rule_book: RuleBook | None = None) -> None:
    for event in journal.events:
        etype = event["event_type"]
        body = event["payload"]
        if etype == "LOT_ACCEPTED":
            lot_id = event["aggregate_id"]
            if lot_id in journal.lots or lot_id in journal.batch_producer:
                _add(journal, LedgerIssue("node_id_redefined", f"节点 {lot_id} 重复定义",
                                          event["event_id"], ref=lot_id))
            journal.lots[lot_id] = event
            journal._available[lot_id] = float(body["quantity"])
            journal._unit[lot_id] = body["unit"]
        elif etype == "BATCH_TRANSFORMED":
            _build_transform(journal, event)
        elif etype == "SAMPLE_DRAWN":
            _build_sample(journal, event)
        elif etype == "MATERIAL_SCRAPPED":
            _build_scrap(journal, event)
        elif etype == "OBSERVATION_RECORDED":
            journal.observations.append(event)
        elif etype == "RELEASE_DECIDED":
            _build_release(journal, event)
        elif etype == "DEVIATION_EXEMPTION_GRANTED":
            journal.exemptions.append(event)
            _build_exemption(journal, event)
        elif etype == "STOCK_MOVED":
            _build_movement(journal, event)
        elif etype == "STOCK_HELD":
            _build_hold(journal, event)
        elif etype == "DISPOSITION_APPROVED":
            _build_disposition(journal, event)
        elif etype == "COMPLAINT_FILED":
            journal.complaints[event["aggregate_id"]] = event

    _check_release_gates(journal, rule_book)


def _build_transform(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    wip_id = event["aggregate_id"]
    output_ids = [item["batch_id"] for item in body["outputs"]]
    if wip_id not in output_ids:
        _add(journal, LedgerIssue(
            "transform_aggregate_not_in_outputs",
            "转换事件的在制批次必须出现在产出列表中", event["event_id"], ref=wip_id))
    input_total = 0.0
    tainted = False
    units: set[str] = set()
    for item in body["input_allocations"]:
        source = item["source_batch_id"]
        if not _node_exists(journal, source):
            _add(journal, LedgerIssue(
                "unknown_source_batch", f"来源批次 {source} 尚未登记", event["event_id"], ref=source))
            tainted = True
            continue
        if journal.is_tainted(source) or _consume(
                journal, source, float(item["quantity"]),
                item.get("unit") or journal._unit.get(source, ""), event, "投入"):
            tainted = True
        input_total += float(item["quantity"])
        units.add(journal._unit.get(source, ""))
    output_total = sum(float(item["quantity"]) for item in body["outputs"])
    loss = float(body.get("loss_quantity", 0.0))
    gap = input_total - output_total - loss
    if abs(gap) > CONSERVATION_TOLERANCE * max(input_total, 1.0):
        tainted = True
        _add(journal, LedgerIssue(
            "quantity_not_conserved",
            f"工序 {body['step']} 投入 {input_total:.3f} ≠ 产出 {output_total:.3f} + 损耗 {loss:.3f}，"
            "无法说明去向，禁止继续向下游放行",
            event["event_id"], ref=wip_id))
    if loss < 0:
        _add(journal, LedgerIssue("invalid_loss", "损耗数量不能为负", event["event_id"], ref=wip_id))
    for item in body["outputs"]:
        batch_id = item["batch_id"]
        if batch_id in journal.lots or batch_id in journal.batch_producer:
            _add(journal, LedgerIssue("node_id_redefined", f"批次 {batch_id} 重复定义",
                                      event["event_id"], ref=batch_id))
            tainted = True
            continue
        journal.batch_producer[batch_id] = event
        journal.batch_inputs[batch_id] = list(body["input_allocations"])
        journal.batch_steps.setdefault(batch_id, []).append(body["step"])
        journal._available[batch_id] = float(item["quantity"])
        journal._unit[batch_id] = item.get("unit") or next(iter(u for u in units if u), "")
        if tainted:
            journal._tainted[batch_id] = True


def _build_sample(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    source = body["source_batch_id"]
    if not _node_exists(journal, source):
        _add(journal, LedgerIssue("unknown_source_batch", f"抽样来源 {source} 尚未登记",
                                  event["event_id"], ref=source))
        return
    sample_id = body["sample_id"]
    if sample_id in journal.samples:
        _add(journal, LedgerIssue("duplicate_sample_id", f"样品 {sample_id} 重复登记",
                                  event["event_id"], ref=sample_id))
    journal.samples[sample_id] = event
    _consume(journal, source, float(body["quantity_consumed"]), body["unit"], event, "抽样消耗")


def _build_scrap(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    source = body["source_batch_id"]
    if not _node_exists(journal, source):
        _add(journal, LedgerIssue("unknown_source_batch", f"报废来源 {source} 尚未登记",
                                  event["event_id"], ref=source))
        return
    _consume(journal, source, float(body["quantity"]), body["unit"], event, "报废")


def _build_release(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    batch_id = body["batch_id"]
    if not _node_exists(journal, batch_id) or batch_id in journal.lots:
        _add(journal, LedgerIssue("unknown_batch", f"放行对象 {batch_id} 不是已登记的在制批次",
                                  event["event_id"], ref=batch_id))
    journal.releases.setdefault(batch_id, []).append(event)
    if body.get("decision") == "REJECTED" and body.get("produced_stock"):
        _add(journal, LedgerIssue("rejected_release_has_stock",
                                  "拒绝放行的批次不得登记入库库存", event["event_id"], ref=batch_id))
    if body.get("decision") not in ("RELEASED", "CONDITIONAL"):
        return
    for index, unit in enumerate(body.get("produced_stock", [])):
        prefix = f"payload.produced_stock[{index}]"
        stock_id = unit.get("stock_unit_id")
        if not isinstance(stock_id, str) or not stock_id.strip():
            _add(journal, LedgerIssue("required", "库存单元标识必填", event["event_id"], ref=prefix))
            continue
        if stock_id in journal.stock_units:
            _add(journal, LedgerIssue("duplicate_stock_unit", f"库存单元 {stock_id} 重复登记",
                                      event["event_id"], ref=stock_id))
            continue
        qty = float(unit.get("quantity", 0.0))
        if qty <= 0:
            _add(journal, LedgerIssue("positive_number", "入库数量必须为正",
                                      event["event_id"], ref=f"{prefix}.quantity"))
        stock_unit_name = unit.get("unit") or journal._unit.get(batch_id, "")
        if stock_unit_name and journal._unit.get(batch_id) and stock_unit_name != journal._unit[batch_id]:
            _add(journal, LedgerIssue(
                "unit_mismatch",
                f"库存单元计量单位 {stock_unit_name} 与批次 {batch_id} 的 {journal._unit[batch_id]} 不一致",
                event["event_id"], ref=stock_id))
        journal.stock_units[stock_id] = StockUnit(
            stock_unit_id=stock_id, product=unit.get("product", ""),
            quantity=qty, unit=stock_unit_name,
            location=unit.get("location", ""), release_event_id=event["event_id"],
            batch_id=batch_id)
        journal.stock_movements[stock_id] = []
        _consume(journal, batch_id, qty, stock_unit_name, event, "入库")
    journal._released[batch_id] = journal._released.get(batch_id, 0.0) + sum(
        float(u.get("quantity", 0.0)) for u in body.get("produced_stock", []))


def _build_exemption(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    batch_id = body["batch_id"]
    observation_id = body["observation_id"]
    observation = next((e for e in journal.observations if e["aggregate_id"] == observation_id), None)
    if observation is None:
        _add(journal, LedgerIssue("unknown_observation", f"豁免依据的观察 {observation_id} 不存在",
                                  event["event_id"], ref=observation_id))
    elif observation["payload"].get("subject_id") != batch_id:
        _add(journal, LedgerIssue(
            "exemption_observation_subject_mismatch",
            "豁免只能针对本批次上的观察", event["event_id"], ref=observation_id))
    if _node_exists(journal, batch_id) and body["step"] not in journal.batch_steps.get(batch_id, []):
        _add(journal, LedgerIssue("unknown_step",
                                  f"批次 {batch_id} 不存在工序 {body['step']}",
                                  event["event_id"], ref=batch_id))


def _build_movement(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    stock_id = body["stock_unit_id"]
    unit = journal.stock_units.get(stock_id)
    if unit is None:
        _add(journal, LedgerIssue("unknown_stock_unit", f"库存单元 {stock_id} 尚未入库",
                                  event["event_id"], ref=stock_id))
        return
    case_holding = next((case_id for case_id, hold in journal.holds.items()
                         if stock_id in hold["payload"].get("scope", {}).get("stock_unit_ids", [])), None)
    if case_holding and case_holding not in journal.dispositions:
        _add(journal, LedgerIssue(
            "stock_movement_blocked",
            f"库存 {stock_id} 已被处置案 {case_holding} 冻结，处置决定前禁止移动",
            event["event_id"], ref=stock_id))
    journal.stock_movements[stock_id].append(event)


def _build_hold(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    case_id = event["aggregate_id"]
    if case_id in journal.holds:
        _add(journal, LedgerIssue("hold_already_active", f"处置案 {case_id} 已冻结过库存",
                                  event["event_id"], ref=case_id))
    if body.get("held_by_role") != "quality_lead":
        _add(journal, LedgerIssue(
            "freeze_requires_quality_lead",
            "只有质量负责人确认处置范围后才能冻结库存", event["event_id"], ref=case_id))
    for stock_id in body.get("scope", {}).get("stock_unit_ids", []):
        if stock_id not in journal.stock_units:
            _add(journal, LedgerIssue("unknown_stock_unit", f"冻结范围中的 {stock_id} 尚未入库",
                                      event["event_id"], ref=stock_id))
    journal.holds[case_id] = event


def _build_disposition(journal: Journal, event: Mapping[str, Any]) -> None:
    body = event["payload"]
    case_id = body["case_id"]
    if case_id not in journal.holds:
        _add(journal, LedgerIssue("disposition_without_hold",
                                  "处置决定必须基于已确认并冻结的范围", event["event_id"], ref=case_id))
    if body.get("approved_by_role") != "quality_lead":
        _add(journal, LedgerIssue("disposition_requires_quality_lead",
                                  "处置决定必须由质量负责人批准", event["event_id"], ref=case_id))
    journal.dispositions[case_id] = event


def _check_release_gates(journal: Journal, rule_book: RuleBook | None = None) -> None:
    """放行时的污染与未豁免偏差闸门（按放行时点已生效的阈值判断）。

    无规则册时退回观察载荷自带的 ``verdict``；有规则册时对成品路径
    （含上游在制批次与原料）上放行前的每条测量按当日阈值重算。
    """
    exempted_observations: dict[str, list[Mapping[str, Any]]] = {}
    for event in sorted(journal.events, key=lambda item: parse(item["occurred_at"])):
        if event["event_type"] == "DEVIATION_EXEMPTION_GRANTED":
            exempted_observations.setdefault(event["payload"]["batch_id"], []).append(event)
        elif event["event_type"] == "RELEASE_DECIDED" and event["payload"]["decision"] in ("RELEASED", "CONDITIONAL"):
            body = event["payload"]
            batch_id = body["batch_id"]
            if journal.is_tainted(batch_id):
                _add(journal, LedgerIssue(
                    "release_blocked_by_imbalance",
                    f"批次 {batch_id} 或其上游存在无法守恒的数量，不得放行",
                    event["event_id"], ref=batch_id))
            decision_time = parse(body["decided_at"])
            path_nodes = _ancestor_nodes(journal, batch_id) | {batch_id}
            for observation in journal.observations:
                payload = observation["payload"]
                subject_id = payload.get("subject_id")
                if subject_id not in path_nodes:
                    continue
                if parse(payload["observed_at"]) >= decision_time:
                    continue
                if rule_book is not None:
                    rule = rule_at(rule_book, observation_metric(observation),
                                   observation_stage(observation), decision_time)
                    if rule is None:
                        continue  # 当日无适用阈值：记为证据缺口，由追因层提示
                    value = payload.get("result", {}).get("value")
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        continue
                    out_of_limit = rule.evaluate(float(value)) == "OUT"
                else:
                    out_of_limit = payload.get("verdict") == "OUT"
                if not out_of_limit:
                    continue
                valid = any(
                    exemption["payload"].get("observation_id") == observation["aggregate_id"]
                    and exemption["payload"].get("granted_by") != exemption["payload"].get("step_owner")
                    and parse(exemption["payload"]["granted_at"]) <= decision_time
                    and exemption["payload"].get("decision", "APPROVED") == "APPROVED"
                    for exemption in exempted_observations.get(subject_id, [])
                    + exempted_observations.get(batch_id, []))
                if not valid:
                    _add(journal, LedgerIssue(
                        "release_blocked_by_unresolved_deviation",
                        f"批次 {batch_id} 的生产路径存在越限观察 {observation['aggregate_id']}，"
                        "放行前需由非工序负责人追加有效豁免",
                        event["event_id"], ref=observation["aggregate_id"]))
