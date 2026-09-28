"""领域事件交换契约校验（只做结构校验，不改写调用方输入）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

_DECISIONS = {"RELEASED", "REJECTED", "CONDITIONAL"}
_EXEMPTION_DECISIONS = {"APPROVED", "DENIED"}
_DISPOSITION_DECISIONS = {"RECALL", "HOLD_CONTINUED", "RELEASE_WITH_NOTICE", "DESTROY"}


@dataclass(frozen=True)
class ContractIssue:
    field: str
    code: str
    message: str


def _timezone_is_explicit(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _issue(field: str, code: str, message: str) -> ContractIssue:
    return ContractIssue(field, code, message)


def _check_quantity(prefix: str, obj: Mapping[str, Any], issues: list[ContractIssue],
                    qty_field: str = "quantity") -> None:
    if qty_field in obj and (not _is_number(obj[qty_field]) or obj[qty_field] <= 0):
        issues.append(_issue(f"{prefix}.{qty_field}", "positive_number", "数量必须是正数"))
    if "unit" in obj and (not isinstance(obj.get("unit"), str) or not obj["unit"].strip()):
        issues.append(_issue(f"{prefix}.unit", "non_empty_string", "计量单位必须是非空字符串"))


def _check_allocations(body: Mapping[str, Any], issues: list[ContractIssue]) -> None:
    allocations = body.get("input_allocations")
    if not isinstance(allocations, list) or not allocations:
        issues.append(_issue("payload.input_allocations", "non_empty_array", "拆批/混批必须给出至少一项来源投入"))
        return
    total_fraction: float | None = 0.0
    fractions_present = True
    for index, item in enumerate(allocations):
        prefix = f"payload.input_allocations[{index}]"
        if not isinstance(item, Mapping):
            issues.append(_issue(prefix, "object_required", "来源投入必须是对象"))
            continue
        if not isinstance(item.get("source_batch_id"), str) or not item["source_batch_id"].strip():
            issues.append(_issue(f"{prefix}.source_batch_id", "non_empty_string", "来源批次标识必填"))
        _check_quantity(prefix, item, issues)
        if "fraction" in item:
            if not _is_number(item["fraction"]) or not 0 < item["fraction"] <= 1:
                issues.append(_issue(f"{prefix}.fraction", "ratio_out_of_range", "来源占比必须在 (0,1] 区间"))
            else:
                total_fraction += item["fraction"]
        else:
            fractions_present = False
    if fractions_present and allocations and isinstance(total_fraction, float):
        if abs(total_fraction - 1.0) > 1e-6:
            issues.append(_issue("payload.input_allocations", "fractions_must_sum_to_one",
                                 "来源占比之和必须等于 1"))


def _check_outputs(body: Mapping[str, Any], issues: list[ContractIssue]) -> None:
    outputs = body.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        issues.append(_issue("payload.outputs", "non_empty_array", "转换必须登记至少一项产出"))
        return
    for index, item in enumerate(outputs):
        prefix = f"payload.outputs[{index}]"
        if not isinstance(item, Mapping):
            issues.append(_issue(prefix, "object_required", "产出必须是对象"))
            continue
        if not isinstance(item.get("batch_id"), str) or not item["batch_id"].strip():
            issues.append(_issue(f"{prefix}.batch_id", "non_empty_string", "产出批次标识必填"))
        _check_quantity(prefix, item, issues)


def validate_event(payload: Any, schema: Mapping[str, Any]) -> list[ContractIssue]:
    """返回稳定排序的问题列表；守恒、幂等等跨事件规则由台账层负责。"""
    if not isinstance(payload, Mapping):
        return [_issue("$", "object_required", "事件必须是 JSON 对象")]
    issues: list[ContractIssue] = []
    for field in schema.get("required", []):
        if field not in payload:
            issues.append(_issue(str(field), "required", "缺少必填字段"))
    for field in ("event_id", "event_type", "aggregate_type", "aggregate_id"):
        if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
            issues.append(_issue(field, "non_empty_string", "字段必须是非空字符串"))
    version = payload.get("version")
    if "version" in payload and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        issues.append(_issue("version", "positive_integer", "版本必须是正整数"))
    occurred_at = payload.get("occurred_at")
    if "occurred_at" in payload and (not isinstance(occurred_at, str) or not _timezone_is_explicit(occurred_at)):
        issues.append(_issue("occurred_at", "timezone_required", "发生时间必须包含时区"))
    properties = schema.get("properties", {})
    event_type = payload.get("event_type")
    aggregate_type = payload.get("aggregate_type")
    for field in ("event_type", "aggregate_type"):
        allowed = properties.get(field, {}).get("enum", [])
        value = payload.get(field)
        if isinstance(value, str) and allowed and value not in allowed:
            issues.append(_issue(field, "unsupported_value", "字段值未在契约中登记"))
    if (isinstance(event_type, str) and isinstance(aggregate_type, str)
            and event_type in schema.get("aggregate_types_by_event", {})
            and aggregate_type not in schema["aggregate_types_by_event"][event_type]):
        issues.append(_issue("aggregate_type", "aggregate_event_mismatch",
                             "聚合类型与事件类型不匹配"))
    body = payload.get("payload")
    if "payload" in payload and not isinstance(body, Mapping):
        issues.append(_issue("payload", "object_required", "事件载荷必须是 JSON 对象"))
    elif isinstance(event_type, str) and isinstance(body, Mapping):
        for field in schema.get("payload_required_by_event", {}).get(event_type, []):
            if field not in body:
                issues.append(_issue(f"payload.{field}", "required", "事件载荷缺少必填字段"))
        if event_type == "LOT_ACCEPTED":
            _check_quantity("payload", body, issues)
            if not isinstance(body.get("supplier_certificate_refs"), list) or not body["supplier_certificate_refs"]:
                issues.append(_issue("payload.supplier_certificate_refs", "non_empty_array",
                                     "供应商证明至少登记一份"))
        elif event_type == "BATCH_TRANSFORMED":
            _check_allocations(body, issues)
            _check_outputs(body, issues)
            if not isinstance(body.get("step"), str) or not body["step"].strip():
                issues.append(_issue("payload.step", "non_empty_string", "处理工序必填"))
        elif event_type in ("SAMPLE_DRAWN", "MATERIAL_SCRAPPED", "STOCK_MOVED"):
            _check_quantity("payload", body, issues,
                            qty_field="quantity_consumed" if event_type == "SAMPLE_DRAWN" else "quantity")
        elif event_type == "OBSERVATION_RECORDED":
            if not isinstance(body.get("result"), Mapping):
                issues.append(_issue("payload.result", "object_required", "测量结果必须是对象"))
            if body.get("subject_type") not in ("raw_yolk_lot", "process_batch", "stock_unit"):
                issues.append(_issue("payload.subject_type", "unsupported_value",
                                     "观察对象必须是原料批次、在制批次或库存单元"))
            for field in ("method_version", "rule_version"):
                if field in body and (not isinstance(body.get(field), str) or not body[field].strip()):
                    issues.append(_issue(f"payload.{field}", "non_empty_string", f"{field} 必须是非空字符串"))
            observed_at = body.get("observed_at")
            if "observed_at" in body and (not isinstance(observed_at, str) or not _timezone_is_explicit(observed_at)):
                issues.append(_issue("payload.observed_at", "timezone_required", "观察时间必须包含时区"))
        elif event_type == "RELEASE_DECIDED" and body.get("decision") not in _DECISIONS:
            issues.append(_issue("payload.decision", "unsupported_value",
                                 "放行决定必须是 RELEASED / REJECTED / CONDITIONAL"))
        elif event_type == "DEVIATION_EXEMPTION_GRANTED":
            granted_by, step_owner = body.get("granted_by"), body.get("step_owner")
            if isinstance(granted_by, str) and isinstance(step_owner, str) and granted_by == step_owner:
                issues.append(_issue("payload.granted_by", "self_approval_forbidden",
                                     "班组长不得批准自己负责工序的偏差豁免"))
        elif event_type == "DISPOSITION_APPROVED" and body.get("decision") not in _DISPOSITION_DECISIONS:
            issues.append(_issue("payload.decision", "unsupported_value", "处置决定未在契约中登记"))
    return sorted(issues, key=lambda issue: (issue.field, issue.code))
