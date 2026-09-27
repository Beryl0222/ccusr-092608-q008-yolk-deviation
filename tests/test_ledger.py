import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yolk_deviation.ledger import replay
from yolk_deviation.lineage import (
    affected_stores,
    candidate_batches,
    check_action_scope,
    check_holds_before_disposition,
    downstream_batches,
    raw_attribution,
    triage,
)


def load_sample():
    schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
    events = json.loads((ROOT / "data/ledger.json").read_text(encoding="utf-8"))
    return schema, events


def codes(ledger):
    return {(v.event_id, v.code) for v in ledger.violations}


def by_id(events):
    return {e["event_id"]: e for e in events}


def drop(events, event_id):
    return [e for e in events if e["event_id"] != event_id]


class SampleLedgerTests(unittest.TestCase):
    def setUp(self):
        self.schema, self.events = load_sample()
        self.ledger = replay(self.events, self.schema)

    def test_sample_ledger_is_clean(self):
        self.assertEqual([], self.ledger.violations, msg=[str(v) for v in self.ledger.violations])

    def test_mixed_batch_attribution(self):
        rows = {r["raw_lot"]: r for r in raw_attribution(self.ledger, "FG-0924-G")}
        self.assertAlmostEqual(rows["raw-A"]["share"], 0.7)
        self.assertAlmostEqual(rows["raw-C"]["share"], 0.3)
        self.assertAlmostEqual(rows["raw-A"]["allocated_quantity"], 136.5)

    def test_single_origin_attribution(self):
        rows = raw_attribution(self.ledger, "FG-0924-B")
        self.assertEqual([("raw-B", 1.0)], [(r["raw_lot"], r["share"]) for r in rows])

    def test_downstream_and_stores(self):
        self.assertIn("FG-0924-G", downstream_batches(self.ledger, "raw-A"))
        self.assertNotIn("FG-0924-B", downstream_batches(self.ledger, "raw-A"))
        stores = affected_stores(self.ledger, ["raw-B"])
        self.assertEqual(set(stores), {"S1-南京东路店", "S2-徐家汇店"})
        stores_a = affected_stores(self.ledger, ["raw-A"])
        self.assertEqual(set(stores_a), {"S3-陆家嘴店"})

    def test_historical_release_keeps_rules_of_its_day(self):
        # water_pct v2 在 9-25 生效；9-24 的 G/B 批放行钉版 v1 依旧有效，
        # 9-26 的 N 批必须使用 v2。
        ledger = self.ledger
        g_release = ledger.releases["FG-0924-G"][0]
        self.assertEqual(g_release["threshold_versions"]["water_pct"], 1)
        n_release = ledger.releases["FG-0926-N"][0]
        self.assertEqual(n_release["threshold_versions"]["water_pct"], 2)

    def test_triage_narrows_and_lists_suspects_gaps_exclusions(self):
        report = triage(self.ledger, "CMP-20260926-001")
        self.assertEqual(report["candidate_batches"], ["FG-0924-B"])
        self.assertTrue(any(n.startswith("sample:") for n in report["narrowing"]))
        path = report["paths"][0]
        self.assertIn("grading", path["suspect_causes"])
        self.assertIn("thawing", path["suspect_causes"])
        gap_stages = {g["stage"] for g in path["gaps"]}
        self.assertIn("raw", gap_stages)  # 缺供应商证明
        self.assertEqual(set(report["affected_stores"]), {"S1-南京东路店", "S2-徐家汇店"})

    def test_scope_and_hold_order_are_satisfied(self):
        scope = {
            "complaint_id": "CMP-20260926-001",
            "batches": ["FG-0924-B"],
            "stores": ["S1-南京东路店", "S2-徐家汇店"],
        }
        self.assertEqual([], check_action_scope(self.ledger, scope, None))
        self.assertEqual([], check_holds_before_disposition(self.ledger))


class ConservationGateTests(unittest.TestCase):
    def setUp(self):
        self.schema, self.events = load_sample()

    def test_quantity_not_conserved_blocks_downstream(self):
        events = copy.deepcopy(self.events)
        # 烘烤 G：凭空多出 10kg 成品。
        bake = by_id(events)["step-bake-G"]
        bake["payload"]["outputs"][0]["quantity"] = 205.0
        ledger = replay(events, self.schema)
        self.assertIn(("step-bake-G", "quantity_not_conserved"), codes(ledger))
        self.assertTrue(ledger.batches["FG-0924-G"].blocked)
        self.assertIn(("ship-S3-G", "blocked_shipment"), codes(ledger))

    def test_overdraw_is_rejected(self):
        events = copy.deepcopy(self.events)
        bake = by_id(events)["step-bake-B"]
        bake["payload"]["input_allocations"][0]["quantity"] = 999.0
        ledger = replay(events, self.schema)
        self.assertIn(("step-bake-B", "insufficient_stock"), codes(ledger))

    def test_sample_consumption_cannot_exceed_reserve(self):
        events = copy.deepcopy(self.events)
        obs = by_id(events)["obs-brine-A-salt"]
        obs["payload"]["sample_consumed"]["quantity"] = 5.0  # 步骤仅留样 2kg
        ledger = replay(events, self.schema)
        self.assertIn(("obs-brine-A-salt", "sample_over_consumed"), codes(ledger))

    def test_unit_mismatch_is_rejected(self):
        events = copy.deepcopy(self.events)
        bake = by_id(events)["step-bake-G"]
        bake["payload"]["unit"] = "g"
        ledger = replay(events, self.schema)
        self.assertIn(("step-bake-G", "unit_mismatch"), codes(ledger))


class DecisionRuleTests(unittest.TestCase):
    def setUp(self):
        self.schema, self.events = load_sample()

    def test_self_approval_is_blocked(self):
        events = copy.deepcopy(self.events)
        exempt = by_id(events)["exempt-brine-B-salt"]
        exempt["payload"]["granted_by"] = exempt["payload"]["responsible"]
        ledger = replay(events, self.schema)
        self.assertIn(("exempt-brine-B-salt", "self_approval"), codes(ledger))
        # 盐度超限又失去豁免，B 批放行随即暴露失败指标。
        self.assertIn(("release-B", "release_without_exemption"), codes(ledger))

    def test_release_without_measurement_is_blocked(self):
        events = drop(copy.deepcopy(self.events), "obs-brine-A-salt")
        events = drop(events, "obs-brine-C-salt")
        ledger = replay(events, self.schema)
        self.assertIn(("release-G", "missing_measurement"), codes(ledger))

    def test_release_must_pin_current_threshold(self):
        events = copy.deepcopy(self.events)
        # 9-26 的 N 批若仍钉旧版 water_pct v1，应被判错版。
        by_id(events)["release-N"]["payload"]["threshold_versions"]["water_pct"] = 1
        ledger = replay(events, self.schema)
        self.assertIn(("release-N", "threshold_not_current"), codes(ledger))

    def test_future_threshold_cannot_be_pinned(self):
        events = copy.deepcopy(self.events)
        # 提前发布、但 10 月才生效的版本：9-24 放行钉版 3 属于使用未生效阈值。
        future = {
            "event_id": "th-water-v3",
            "event_type": "THRESHOLD_PUBLISHED",
            "aggregate_type": "threshold",
            "aggregate_id": "threshold-water_pct",
            "occurred_at": "2026-09-10T00:00:00+08:00",
            "version": 3,
            "payload": {
                "metric": "water_pct",
                "limits": {"min": 20.0, "max": 35.0},
                "effective_from": "2026-10-01T00:00:00+08:00",
                "method_version": "MOISTURE-OVEN-v3",
            },
        }
        events.append(future)
        by_id(events)["release-G"]["payload"]["threshold_versions"]["water_pct"] = 3
        ledger = replay(events, self.schema)
        self.assertIn(("release-G", "threshold_not_effective"), codes(ledger))
        self.assertIn(("release-G", "threshold_not_current"), codes(ledger))

    def test_recheck_is_append_only_and_preserves_release(self):
        ledger = replay(self.events, self.schema)
        water_readings = [
            o.result
            for o in ledger.observations
            if o.aggregate_id == "thawed-B" and o.metric == "water_pct"
        ]
        self.assertEqual([43.0, 42.0], water_readings)  # 初检与复检都保留
        release = ledger.releases["FG-0924-B"][0]
        self.assertEqual(release["threshold_versions"]["water_pct"], 1)
        self.assertEqual(release["conclusion"], "released_with_exemptions")

    def test_version_gap_is_detected(self):
        events = copy.deepcopy(self.events)
        by_id(events)["release-G"]["version"] = 9
        ledger = replay(events, self.schema)
        self.assertIn(("release-G", "version_gap"), codes(ledger))


class ComplaintScopeTests(unittest.TestCase):
    def setUp(self):
        self.schema, self.events = load_sample()
        self.ledger = replay(self.events, self.schema)

    def test_store_and_purchase_window_narrowing_without_sample(self):
        complaint_id = "CMP-20260926-001"
        events = copy.deepcopy(self.events)
        complaint = by_id(events)["cmp-001"]
        complaint["payload"].pop("sample_batch_id")
        complaint["payload"].pop("sample_received")
        ledger = replay(events, self.schema)
        batches, reasons = candidate_batches(ledger, ledger.complaints[complaint_id])
        self.assertEqual(["FG-0924-B"], batches)
        self.assertTrue(any("ship-" in r for r in reasons))

    def test_unreachable_store_in_scope_is_rejected(self):
        scope = {
            "complaint_id": "CMP-20260926-001",
            "batches": ["FG-0924-B"],
            "stores": ["S99-不存在门店"],
        }
        issues = check_action_scope(self.ledger, scope, None)
        self.assertIn("scope_store_unreachable", {i["code"] for i in issues})

    def test_unrelated_batch_in_scope_is_rejected(self):
        scope = {
            "complaint_id": "CMP-20260926-001",
            "batches": ["FG-0924-G"],
            "stores": ["S1-南京东路店"],
        }
        issues = check_action_scope(self.ledger, scope, None)
        self.assertIn("scope_batch_unreachable", {i["code"] for i in issues})

    def test_disposition_without_prior_hold_is_rejected(self):
        events = drop(copy.deepcopy(self.events), "hold-case-001")
        ledger = replay(events, self.schema)
        issues = check_holds_before_disposition(ledger)
        self.assertTrue(any(i["code"] == "disposition_without_hold" for i in issues))


def split_ledger(schema, scrap_quantity=8.0):
    events = [
        {
            "event_id": "lot-L",
            "event_type": "LOT_ACCEPTED",
            "aggregate_type": "raw_yolk_lot",
            "aggregate_id": "raw-L",
            "occurred_at": "2026-09-20T09:00:00+08:00",
            "version": 1,
            "payload": {
                "supplier": "测试供应商",
                "grade": "A",
                "quantity": {"value": 100.0, "unit": "kg"},
                "certificates": ["CERT-L"],
            },
        },
        {
            "event_id": "step-split",
            "event_type": "BATCH_TRANSFORMED",
            "aggregate_type": "process_batch",
            "aggregate_id": "wip-P1",
            "occurred_at": "2026-09-21T08:00:00+08:00",
            "version": 1,
            "payload": {
                "stage": "grading",
                "step_id": "split",
                "unit": "kg",
                "input_allocations": [{"source_batch": "raw-L", "quantity": 100.0}],
                "outputs": [
                    {"batch_id": "wip-P1", "quantity": 40.0, "role": "product"},
                    {"batch_id": "wip-P2", "quantity": 50.0, "role": "product"},
                    {"quantity": 2.0, "role": "sample"},
                    {"quantity": scrap_quantity, "role": "scrap"},
                ],
            },
        },
    ]
    return replay(events, schema)


class SplitScrapTests(unittest.TestCase):
    def setUp(self):
        self.schema, _ = load_sample()

    def test_split_propagates_origins_to_each_output(self):
        ledger = split_ledger(self.schema)
        self.assertEqual([], ledger.violations)
        p1 = raw_attribution(ledger, "wip-P1")
        p2 = raw_attribution(ledger, "wip-P2")
        self.assertEqual([("raw-L", 1.0)], [(r["raw_lot"], r["share"]) for r in p1])
        self.assertEqual(40.0, ledger.batches["wip-P1"].available)
        self.assertEqual(50.0, ledger.batches["wip-P2"].available)
        self.assertEqual(8.0, 100.0 - 40.0 - 50.0 - 2.0)  # 报废量必须显式入账

    def test_scrap_imbalance_blocks_the_step(self):
        ledger = split_ledger(self.schema, scrap_quantity=7.0)  # 合计 99，凭空少 1kg
        self.assertIn(("step-split", "quantity_not_conserved"), codes(ledger))
        self.assertTrue(ledger.batches["wip-P1"].blocked)
        self.assertTrue(ledger.batches["wip-P2"].blocked)


if __name__ == "__main__":
    unittest.main()
