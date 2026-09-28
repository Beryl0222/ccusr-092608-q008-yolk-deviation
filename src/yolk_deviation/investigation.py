"""投诉追因：候选收窄、逐生产路径的证据/缺口比对与冻结范围。

结论只基于台账事实与当日规则：
- 已放行批次按观察发生时生效的规则判断，新阈值不翻历史案；
- 尚无放行结论的在制批次用现行阈值重判；
- 冻结必须由质量负责人在 ``STOCK_HELD`` 事件上确认，本模块只给范围建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .ledger import Journal
from .lineage import trace_back
from .rules import RuleBook, evaluate_at, observation_metric, observation_stage, reevaluate_with_current
from .timeutil import parse


@dataclass
class CauseFinding:
    code: str
    label: str
    status: str  # EXCLUDED / SUSPECTED / MISSING_EVIDENCE
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "label": self.label, "status": self.status, "detail": self.detail}


@dataclass
class CandidatePath:
    batch_id: str
    stock_units: list[dict[str, Any]]
    released: str | None
    historical_rule_versions: list[str]
    raw_shares: list[dict[str, Any]]
    causes: list[CauseFinding] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)
    tainted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "stock_units": self.stock_units,
            "released": self.released,
            "historical_rule_versions": self.historical_rule_versions,
            "raw_shares": self.raw_shares,
            "causes": [cause.as_dict() for cause in self.causes],
            "gaps": self.gaps,
            "tainted": self.tainted,
        }


def _location_at(journal: Journal, stock_id: str, when: Any) -> str | None:
    unit = journal.stock_units[stock_id]
    release_time = min((parse(e["payload"]["decided_at"]) for e in journal.releases.get(unit.batch_id, [])),
                       default=None)
    if release_time is not None and when < release_time:
        return None
    location = unit.location
    for event in sorted(journal.stock_movements.get(stock_id, []),
                        key=lambda e: parse(e["payload"]["moved_at"])):
        moved_at = parse(event["payload"]["moved_at"])
        if moved_at <= when:
            location = event["payload"]["to_location"]
        else:
            break
    return location


def narrow_candidates(journal: Journal, complaint: Mapping[str, Any]) -> list[CandidatePath]:
    """按品名、门店与购买时间收窄：购买时点确实在售于该门店的库存所属批次。"""
    body = complaint["payload"]
    purchased_at = parse(body["purchased_at"])
    store_id = body["store_id"]
    product = body["product"]
    per_batch: dict[str, list[dict[str, Any]]] = {}
    for stock_id, unit in journal.stock_units.items():
        if unit.product != product:
            continue
        location = _location_at(journal, stock_id, purchased_at)
        if location != store_id:
            continue
        per_batch.setdefault(unit.batch_id, []).append({
            "stock_unit_id": stock_id,
            "quantity": unit.quantity,
            "unit": unit.unit,
            "location_at_purchase": location,
        })
    candidates: list[CandidatePath] = []
    for batch_id, units in sorted(per_batch.items()):
        genealogy = trace_back(journal, batch_id)
        releases = journal.releases.get(batch_id, [])
        latest = max(releases, key=lambda e: parse(e["payload"]["decided_at"]), default=None)
        decision = latest["payload"]["decision"] if latest else None
        rule_versions = sorted({e["payload"].get("rule_version", "") for e in releases
                                if e["payload"].get("rule_version")})
        candidates.append(CandidatePath(
            batch_id=batch_id,
            stock_units=sorted(units, key=lambda item: item["stock_unit_id"]),
            released=decision,
            historical_rule_versions=rule_versions,
            raw_shares=[{"lot_id": share.lot_id, "supplier_id": share.supplier_id,
                         "share": round(share.share, 6),
                         "raw_equivalent_quantity": round(share.raw_equivalent_quantity, 3)}
                        for share in genealogy.raw_shares],
            tainted=genealogy.tainted,
        ))
    return candidates


def _path_nodes(candidate: CandidatePath, journal: Journal) -> tuple[set[str], set[str]]:
    """收集成品路径上的全部批次节点与原料节点。"""
    batches: set[str] = {candidate.batch_id}
    lots = {share["lot_id"] for share in candidate.raw_shares}
    stack = [candidate.batch_id]
    while stack:
        node = stack.pop()
        event = journal.batch_producer.get(node)
        if event is None:
            continue
        for item in event["payload"]["input_allocations"]:
            source = item["source_batch_id"]
            if source in journal.lots:
                lots.add(source)
            elif source not in batches:
                batches.add(source)
                stack.append(source)
    return batches, lots


def _observations_on(journal: Journal, node_id: str) -> list[Mapping[str, Any]]:
    return [event for event in journal.observations
            if event["payload"].get("subject_id") == node_id]


def _verdict_for(book: RuleBook, observation: Mapping[str, Any], released: bool) -> tuple[str, str]:
    """返回 (结论, 依据规则版本)；已放行用观察当日规则，在制用现行规则。"""
    if released:
        when = parse(observation["payload"]["observed_at"])
        verdict = evaluate_at(book, observation, when)
        return verdict or "NO_RULE", observation["payload"].get("rule_version", "")
    verdict, rule = reevaluate_with_current(book, observation)
    return verdict or "NO_RULE", (rule.rule_version if rule else "")


def _matching_observations(journal: Journal, book: RuleBook, nodes: set[str], stage: str,
                           metric: str, released: bool) -> list[tuple[Mapping[str, Any], str, str]]:
    found: list[tuple[Mapping[str, Any], str, str]] = []
    for node in nodes:
        for observation in _observations_on(journal, node):
            payload = observation["payload"]
            if observation_stage(observation) == stage and observation_metric(observation) == metric:
                verdict, rule_version = _verdict_for(book, observation, released)
                found.append((observation, verdict, rule_version))
    return found


def investigate_path(journal: Journal, candidate: CandidatePath, book: RuleBook) -> None:
    """就地填充一条候选路径的原因排除与证据缺口。"""
    batches, lots = _path_nodes(candidate, journal)
    released = candidate.released in ("RELEASED", "CONDITIONAL")
    plan = book.evidence_plan

    # 供应商证明：原料接收时必须登记。
    cert_missing = [lot_id for lot_id in lots
                    if not journal.lots[lot_id]["payload"].get("supplier_certificate_refs")]
    if cert_missing:
        candidate.gaps.append({"type": "missing_certificate",
                               "detail": "原料缺少供应商证明: " + ", ".join(sorted(cert_missing))})

    for cause in plan.get("causes", []):
        stage, metric = cause["stage"], cause["metric"]
        scope = cause.get("scope", "batch")
        if scope == "input_lots":
            nodes: set[str] = set(lots)
        elif scope == "stock":
            nodes = {unit["stock_unit_id"] for unit in candidate.stock_units}
        else:
            # 该指标只在真正经历过该工序的节点上产生：用台账记录的工序列表过滤。
            nodes = {node for node in batches if stage in journal.batch_steps.get(node, [])}
        matches = _matching_observations(journal, book, nodes, stage, metric, released)
        covered = {m[0]["payload"]["subject_id"] for m in matches}
        missing_nodes = sorted(nodes - covered)

        def _exemption_note(observation: Mapping[str, Any]) -> str:
            for exemption in journal.exemptions:
                if exemption["payload"].get("observation_id") != observation["aggregate_id"]:
                    continue
                if exemption["payload"].get("decision") != "APPROVED":
                    continue
                return (f"；该偏差已由 {exemption['payload']['granted_by']}"
                        f"（非工序负责人 {exemption['payload']['step_owner']}）于 "
                        f"{exemption['payload']['granted_at']} 豁免")
            return ""

        out = [(observation, rule_version) for observation, verdict, rule_version in matches if verdict == "OUT"]
        if out and not missing_nodes:
            obs_ids = ", ".join(observation["aggregate_id"] + _exemption_note(observation)
                                for observation, _ in out)
            candidate.causes.append(CauseFinding(
                cause["code"], cause["label"], "SUSPECTED",
                f"路径上存在越限测量（{obs_ids}），按测量当日已生效阈值判定（历史放行不翻案）" if released
                else f"路径上存在按现行阈值越限的测量（{obs_ids}）"))
        elif out:
            obs_ids = ", ".join(observation["aggregate_id"] + _exemption_note(observation)
                                for observation, _ in out)
            candidate.causes.append(CauseFinding(
                cause["code"], cause["label"], "SUSPECTED",
                f"存在越限测量（{obs_ids}），且部分节点缺测"))
        elif matches and not missing_nodes:
            candidate.causes.append(CauseFinding(
                cause["code"], cause["label"], "EXCLUDED",
                "路径上各节点该工序测量均在当日限值内" if released
                else "路径上各节点该工序测量均在现行限值内"))
        elif matches:
            candidate.causes.append(CauseFinding(
                cause["code"], cause["label"], "MISSING_EVIDENCE",
                f"已有测量合格，但以下节点缺少 {stage}/{metric} 测量: {', '.join(missing_nodes)}"))
        else:
            candidate.causes.append(CauseFinding(
                cause["code"], cause["label"], "MISSING_EVIDENCE",
                f"整条路径缺少 {stage}/{metric} 测量，无法排除该原因"))

    # 留样缺口：计划要求的工序必须有留样且 retained=true。
    for requirement in plan.get("samples_required", []):
        stage = requirement["stage"]
        retained = [event for event in journal.samples.values()
                    if event["payload"].get("stage") == stage
                    and event["payload"].get("source_batch_id") in batches
                    and event["payload"].get("retained") is True]
        if not retained:
            candidate.gaps.append({
                "type": "missing_retained_sample",
                "detail": f"工序 {stage} 缺少可追溯留样，投诉无法在该工序复核",
            })

    # 放行决定链：每条质量决定都要能从成品反查。
    if not journal.releases.get(candidate.batch_id):
        candidate.gaps.append({"type": "missing_release_decision",
                               "detail": "成品批次查不到放行决定记录"})
    if journal.is_tainted(candidate.batch_id):
        candidate.gaps.append({"type": "conservation_broken",
                               "detail": "该路径存在无法守恒的数量，台账已阻断放行"})


def investigate(journal: Journal, complaint: Mapping[str, Any], book: RuleBook) -> dict[str, Any]:
    candidates = narrow_candidates(journal, complaint)
    for candidate in candidates:
        investigate_path(journal, candidate, book)
    rendered = [candidate.as_dict() for candidate in candidates]
    # 只有存在越限证据或数量污染的候选才进入建议冻结范围；纯证据缺口候选继续补证。
    flagged = [
        candidate for candidate in candidates
        if candidate.tainted or any(cause.status == "SUSPECTED" for cause in candidate.causes)
    ]
    flagged_units = sorted({
        unit["stock_unit_id"] for candidate in flagged for unit in candidate.stock_units
    })
    all_store_units = sorted({
        unit["stock_unit_id"] for candidate in candidates for unit in candidate.stock_units
    })
    return {
        "complaint_id": complaint["aggregate_id"],
        "narrowing": {
            "product": complaint["payload"]["product"],
            "store_id": complaint["payload"]["store_id"],
            "purchased_at": complaint["payload"]["purchased_at"],
        },
        "candidate_count": len(candidates),
        "candidates": rendered,
        "excluded_causes_overall": sorted(
            code for code in {cause["code"] for candidate in rendered for cause in candidate["causes"]}
            if all(
                next((c["status"] for c in candidate["causes"] if c["code"] == code), None) == "EXCLUDED"
                for candidate in rendered)
        ),
        "open_gaps": [
            {"batch_id": candidate["batch_id"], **gap}
            for candidate in rendered for gap in candidate["gaps"]
        ],
        "suggested_freeze_scope": {
            "stock_unit_ids": flagged_units,
            "other_in_store_units": sorted(set(all_store_units) - set(flagged_units)),
            "note": "仅为建议范围；须由质量负责人确认后通过 STOCK_HELD 冻结，冻结后处置决定前禁止移动。"
                    "未列入的候选仅存在证据缺口，应先补留样/补测量。",
        },
    }
