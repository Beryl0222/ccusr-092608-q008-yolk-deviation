import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(ROOT / "src"))

from yolk_deviation.contracts import validate_event

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))


def base_event(**overrides):
    event = {
        "event_id": "evt-1",
        "event_type": "LOT_ACCEPTED",
        "aggregate_type": "raw_yolk_lot",
        "aggregate_id": "LOT-1",
        "occurred_at": "2026-09-25T10:00:00+08:00",
        "version": 1,
        "payload": {
            "supplier_id": "S-01",
            "supplier_certificate_refs": ["CERT-1"],
            "grade": "A",
            "quantity": 10.0,
            "unit": "kg",
        },
    }
    event.update(overrides)
    return event


class EnvelopeTests(unittest.TestCase):
    def test_missing_fields_are_stable(self):
        issues = validate_event({}, SCHEMA)
        fields = sorted(x.field for x in issues)
        self.assertEqual(fields, sorted(fields))

    def test_time_and_version_boundaries(self):
        issues = validate_event(base_event(occurred_at="2026-09-25T10:00:00", version=0), SCHEMA)
        codes = {(x.field, x.code) for x in issues}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_unknown_event_is_rejected(self):
        issues = validate_event(base_event(event_type="UNKNOWN"), SCHEMA)
        self.assertIn(("event_type", "unsupported_value"), [(x.field, x.code) for x in issues])

    def test_aggregate_event_mismatch(self):
        issues = validate_event(base_event(event_type="BATCH_TRANSFORMED",
                                           aggregate_type="raw_yolk_lot",
                                           aggregate_id="X",
                                           payload={"step": "baking",
                                                    "input_allocations": [],
                                                    "outputs": []}), SCHEMA)
        self.assertIn(("aggregate_type", "aggregate_event_mismatch"),
                      [(x.field, x.code) for x in issues])


class PayloadTests(unittest.TestCase):
    def test_transform_requires_allocations_and_outputs(self):
        event = base_event(event_type="BATCH_TRANSFORMED", aggregate_type="process_batch",
                           aggregate_id="WIP-1", payload={"step": "baking"})
        fields = {x.field for x in validate_event(event, SCHEMA)}
        self.assertIn("payload.input_allocations", fields)
        self.assertIn("payload.outputs", fields)

    def test_fractions_must_sum_to_one(self):
        event = base_event(
            event_type="BATCH_TRANSFORMED", aggregate_type="process_batch", aggregate_id="WIP-1",
            payload={
                "step": "baking",
                "input_allocations": [
                    {"source_batch_id": "LOT-1", "quantity": 6.0, "unit": "kg", "fraction": 0.6},
                    {"source_batch_id": "LOT-2", "quantity": 3.0, "unit": "kg", "fraction": 0.3},
                ],
                "outputs": [{"batch_id": "WIP-1", "quantity": 9.0, "unit": "kg"}],
            })
        self.assertIn(("payload.input_allocations", "fractions_must_sum_to_one"),
                      [(x.field, x.code) for x in validate_event(event, SCHEMA)])

    def test_fraction_range(self):
        event = base_event(
            event_type="BATCH_TRANSFORMED", aggregate_type="process_batch", aggregate_id="WIP-1",
            payload={
                "step": "baking",
                "input_allocations": [
                    {"source_batch_id": "LOT-1", "quantity": 6.0, "unit": "kg", "fraction": 1.4},
                ],
                "outputs": [{"batch_id": "WIP-1", "quantity": 6.0, "unit": "kg"}],
            })
        self.assertIn(("payload.input_allocations[0].fraction", "ratio_out_of_range"),
                      [(x.field, x.code) for x in validate_event(event, SCHEMA)])

    def test_self_exemption_is_rejected_at_contract(self):
        event = base_event(
            event_type="DEVIATION_EXEMPTION_GRANTED", aggregate_type="process_batch",
            aggregate_id="WIP-1",
            payload={"batch_id": "WIP-1", "step": "thawing", "observation_id": "OBS-1",
                     "decision": "APPROVED", "granted_by": "u-leader", "step_owner": "u-leader",
                     "granted_at": "2026-09-12T08:00:00+08:00"})
        self.assertIn(("payload.granted_by", "self_approval_forbidden"),
                      [(x.field, x.code) for x in validate_event(event, SCHEMA)])

    def test_observation_requires_timezone(self):
        event = base_event(
            event_type="OBSERVATION_RECORDED", aggregate_type="quality_observation",
            aggregate_id="OBS-1",
            payload={"subject_type": "process_batch", "subject_id": "WIP-1",
                     "check_stage": "curing", "method_version": "v1", "rule_version": "r1",
                     "observed_at": "2026-09-12T10:00:00",
                     "result": {"metric": "salt_pct", "value": 4.0}, "observed_by": "u-qc"})
        self.assertIn(("payload.observed_at", "timezone_required"),
                      [(x.field, x.code) for x in validate_event(event, SCHEMA)])

    def test_release_decision_enum(self):
        event = base_event(
            event_type="RELEASE_DECIDED", aggregate_type="process_batch", aggregate_id="WIP-1",
            payload={"batch_id": "WIP-1", "decision": "MAYBE", "rule_version": "r1",
                     "decided_by": "u-q", "decided_at": "2026-09-13T10:00:00+08:00"})
        self.assertIn(("payload.decision", "unsupported_value"),
                      [(x.field, x.code) for x in validate_event(event, SCHEMA)])


if __name__ == "__main__":
    unittest.main()
