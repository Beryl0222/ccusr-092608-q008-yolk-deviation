"""谱系计算与投诉研判。

账本重放给出"事实是否守恒"，本模块给出"差异可能从哪里来、影响到哪里去"：
任一成品可逆向展开原料分摊与逐段证据；投诉按门店/购买时间/样品收窄候选批次，
并核对每条生产路径上的证据与缺口；处置/冻结范围必须落在溯源可达集合内。
"""

from __future__ import annotations

from typing import Any, Mapping

from .ledger import PRODUCT, Ledger, ObservationRecord, TransformRecord

# 每个工序阶段在风味研判中应当拿出的证据。指标缺失即缺口；
# 指标存在且符合观察当日已生效阈值，则该成因被排除。
STAGE_EVIDENCE_PLAN: dict[str, list[str]] = {
    "raw": ["sensory_fishy"],
    "grading": ["sensory_fishy"],
    "brining": ["salt_pct", "sensory_fishy"],
    "thawing": ["water_pct"],
    "baking": ["sensory_texture"],
    "packing": ["salt_pct", "water_pct"],
}

SYMPTOM_STAGE_RELEVANCE: dict[str, list[str]] = {
    "fishy": ["raw", "grading", "brining"],
    "hard": ["brining", "thawing", "baking"],
    "oily_gritty": [],
}


def _metric_conforms(ledger: Ledger, observation: ObservationRecord) -> bool | None:
    threshold = ledger.latest_effective_threshold(observation.metric, observation.occurred_at)
    if threshold is None:
        return None
    return ledger.result_conforms(observation.result, threshold["limits"])


def raw_attribution(ledger: Ledger, batch_id: str, quantity: float | None = None) -> list[dict[str, Any]]:
    """成品批次到原料批的分摊；quantity 给定时换算成原料分摊数量。"""
    batch = ledger.batches.get(batch_id)
    if batch is None:
        return []
    base = quantity if quantity is not None else batch.produced
    rows = [
        {"raw_lot": lot_id, "share": share, "allocated_quantity": round(share * base, 6)}
        for lot_id, share in batch.origins.items()
    ]
    return sorted(rows, key=lambda row: (-row["share"], row["raw_lot"]))


def production_path(ledger: Ledger, batch_id: str) -> list[TransformRecord]:
    """从原料到成品的全部工序步骤序列（混批时覆盖所有输入分支）。"""
    chain: list[TransformRecord] = []
    seen: set[str] = set()

    def walk(current: str) -> None:
        record = ledger.producing_transform(current)
        if record is None or record.step_id in seen:
            return
        seen.add(record.step_id)
        for item in record.inputs:
            walk(item["source_batch"])
        chain.append(record)

    walk(batch_id)
    return chain


def upstream_closure(ledger: Ledger, batch_id: str) -> set[str]:
    """该批次的全部上游批次（含自身）。"""
    seen = {batch_id}
    stack = [batch_id]
    while stack:
        record = ledger.producing_transform(stack.pop())
        if record is None:
            continue
        for item in record.inputs:
            if item["source_batch"] not in seen:
                seen.add(item["source_batch"])
                stack.append(item["source_batch"])
    return seen


def downstream_batches(ledger: Ledger, batch_id: str) -> list[str]:
    """正向枚举吸收过该批次（含跨级传播）的全部下游批次。"""
    found: list[str] = []
    seen = {batch_id}
    frontier = [batch_id]
    while frontier:
        current = frontier.pop()
        for record in ledger.transforms.values():
            if any(i["source_batch"] == current for i in record.inputs):
                for out in record.outputs:
                    if out.get("role", PRODUCT) != PRODUCT:
                        continue
                    next_id = out["batch_id"]
                    if next_id not in seen:
                        seen.add(next_id)
                        found.append(next_id)
                        frontier.append(next_id)
    return sorted(seen - {batch_id})


def affected_stores(ledger: Ledger, batch_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    stores: dict[str, list[dict[str, Any]]] = {}
    closure = set(batch_ids)
    for batch_id in batch_ids:
        closure.update(downstream_batches(ledger, batch_id))
    for batch_id in closure:
        for ship in ledger.shipped_by_batch.get(batch_id, []):
            stores.setdefault(ship["store_id"], []).append(
                {"batch_id": batch_id, "quantity": ship["quantity"], "shipment_event": ship["event_id"]}
            )
    return {store: sorted(rows, key=lambda r: r["batch_id"]) for store, rows in sorted(stores.items())}


def _observations_on(ledger: Ledger, batch_id: str) -> list[ObservationRecord]:
    return [o for o in ledger.observations if o.aggregate_id == batch_id]


def _stage_evidence(
    ledger: Ledger,
    stage: str,
    batch_id: str,
    step_id: str | None,
    relevant_metrics: list[str],
) -> dict[str, Any]:
    observations = _observations_on(ledger, batch_id)
    metric_rows: list[dict[str, Any]] = []
    covered: set[str] = set()
    conforming: set[str] = set()
    for observation in observations:
        if observation.metric not in relevant_metrics:
            continue
        covered.add(observation.metric)
        conforms = _metric_conforms(ledger, observation)
        if conforms is True:
            conforming.add(observation.metric)
        metric_rows.append(
            {
                "metric": observation.metric,
                "result": observation.result,
                "method_version": observation.method_version,
                "observed_at": observation.occurred_at.isoformat(),
                "conforms_to_rules_of_day": conforms,
                "event_id": observation.event_id,
            }
        )
    exemptions = [
        {
            "event_id": e["event_id"],
            "responsible": e["responsible"],
            "granted_by": e["granted_by"],
            "reason": e["reason"],
        }
        for e in ledger.exemptions
        if e["batch_id"] == batch_id and (step_id is None or e["step_id"] == step_id)
    ]
    gaps = [m for m in relevant_metrics if m not in covered]
    if not relevant_metrics:
        status = "not_relevant"
    elif gaps:
        status = "gap"
    elif conforming == covered:
        status = "excluded"
    else:
        status = "suspect"
    retained_sample = None
    if step_id is not None and step_id in ledger.step_sample_remaining:
        retained_sample = {
            "quantity": round(ledger.step_sample_remaining[step_id], 6),
            "unit": ledger.step_unit[step_id],
        }
    return {
        "stage": stage,
        "batch_id": batch_id,
        "step_id": step_id,
        "status": status,
        "measurements": sorted(metric_rows, key=lambda r: r["observed_at"]),
        "missing_metrics": gaps,
        "retained_sample": retained_sample,
        "exemptions": exemptions,
    }


def path_evidence(ledger: Ledger, batch_id: str, symptoms: list[str]) -> dict[str, Any]:
    """沿一条生产路径逐段汇总证据、排除项与缺口。"""
    relevant_stages = set()
    for symptom in symptoms:
        relevant_stages.update(SYMPTOM_STAGE_RELEVANCE.get(symptom, []))
    if not relevant_stages:
        relevant_stages = set(STAGE_EVIDENCE_PLAN)

    stages: list[dict[str, Any]] = []

    # 原料段：供应商证明、分级与原料观察。
    chain = production_path(ledger, batch_id)
    closure = upstream_closure(ledger, batch_id)
    raw_lots = sorted(ledger.batches[batch_id].origins)
    for lot_id in raw_lots:
        meta = ledger.raw_meta.get(lot_id, {})
        section = _stage_evidence(
            ledger,
            "raw",
            lot_id,
            None,
            STAGE_EVIDENCE_PLAN["raw"] if "raw" in relevant_stages else [],
        )
        section["supplier"] = meta.get("supplier")
        section["grade"] = meta.get("grade")
        section["certificates"] = list(meta.get("certificates", []))
        if not section["certificates"]:
            section["status"] = "gap"
            section["missing_metrics"].append("supplier_certificate")
        stages.append(section)

    for record in chain:
        output_ids = [
            o["batch_id"]
            for o in record.outputs
            if o.get("role", PRODUCT) == PRODUCT and o["batch_id"] in closure
        ]
        for target_batch in output_ids:
            stage = record.stage or "unknown"
            section = _stage_evidence(
                ledger,
                stage,
                target_batch,
                record.step_id,
                STAGE_EVIDENCE_PLAN.get(stage, []) if stage in relevant_stages else [],
            )
            section["equipment_program"] = record.equipment_program
            section["params"] = dict(record.params)
            if stage == "baking" and not record.equipment_program:
                section["status"] = "gap"
                section["missing_metrics"].append("equipment_program")
            stages.append(section)

    releases = ledger.releases.get(batch_id, [])
    return {
        "candidate_batch": batch_id,
        "raw_attribution": raw_attribution(ledger, batch_id),
        "stages": stages,
        "excluded_causes": [s["stage"] for s in stages if s["status"] == "excluded"],
        "suspect_causes": [s["stage"] for s in stages if s["status"] == "suspect"],
        "gaps": [
            {"stage": s["stage"], "batch_id": s["batch_id"], "missing": s["missing_metrics"]}
            for s in stages
            if s["status"] == "gap"
        ],
        "release_history": [
            {
                "event_id": r["event_id"],
                "at": r["at"].isoformat(),
                "threshold_versions": r["threshold_versions"],
                "decided_by": r["decided_by"],
                "conclusion": r["conclusion"],
            }
            for r in releases
        ],
    }


def candidate_batches(ledger: Ledger, complaint: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """返回（候选批次, 收窄依据）。样品优先，否则按门店与购买时间匹配发运。"""
    reasons: list[str] = []
    if complaint.get("sample_batch_id"):
        reasons.append(f"sample:{complaint.get('sample_received')}")
        return [complaint["sample_batch_id"]], reasons

    purchased_at = complaint["purchased_at"]
    candidates: set[str] = set()
    for shipment in ledger.shipments:
        if shipment.store_id != complaint["store_id"]:
            continue
        if shipment.dispatched_at > purchased_at:
            continue
        if shipment.sell_by < purchased_at:
            continue
        for item in shipment.allocations:
            candidates.add(item["batch_id"])
            reasons.append(
                f"shipment {shipment.event_id}:{item['batch_id']} @ {shipment.store_id}"
            )
    return sorted(candidates), reasons


def triage(ledger: Ledger, complaint_id: str) -> dict[str, Any]:
    complaint = ledger.complaints.get(complaint_id)
    if complaint is None:
        return {"complaint_id": complaint_id, "found": False}
    candidates, reasons = candidate_batches(ledger, complaint)
    paths = [path_evidence(ledger, batch_id, complaint["symptoms"]) for batch_id in candidates]
    stores = affected_stores(ledger, candidates)
    return {
        "complaint_id": complaint_id,
        "found": True,
        "store_id": complaint["store_id"],
        "purchased_at": complaint["purchased_at"].isoformat(),
        "symptoms": complaint["symptoms"],
        "narrowing": reasons,
        "candidate_batches": candidates,
        "paths": paths,
        "affected_stores": stores,
        "stock_on_hand": {
            batch_id: {
                "available": round(ledger.batches[batch_id].available, 6),
                "unit": ledger.batches[batch_id].unit,
                "blocked": ledger.batches[batch_id].blocked,
            }
            for batch_id in candidates
            if batch_id in ledger.batches
        },
    }


def check_action_scope(ledger: Ledger, scope: Mapping[str, Any], case_complaint_id: str | None) -> list[dict[str, str]]:
    """处置/冻结范围核对：批次必须在投诉溯源闭包内，门店必须实际收到过货。"""
    issues: list[dict[str, str]] = []
    complaint_id = scope.get("complaint_id") or case_complaint_id
    if complaint_id is None or complaint_id not in ledger.complaints:
        issues.append({"code": "scope_without_complaint", "message": "处置范围必须关联到已登记投诉"})
        return issues

    result = triage(ledger, complaint_id)
    reachable_batches = set(result["candidate_batches"])
    for batch_id in result["candidate_batches"]:
        reachable_batches.update(upstream_closure(ledger, batch_id))
    reachable_stores = set(result["affected_stores"])

    for batch_id in scope.get("batches", []):
        if batch_id not in reachable_batches:
            issues.append(
                {
                    "code": "scope_batch_unreachable",
                    "message": f"批次 {batch_id} 不在投诉 {complaint_id} 的溯源闭包内，不得纳入处置",
                }
            )
    for store_id in scope.get("stores", []):
        if store_id not in reachable_stores:
            issues.append(
                {
                    "code": "scope_store_unreachable",
                    "message": f"门店 {store_id} 未收到候选批次的成品，不得纳入处置",
                }
            )
    return issues


def check_holds_before_disposition(ledger: Ledger) -> list[dict[str, str]]:
    """处置决定生效前，范围中未被在先冻结覆盖的批次/门店。"""
    issues: list[dict[str, str]] = []
    for case in ledger.dispositions:
        scope = case["scope"]
        covering_holds = [
            hold
            for hold in ledger.holds
            if hold["at"] <= case["at"]
            and (hold.get("case_id") in (None, case["case_id"]))
        ]
        held_batches = {b for hold in covering_holds for b in hold["scope"].get("batches", [])}
        held_stores = {s for hold in covering_holds for s in hold["scope"].get("stores", [])}
        for batch_id in scope.get("batches", []):
            if batch_id not in held_batches:
                issues.append(
                    {
                        "code": "disposition_without_hold",
                        "message": f"处置 {case['event_id']} 中批次 {batch_id} 在决定前未被冻结",
                    }
                )
        for store_id in scope.get("stores", []):
            if store_id not in held_stores:
                issues.append(
                    {
                        "code": "disposition_without_hold",
                        "message": f"处置 {case['event_id']} 中门店 {store_id} 在决定前未被冻结",
                    }
                )
    return issues
