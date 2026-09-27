import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from formula_claim.impact import ImpactPropagator, InMemoryCheckpointStore
from formula_claim.model import (
    EVIDENCE_NUTRITION_TEST,
    EVIDENCE_TRIAL_RESULT,
    Actor,
    IngredientSpec,
    Role,
)
from formula_claim.service import ReleaseService

TZ = timezone(timedelta(hours=8))
RND = Actor("rd-1", Role.RND)
QA = Actor("qa-1", Role.QUALITY)
REG = Actor("reg-1", Role.REGULATORY)
PLAN = Actor("plan-1", Role.PLANNER)


def ts(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=TZ)


VALID_UNTIL = datetime(2027, 3, 1, tzinfo=TZ)


class ImpactTestBase(unittest.TestCase):
    def setUp(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        self.svc = ReleaseService(schema)
        self.store = InMemoryCheckpointStore()
        self.prop = ImpactPropagator(self.svc, self.store)

    def propose(self, formula_id, ingredients, occurred_at=None):
        return self.svc.propose_formula(
            RND,
            formula_id=formula_id,
            factory="F1",
            window_start=ts(1),
            window_end=ts(28, 18),
            ingredients=ingredients,
            occurred_at=occurred_at or ts(1),
        )

    def approve_with_pair(self, formula_id, claim_id, nt, tr):
        self.svc.confirm_evidence(
            QA, evidence_id=nt, formula_id=formula_id, kind=EVIDENCE_NUTRITION_TEST,
            method_ref="GB28050", valid_until=VALID_UNTIL, occurred_at=ts(2),
        )
        self.svc.confirm_evidence(
            QA, evidence_id=tr, formula_id=formula_id, kind=EVIDENCE_TRIAL_RESULT,
            method_ref="TR-09", valid_until=VALID_UNTIL, occurred_at=ts(2),
        )
        return self.svc.approve_claim(
            REG, claim_id=claim_id, formula_id=formula_id, text="低糖莲蓉月饼",
            market_scope={"华东"}, evidence_set=(nt, tr), occurred_at=ts(3),
        )

    def lock(self, lot_id, formula_id, version=1, qty=100, sold=0):
        self.svc.lock_lot(
            PLAN, lot_id=lot_id, formula_id=formula_id, formula_version=version,
            produced_at=ts(5), regions={"华东"}, inventory_batches=(), quantity=qty, occurred_at=ts(5),
        )
        if sold:
            self.svc.record_sale(lot_id=lot_id, qty=sold)


class EvidenceExpiryTests(ImpactTestBase):
    def setUp(self):
        super().setUp()
        self.propose("mooncake", {"lotus": IngredientSpec("lotus", "一级莲蓉", 120.0)})
        self.approve_with_pair("mooncake", "cl-a", "ev-a-nt", "ev-a-tr")
        self.propose("mooncake-b", {"redbean": IngredientSpec("redbean", "红豆沙", 100.0)})
        self.approve_with_pair("mooncake-b", "cl-b", "ev-b-nt", "ev-b-tr")
        self.lock("lot-a1", "mooncake")
        self.lock("lot-a2", "mooncake", sold=30)
        self.lock("lot-b1", "mooncake-b")

    def test_expiry_targets_precisely_and_resumes_from_checkpoint(self):
        job = self.prop.start_evidence_expiry(evidence_id="ev-a-nt", occurred_at=ts(20))
        self.assertEqual(["lot-a1", "lot-a2"], job.pending)
        self.assertEqual("expired", self.svc.evidence("ev-a-nt").status)

        job = self.prop.run(job.job_id, max_steps=1)
        self.assertEqual("running", job.status)
        self.assertEqual(["lot-a1"], job.processed)
        self.assertEqual(["lot-a2"], job.pending)
        checkpoint = self.store.load(job.job_id)
        self.assertEqual(["lot-a1"], checkpoint.processed)

        job = self.prop.resume(job.job_id)
        self.assertEqual("completed", job.status)
        self.assertEqual(["lot-a1", "lot-a2"], job.processed)
        self.assertEqual([("lot-a2", 30)], [(n["lot_id"], n["sold_qty"]) for n in job.notifications])

        lot_a1 = self.svc.lot("lot-a1")
        self.assertEqual({"华东"}, lot_a1.packaging_holds["cl-a"])
        denied = self.svc.evaluate_lot_claims(lot_id="lot-a1", on_date=ts(21), region="华东").denied
        self.assertIn("该声明未售包装已暂停", denied[0].reasons)
        self.assertIn("证据已失效：ev-a-nt", denied[0].reasons)

        lot_b1 = self.svc.lot("lot-b1")
        self.assertEqual({}, lot_b1.packaging_holds)
        allowed = self.svc.evaluate_lot_claims(lot_id="lot-b1", on_date=ts(21), region="华东").allowed
        self.assertEqual(("低糖莲蓉月饼",), allowed)

    def test_completed_job_is_idempotent(self):
        job = self.prop.start_evidence_expiry(evidence_id="ev-a-nt", occurred_at=ts(20))
        self.prop.run(job.job_id)
        event_count = len(self.svc.events)
        again = self.prop.run(job.job_id)
        self.assertEqual("completed", again.status)
        self.assertEqual(event_count, len(self.svc.events))
        propagated = [e for e in self.svc.events if e["event_type"] == "IMPACT_PROPAGATED"]
        lot_events = [e for e in propagated if e["aggregate_type"] == "production_lot"]
        self.assertEqual(2, len(lot_events))


class IngredientChangeTests(ImpactTestBase):
    def test_change_hits_only_lots_locked_on_versions_using_it(self):
        self.propose("mooncake", {"lotus": IngredientSpec("lotus", "一级莲蓉", 120.0)})
        self.lock("lot-old", "mooncake", version=1)
        self.propose(
            "mooncake",
            {
                "lotus": IngredientSpec("lotus", "一级莲蓉", 120.0),
                "walnut": IngredientSpec("walnut", "纸皮核桃", 30.0),
            },
            occurred_at=ts(6),
        )
        self.lock("lot-new", "mooncake", version=2)
        job = self.prop.start_ingredient_change(ingredient_id="walnut", occurred_at=ts(20))
        self.assertEqual(["lot-new"], job.pending)
        self.prop.run(job.job_id)
        self.assertEqual({"原料变更待评估：walnut"}, self.svc.lot("lot-new").hold_reasons)
        self.assertEqual(set(), self.svc.lot("lot-old").hold_reasons)


class RecallTests(ImpactTestBase):
    def test_recall_marks_lots_and_blocks_evaluation(self):
        self.propose("mooncake", {"lotus": IngredientSpec("lotus", "一级莲蓉", 120.0)})
        self.approve_with_pair("mooncake", "cl-a", "ev-a-nt", "ev-a-tr")
        self.lock("lot-1", "mooncake")
        job = self.prop.start_recall(lot_ids=["lot-1"], reason="异物投诉", occurred_at=ts(20))
        self.prop.run(job.job_id)
        self.assertTrue(self.svc.lot("lot-1").under_recall)
        denied = self.svc.evaluate_lot_claims(lot_id="lot-1", on_date=ts(21), region="华东").denied
        self.assertIn("批次处于召回状态", denied[0].reasons)


if __name__ == "__main__":
    unittest.main()
