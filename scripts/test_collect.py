#!/usr/bin/env python3
"""Regression tests for the differ. Run: python scripts/test_collect.py"""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect import classify, merge_baseline, schema_of, unobserved

# The real shape: statuspage summary.json with one open incident, then none.
WITH_INCIDENT = schema_of({
    "page": {"id": "x", "name": "Twilio"},
    "status": {"indicator": "none", "description": "All Systems Operational"},
    "components": [{"id": "c1", "name": "SMS", "status": "operational", "group": False}],
    "incidents": [{
        "id": "i1", "name": "Elevated errors", "status": "resolved",
        "created_at": "2026-08-18T22:00:00Z", "shortlink": "http://stspg.io/x",
        "components": [{"id": "c1", "name": "SMS", "description": None, "group": False,
                        "created_at": "2026-08-18T22:00:00Z"}],
        "incident_updates": [{"id": "u1", "body": "Recovered", "status": "resolved",
                              "affected_components": [{"code": "c1"}]}],
    }],
    "scheduled_maintenances": [],
})
NO_INCIDENT = schema_of({
    "page": {"id": "x", "name": "Twilio"},
    "status": {"indicator": "none", "description": "All Systems Operational"},
    "components": [{"id": "c1", "name": "SMS", "status": "operational", "group": False}],
    "incidents": [],
    "scheduled_maintenances": [],
})


class EmptyContainersAreNotRemovals(unittest.TestCase):
    def test_emptied_list_reports_nothing(self):
        changes, carried, _fo = classify(WITH_INCIDENT, NO_INCIDENT)
        self.assertEqual([], changes, f"emptied incidents[] produced {len(changes)} phantom change(s)")
        self.assertTrue(carried, "children of the emptied list must be carried forward")
        self.assertTrue(all(c.startswith("incidents") for c in carried))

    def test_blind_set_finds_the_empty_list(self):
        self.assertIn("incidents", unobserved(NO_INCIDENT))
        self.assertNotIn("incidents", unobserved(WITH_INCIDENT))

    def test_refill_does_not_replay_as_additions(self):
        _, carried, _fo = classify(WITH_INCIDENT, NO_INCIDENT)
        baseline = merge_baseline(WITH_INCIDENT, NO_INCIDENT, carried, "json")
        self.assertEqual(WITH_INCIDENT, baseline, "baseline lost the unobserved paths")
        again, _, _fo = classify(baseline, WITH_INCIDENT)
        self.assertEqual([], again, "incident coming back fired FIELD_ADDED")


class RealChangesStillFire(unittest.TestCase):
    def test_removal_from_a_populated_object_is_breaking(self):
        old = {"page": ["object"], "page.id": ["string"], "page.name": ["string"]}
        new = {"page": ["object"], "page.id": ["string"]}
        changes, carried, _fo = classify(old, new)
        self.assertEqual({}, carried)
        self.assertEqual(["FIELD_REMOVED"], [c["kind"] for c in changes])
        self.assertEqual("breaking", changes[0]["severity"])

    def test_removal_inside_a_still_populated_list_is_breaking(self):
        old = {"i": ["array"], "i[]": ["object"], "i[].id": ["string"], "i[].name": ["string"]}
        new = {"i": ["array"], "i[]": ["object"], "i[].id": ["string"]}
        changes, _, _fo = classify(old, new)
        self.assertEqual(["FIELD_REMOVED"], [c["kind"] for c in changes])

    def test_type_swap_is_breaking(self):
        changes, _, _fo = classify({"a": ["string"]}, {"a": ["integer"]})
        self.assertEqual("TYPE_CHANGED", changes[0]["kind"])
        self.assertEqual("breaking", changes[0]["severity"])

    def test_new_field_is_additive(self):
        changes, _, _fo = classify({"a": ["string"]}, {"a": ["string"], "b": ["string"]})
        self.assertEqual("FIELD_ADDED", changes[0]["kind"])


class SamplingVarianceIsNotAContractChange(unittest.TestCase):
    def test_newly_sampled_null_is_info_not_breaking(self):
        old = {"a": ["array"]}
        new = {"a": ["array", "null"]}
        changes, _, _fo = classify(old, new)
        self.assertEqual("TYPE_WIDENED", changes[0]["kind"])
        self.assertEqual("info", changes[0]["severity"])

    def test_widening_is_remembered_so_it_fires_once(self):
        old, new = {"a": ["array"]}, {"a": ["array", "null"]}
        baseline = merge_baseline(old, new, {}, "json")
        self.assertEqual(["array", "null"], baseline["a"])
        self.assertEqual([], classify(baseline, old)[0], "type flip-flopped back")

    def test_unsampled_variant_is_silent(self):
        old, new = {"a": ["array", "null"]}, {"a": ["array"]}
        self.assertEqual([], classify(old, new)[0])
        self.assertEqual("NULLABILITY", classify(old, new, sampled=False)[0][0]["kind"])

    def test_specs_are_not_unioned(self):
        old, new = {"a": ["x"]}, {"a": ["y"]}
        self.assertEqual(["y"], merge_baseline(old, new, {}, "spec")["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
