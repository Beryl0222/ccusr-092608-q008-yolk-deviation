"""命令行入口：单事件校验、台账核验、逆向溯源与投诉追因。

用法:
  python -m yolk_deviation.cli <schema.json> <event.json>           # 单事件契约校验（兼容旧用法）
  python -m yolk_deviation.cli check <schema.json> <journal.json> [--rules rules.json] [--json]
  python -m yolk_deviation.cli trace <schema.json> <journal.json> <批次或库存单元> [--rules ...] [--qty N]
  python -m yolk_deviation.cli impact <schema.json> <journal.json> <原料或在制批次>
  python -m yolk_deviation.cli investigate <schema.json> <journal.json> <投诉编号> --rules rules.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .investigation import investigate
from .ledger import load_journal
from .lineage import downstream_batches, stock_impact, trace_back, trace_stock
from .rules import load_rule_book
from .contracts import validate_event


def _load_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load(args: argparse.Namespace):
    schema = _load_json(args.schema)
    events = _load_json(args.journal)
    book = load_rule_book(_load_json(args.rules)) if args.rules else None
    return schema, events, book


def _cmd_check(args: argparse.Namespace) -> int:
    schema, events, book = _load(args)
    journal = load_journal(events, schema, book)
    if args.json:
        payload = {"valid": not journal.issues,
                   "issues": [issue.as_dict() for issue in journal.issues]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not journal.issues else 1
    if not journal.issues:
        print("valid")
        return 0
    for issue in journal.issues:
        head = f"[{issue.event_id or '-'}]" if issue.event_id else ""
        ref = f" {issue.ref}" if issue.ref else ""
        print(f"{issue.code}\t{head}{ref}\t{issue.message}")
    return 1


def _cmd_trace(args: argparse.Namespace) -> int:
    schema, events, book = _load(args)
    journal = load_journal(events, schema, book)
    if args.node in journal.stock_units:
        genealogy = trace_stock(journal, args.node)
        start_kind = "stock_unit"
    else:
        genealogy = trace_back(journal, args.node, args.qty or 1.0)
        start_kind = journal.node_kind(args.node) or "unknown"
    if genealogy is None:
        print(f"未找到节点 {args.node}", file=sys.stderr)
        return 2
    path_node_ids = {step.batch_id for path in genealogy.paths for step in path} | {genealogy.start_node}
    decisions: list[dict] = []
    for node_id in sorted(path_node_ids):
        for release in journal.releases.get(node_id, []):
            body = release["payload"]
            decisions.append({
                "batch_id": node_id, "kind": "release", "event_id": release["event_id"],
                "decision": body["decision"], "rule_version": body.get("rule_version"),
                "decided_by": body.get("decided_by"), "decided_at": body.get("decided_at"),
            })
        for exemption in journal.exemptions:
            if exemption["payload"].get("batch_id") != node_id:
                continue
            body = exemption["payload"]
            decisions.append({
                "batch_id": node_id, "kind": "exemption", "event_id": exemption["event_id"],
                "decision": body.get("decision"), "step": body.get("step"),
                "observation_id": body.get("observation_id"),
                "granted_by": body.get("granted_by"), "step_owner": body.get("step_owner"),
                "granted_at": body.get("granted_at"),
            })
    payload = {
        "start_node": genealogy.start_node,
        "start_kind": start_kind,
        "tainted": genealogy.tainted,
        "raw_shares": [
            {"lot_id": share.lot_id, "supplier_id": share.supplier_id,
             "share": round(share.share, 6),
             "raw_equivalent_quantity": round(share.raw_equivalent_quantity, 3)}
            for share in genealogy.raw_shares
        ],
        "paths": [
            [{"batch_id": step.batch_id, "step": step.step, "event_id": step.producer_event_id}
             for step in path]
            for path in genealogy.paths
        ],
        "quality_decisions": sorted(decisions, key=lambda item: (item["batch_id"], item["kind"])),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not genealogy.tainted else 3


def _cmd_impact(args: argparse.Namespace) -> int:
    schema, events, book = _load(args)
    journal = load_journal(events, schema, book)
    if journal.node_kind(args.node) is None:
        print(f"未找到节点 {args.node}", file=sys.stderr)
        return 2
    batches = downstream_batches(journal, args.node) | ({args.node} if args.node in journal.batch_producer else set())
    print(json.dumps({"source_node": args.node,
                      "downstream_batches": sorted(batches),
                      "stock": stock_impact(journal, batches)}, ensure_ascii=False, indent=2))
    return 0


def _cmd_investigate(args: argparse.Namespace) -> int:
    if not args.rules:
        print("investigate 需要 --rules 阈值册", file=sys.stderr)
        return 2
    schema, events, book = _load(args)
    journal = load_journal(events, schema, book)
    complaint = journal.complaints.get(args.complaint)
    if complaint is None:
        print(f"未找到投诉 {args.complaint}", file=sys.stderr)
        return 2
    print(json.dumps(investigate(journal, complaint, book), ensure_ascii=False, indent=2))
    return 0


def _cmd_single_event(schema_path: str, event_path: str) -> int:
    """旧用法：校验单个事件信封。"""
    schema = _load_json(schema_path)
    event = _load_json(event_path)
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and not argv[0].startswith("-"):
        return _cmd_single_event(argv[0], argv[1])

    parser = argparse.ArgumentParser(prog="yolk_deviation.cli", description="咸蛋黄风味偏差追因簿")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="核验整本台账（结构、版本链、数量守恒、放行闸门）")
    p_check.add_argument("schema")
    p_check.add_argument("journal")
    p_check.add_argument("--rules")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=_cmd_check)

    p_trace = sub.add_parser("trace", help="从成品批次/库存单元逆向反查原料分摊")
    p_trace.add_argument("schema")
    p_trace.add_argument("journal")
    p_trace.add_argument("node")
    p_trace.add_argument("--rules")
    p_trace.add_argument("--qty", type=float, default=None, help="成品数量（批次节点时默认 1）")
    p_trace.set_defaults(func=_cmd_trace)

    p_impact = sub.add_parser("impact", help="从原料/在制批次正向列出受影响库存")
    p_impact.add_argument("schema")
    p_impact.add_argument("journal")
    p_impact.add_argument("node")
    p_impact.add_argument("--rules")
    p_impact.set_defaults(func=_cmd_impact)

    p_inv = sub.add_parser("investigate", help="投诉追因：候选收窄、证据/缺口、冻结范围建议")
    p_inv.add_argument("schema")
    p_inv.add_argument("journal")
    p_inv.add_argument("complaint")
    p_inv.add_argument("--rules", required=True)
    p_inv.set_defaults(func=_cmd_investigate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
