"""命令行入口：契约校验、账本检查、谱系反查与投诉研判。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_event
from .ledger import replay
from .lineage import (
    affected_stores,
    check_action_scope,
    check_holds_before_disposition,
    raw_attribution,
    triage,
)


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cmd_validate(args: argparse.Namespace) -> int:
    schema = _load(args.schema)
    event = _load(args.event)
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _cmd_check(args: argparse.Namespace) -> int:
    schema = _load(args.schema)
    events = _load(args.ledger)
    ledger = replay(events, schema)

    scope_issues: list[dict[str, str]] = []
    case_complaints = {case["case_id"]: case.get("complaint_id") for case in ledger.dispositions}
    for hold in ledger.holds:
        complaint_id = hold.get("case_id") and case_complaints.get(hold["case_id"])
        if complaint_id:
            scope_issues.extend(check_action_scope(ledger, {"complaint_id": complaint_id, **hold["scope"]}, hold.get("case_id")))
    for case in ledger.dispositions:
        payload = {"complaint_id": case.get("complaint_id"), **case["scope"]}
        scope_issues.extend(check_action_scope(ledger, payload, case["case_id"]))
    scope_issues.extend(check_holds_before_disposition(ledger))

    if not ledger.violations and not scope_issues:
        print(f"valid ledger: {len(events)} 个事件，{len(ledger.batches)} 个批次，无阻断")
        return 0

    for violation in ledger.violations:
        print(f"{violation.event_id}	{violation.code}	{violation.message}")
    for issue in scope_issues:
        print(f"-	{issue['code']}	{issue['message']}")
    return 1


def _cmd_trace(args: argparse.Namespace) -> int:
    schema = _load(args.schema)
    ledger = replay(_load(args.ledger), schema)
    if args.batch not in ledger.batches:
        print(f"未知批次：{args.batch}", file=sys.stderr)
        return 2
    output = {
        "batch_id": args.batch,
        "raw_attribution": raw_attribution(ledger, args.batch),
        "affected_stores": affected_stores(ledger, [args.batch]),
        "available": round(ledger.batches[args.batch].available, 6),
        "unit": ledger.batches[args.batch].unit,
        "blocked": ledger.batches[args.batch].blocked,
        "releases": [
            {
                "event_id": r["event_id"],
                "at": r["at"].isoformat(),
                "threshold_versions": r["threshold_versions"],
                "decided_by": r["decided_by"],
            }
            for r in ledger.releases.get(args.batch, [])
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not ledger.violations else 1


def _cmd_triage(args: argparse.Namespace) -> int:
    schema = _load(args.schema)
    ledger = replay(_load(args.ledger), schema)
    result: Mapping[str, Any] = triage(ledger, args.complaint)
    if not result.get("found"):
        print(f"未知投诉：{args.complaint}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not ledger.violations else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yolk_deviation", description="咸蛋黄风味偏差追因簿")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验单个事件契约")
    p_validate.add_argument("schema")
    p_validate.add_argument("event")
    p_validate.set_defaults(func=_cmd_validate)

    p_check = sub.add_parser("check", help="重放账本并检查守恒、回避、钉版与处置范围")
    p_check.add_argument("schema")
    p_check.add_argument("ledger")
    p_check.set_defaults(func=_cmd_check)

    p_trace = sub.add_parser("trace", help="从成品反查原料分摊与去向门店")
    p_trace.add_argument("schema")
    p_trace.add_argument("ledger")
    p_trace.add_argument("batch")
    p_trace.set_defaults(func=_cmd_trace)

    p_triage = sub.add_parser("triage", help="投诉候选收窄、证据缺口与影响范围研判")
    p_triage.add_argument("schema")
    p_triage.add_argument("ledger")
    p_triage.add_argument("complaint")
    p_triage.set_defaults(func=_cmd_triage)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
