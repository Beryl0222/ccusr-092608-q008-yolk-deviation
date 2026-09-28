import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yolk_deviation.investigation import investigate, narrow_candidates
from yolk_deviation.ledger import load_journal
from yolk_deviation.lineage import downstream_batches, stock_impact, trace_back
from yolk_deviation.rules import load_rule_book

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
SAMPLE_EVENTS = json.loads((ROOT / "data/sample.json").read_text(encoding="utf-8"))
RULES = json.loads((ROOT / "data/rules.json").read_text(encoding="utf-8"))

TS = 0


def tick():
    global TS
    TS += 1
    return f"2026-09-{10 + TS:02d}T08:00:00+08:00"


def reset():
    global TS
    TS = 0


def event(event_id, etype, agg_type, agg_id, payload, version=1, at=None):
    return {"event_id": event_id, "event_type": etype,
            "aggregate_type": agg_type, "aggregate_id": agg_id,
            "occurred_at": at or tick(), "version": version, "payload": payload}


def lot(event_id, lot_id, quantity=100.0):
    return event(event_id, "LOT_ACCEPTED", "raw_yolk_lot", lot_id, {
        "supplier_id": "S-1", "supplier_certificate_refs": ["CERT-1"],
        "grade": "A", "quantity": quantity, "unit": "kg"})


def transform(event_id, wip_id, inputs, outputs, step="baking", loss=0.0, version=1):
    return event(event_id, "BATCH_TRANSFORMED", "process_batch", wip_id, {
        "step": step, "input_allocations": inputs,
        "outputs": outputs, "loss_quantity": loss}, version=version)


def alloc(source, quantity, fraction):
    return {"source_batch_id": source, "quantity": quantity, "unit": "kg", "fraction": fraction}


def out(batch_id, quantity):
    return {"batch_id": batch_id, "quantity": quantity, "unit": "kg"}


class SampleJournalTests(unittest.TestCase):
    def test_sample_journal_is_balanced_and_releasable(self):
        journal = load_journal(copy.deepcopy(SAMPLE_EVENTS), SCHEMA, load_rule_book(RULES))
        blocking = [issue for issue in journal.issues if issue.blocking]
        self.assertEqual([], blocking, [i.message for i in blocking])


class ConservationTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_over_consumption_blocks_downstream_release(self):
        events = [
            lot("e-lot", "LOT-1"),
            transform("e-t1", "WIP-1", [alloc("LOT-1", 120.0, 1.0)], [out("WIP-1", 120.0)]),
            event("e-rel", "RELEASE_DECIDED", "process_batch", "WIP-1", {
                "batch_id": "WIP-1", "decision": "RELEASED", "rule_version": "r1",
                "decided_by": "u-q", "decided_at": tick()}),
        ]
        journal = load_journal(events, SCHEMA)
        codes = {issue.code for issue in journal.issues}
        self.assertIn("quantity_not_conserved", codes)
        self.assertTrue(journal.is_tainted("WIP-1"))
        self.assertIn("release_blocked_by_imbalance", codes)

    def test_input_output_gap_blocks(self):
        events = [
            lot("e-lot", "LOT-1"),
            transform("e-t1", "WIP-1", [alloc("LOT-1", 50.0, 1.0)],
                      [out("WIP-1", 40.0)], loss=0.0),
        ]
        journal = load_journal(events, SCHEMA)
        self.assertTrue(journal.is_tainted("WIP-1"))
        self.assertIn("quantity_not_conserved", {i.code for i in journal.issues})

    def test_sample_and_scrap_consume_quantity(self):
        events = [
            lot("e-lot", "LOT-1", quantity=10.0),
            transform("e-t1", "WIP-1", [alloc("LOT-1", 10.0, 1.0)],
                      [out("WIP-1", 9.5)], loss=0.5),
            event("e-smp", "SAMPLE_DRAWN", "retained_sample", "SMP-1", {
                "source_batch_id": "WIP-1", "stage": "baking", "sample_id": "SMP-1",
                "quantity_consumed": 0.3, "unit": "kg", "retained": True, "drawn_by": "u-qc"}),
            event("e-scrap", "MATERIAL_SCRAPPED", "process_batch", "WIP-1", {
                "source_batch_id": "WIP-1", "quantity": 9.2, "unit": "kg",
                "reason_code": "TAIL"}, version=2),
        ]
        journal = load_journal(events, SCHEMA)
        self.assertEqual([], [i for i in journal.issues if i.code == "quantity_not_conserved"])

    def test_upstream_taint_propagates(self):
        events = [
            lot("e-lot", "LOT-1"),
            transform("e-t1", "WIP-1", [alloc("LOT-1", 120.0, 1.0)], [out("WIP-1", 120.0)]),
            transform("e-t2", "WIP-2", [alloc("WIP-1", 120.0, 1.0)], [out("WIP-2", 120.0)]),
        ]
        journal = load_journal(events, SCHEMA)
        self.assertTrue(journal.is_tainted("WIP-2"))


class LineageTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_split_and_merge_shares(self):
        events = [
            lot("e-a", "LOT-A", 100.0),
            lot("e-b", "LOT-B", 100.0),
            transform("e-1", "WIP-A", [alloc("LOT-A", 40.0, 1.0)], [out("WIP-A", 40.0)]),
            transform("e-2", "WIP-B", [alloc("LOT-B", 30.0, 1.0)], [out("WIP-B", 30.0)]),
            transform("e-3", "WIP-M",
                      [alloc("WIP-A", 10.0, 0.25), alloc("WIP-B", 30.0, 0.75)],
                      [out("WIP-M", 40.0)]),
        ]
        journal = load_journal(events, SCHEMA)
        genealogy = trace_back(journal, "WIP-M", finished_quantity=40.0)
        shares = {s.lot_id: s.share for s in genealogy.raw_shares}
        self.assertAlmostEqual(shares["LOT-A"], 0.25)
        self.assertAlmostEqual(shares["LOT-B"], 0.75)
        self.assertTrue(any("WIP-A" in [s.batch_id for s in path] for path in genealogy.paths))

    def test_downstream_and_stock_impact(self):
        journal = load_journal(copy.deepcopy(SAMPLE_EVENTS), SCHEMA, load_rule_book(RULES))
        affected = downstream_batches(journal, "LOT-B")
        self.assertIn("WIP-BAKE-B1", affected)
        self.assertNotIn("WIP-BAKE-G1", affected)
        impact = stock_impact(journal, {"WIP-BAKE-B1"})
        self.assertEqual(["SU-B1"], [row["stock_unit_id"] for row in impact])
        self.assertEqual("ST-07", impact[0]["current_location"])
        self.assertEqual("CASE-01", impact[0]["frozen_by_case"])


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        reset()

    def _journal_with_observation(self, value, exemption=None, decision_time=None):
        events = [
            lot("e-lot", "LOT-1"),
            transform("e-t1", "WIP-1", [alloc("LOT-1", 10.0, 1.0)],
                      [out("WIP-1", 10.0)], step="thawing"),
            event("e-obs", "OBSERVATION_RECORDED", "quality_observation", "OBS-1", {
                "subject_type": "process_batch", "subject_id": "WIP-1",
                "check_stage": "thawing", "method_version": "v1",
                "rule_version": "ruleset-2026-08",
                "observed_at": "2026-09-12T09:00:00+08:00",
                "result": {"metric": "wait_minutes", "value": value, "unit": "分钟"},
                "observed_by": "u-thaw-leader"}),
        ]
        if exemption:
            events.append(event("e-exempt", "DEVIATION_EXEMPTION_GRANTED",
                                "process_batch", "WIP-1", exemption, version=2,
                                at="2026-09-12T10:00:00+08:00"))
        events.append(event("e-rel", "RELEASE_DECIDED", "process_batch", "WIP-1", {
            "batch_id": "WIP-1", "decision": "RELEASED", "rule_version": "ruleset-2026-08",
            "decided_by": "u-quality-lead-林",
            "decided_at": decision_time or "2026-09-12T11:00:00+08:00"},
            version=3 if exemption else 2,
            at="2026-09-12T11:00:00+08:00"))
        return load_journal(events, SCHEMA, load_rule_book(RULES))

    def test_out_observation_blocks_release(self):
        journal = self._journal_with_observation(150)
        self.assertIn("release_blocked_by_unresolved_deviation",
                      {i.code for i in journal.issues})

    def test_independent_exemption_allows_release(self):
        journal = self._journal_with_observation(150, exemption={
            "batch_id": "WIP-1", "step": "thawing", "observation_id": "OBS-1",
            "decision": "APPROVED", "granted_by": "u-quality-lead-林",
            "step_owner": "u-thaw-leader", "granted_at": "2026-09-12T10:00:00+08:00"})
        self.assertNotIn("release_blocked_by_unresolved_deviation",
                         {i.code for i in journal.issues})

    def test_self_exemption_contract_rejected(self):
        journal = self._journal_with_observation(150, exemption={
            "batch_id": "WIP-1", "step": "thawing", "observation_id": "OBS-1",
            "decision": "APPROVED", "granted_by": "u-thaw-leader",
            "step_owner": "u-thaw-leader", "granted_at": "2026-09-12T10:00:00+08:00"})
        codes = {i.code for i in journal.issues}
        self.assertIn("self_approval_forbidden", codes)
        self.assertIn("release_blocked_by_unresolved_deviation", codes)

    def test_in_limit_passes(self):
        journal = self._journal_with_observation(90)
        self.assertNotIn("release_blocked_by_unresolved_deviation",
                         {i.code for i in journal.issues})


class RuleTimingTests(unittest.TestCase):
    def test_historical_release_uses_old_threshold(self):
        # 7.7 在 v1(≤8.0) 合格、在 v2(≤7.5) 越限；放行发生在 9/13，历史结论不翻案。
        journal = load_journal(copy.deepcopy(SAMPLE_EVENTS), SCHEMA, load_rule_book(RULES))
        blocking = [i for i in journal.issues
                    if i.code == "release_blocked_by_unresolved_deviation"
                    and i.ref in ("OBS-CB-SALT",)]
        self.assertEqual([], blocking)

    def test_new_threshold_flags_in_flight_batch(self):
        from yolk_deviation.rules import reevaluate_with_current
        journal = load_journal(copy.deepcopy(SAMPLE_EVENTS), SCHEMA, load_rule_book(RULES))
        book = load_rule_book(RULES)
        obs = next(e for e in journal.observations if e["aggregate_id"] == "OBS-CB-SALT")
        verdict, rule = reevaluate_with_current(book, obs)
        self.assertEqual("OUT", verdict)
        self.assertEqual("salt-v2", rule.rule_version)


class AppendOnlyTests(unittest.TestCase):
    def test_observation_cannot_be_rewritten(self):
        events = [
            lot("e-lot", "LOT-1"),
            event("e-obs-1", "OBSERVATION_RECORDED", "quality_observation", "OBS-1", {
                "subject_type": "raw_yolk_lot", "subject_id": "LOT-1",
                "check_stage": "incoming", "method_version": "v1", "rule_version": "r1",
                "observed_at": "2026-09-10T10:00:00+08:00",
                "result": {"metric": "grade_score", "value": 90}, "observed_by": "u"},
                at="2026-09-10T10:00:00+08:00"),
            event("e-obs-2", "OBSERVATION_RECORDED", "quality_observation", "OBS-1", {
                "subject_type": "raw_yolk_lot", "subject_id": "LOT-1",
                "check_stage": "incoming", "method_version": "v1", "rule_version": "r1",
                "observed_at": "2026-09-11T10:00:00+08:00",
                "result": {"metric": "grade_score", "value": 92}, "observed_by": "u"},
                version=2, at="2026-09-11T10:00:00+08:00"),
        ]
        journal = load_journal(events, SCHEMA)
        self.assertIn("observation_must_be_append_only", {i.code for i in journal.issues})


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.journal = load_journal(copy.deepcopy(SAMPLE_EVENTS), SCHEMA, load_rule_book(RULES))
        self.book = load_rule_book(RULES)
        self.complaint = self.journal.complaints["CMP-20260921-01"]

    def test_narrows_by_store_and_time(self):
        candidates = narrow_candidates(self.journal, self.complaint)
        batches = {c.batch_id for c in candidates}
        self.assertEqual({"WIP-BAKE-B1", "WIP-BAKE-G1"}, batches)

    def test_other_store_excluded(self):
        other_store = copy.deepcopy(self.complaint)
        other_store["payload"]["store_id"] = "ST-99"
        self.assertEqual([], narrow_candidates(self.journal, other_store))

    def test_findings_and_freeze_scope(self):
        report = investigate(self.journal, self.complaint, self.book)
        by_batch = {c["batch_id"]: c for c in report["candidates"]}
        b1 = by_batch["WIP-BAKE-B1"]
        statuses = {c["code"]: c["status"] for c in b1["causes"]}
        self.assertEqual("SUSPECTED", statuses["THAW_WAIT"])
        self.assertEqual("SUSPECTED", statuses["STORE_STORAGE"])
        self.assertEqual("EXCLUDED", statuses["RAW_GRADE"])
        self.assertEqual("EXCLUDED", statuses["CURING_SALT"])
        # B1 烘烤留样缺失
        self.assertIn("missing_retained_sample", {g["type"] for g in b1["gaps"]})
        # 好批次所有原因排除，整体排除清单不含解冻/门店
        self.assertNotIn("THAW_WAIT", report["excluded_causes_overall"])
        self.assertIn("RAW_GRADE", report["excluded_causes_overall"])
        # 只有越限候选进入冻结建议
        self.assertEqual(["SU-B1"], report["suggested_freeze_scope"]["stock_unit_ids"])

    def test_purchase_before_stock_arrival_excludes(self):
        early = copy.deepcopy(self.complaint)
        early["payload"]["purchased_at"] = "2026-09-10T08:00:00+08:00"
        self.assertEqual([], narrow_candidates(self.journal, early))


class HoldAuthorityTests(unittest.TestCase):
    def test_hold_requires_quality_lead(self):
        events = copy.deepcopy(SAMPLE_EVENTS)
        for e in events:
            if e["event_id"] == "evt-hold-case-01":
                e["payload"]["held_by_role"] = "line_leader"
        journal = load_journal(events, SCHEMA, load_rule_book(RULES))
        self.assertIn("freeze_requires_quality_lead", {i.code for i in journal.issues})

    def test_movement_while_frozen_blocked(self):
        events = copy.deepcopy(SAMPLE_EVENTS)
        events.append({
            "event_id": "evt-move-b1-blocked",
            "event_type": "STOCK_MOVED",
            "aggregate_type": "stock_unit",
            "aggregate_id": "SU-B1",
            "occurred_at": "2026-09-21T16:00:00+08:00",
            "version": 1,
            "payload": {"stock_unit_id": "SU-B1", "from_location": "ST-07",
                        "to_location": "ST-08", "quantity": 23.5, "unit": "kg",
                        "moved_at": "2026-09-21T16:00:00+08:00"},
        })
        journal = load_journal(events, SCHEMA, load_rule_book(RULES))
        self.assertIn("stock_movement_blocked", {i.code for i in journal.issues})

    def test_disposition_without_hold_rejected(self):
        events = copy.deepcopy(SAMPLE_EVENTS)
        events = [e for e in events if e["event_id"] != "evt-hold-case-01"]
        journal = load_journal(events, SCHEMA, load_rule_book(RULES))
        self.assertIn("disposition_without_hold", {i.code for i in journal.issues})


if __name__ == "__main__":
    unittest.main()
