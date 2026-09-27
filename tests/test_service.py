import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from formula_claim.contracts import validate_event
from formula_claim.model import (
    EVIDENCE_NUTRITION_TEST,
    EVIDENCE_TRIAL_RESULT,
    Actor,
    ClaimTier,
    DomainError,
    IngredientSpec,
    Role,
    classify_claim_text,
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


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        self.svc = ReleaseService(schema)
        self.schema = schema

    def propose(self, formula_id="mooncake", ingredients=None, occurred_at=None):
        return self.svc.propose_formula(
            RND,
            formula_id=formula_id,
            factory="F1",
            window_start=ts(1),
            window_end=ts(28, 18),
            ingredients=ingredients or {"lotus": IngredientSpec("lotus", "一级莲蓉", 120.0)},
            allergens=("egg",),
            homology_basis=(),
            substitutions={"sucrose": "maltitol"},
            occurred_at=occurred_at or ts(1),
        )

    def confirm_pair(self, formula_id="mooncake", nt="ev-nt", tr="ev-tr"):
        self.svc.confirm_evidence(
            QA, evidence_id=nt, formula_id=formula_id, kind=EVIDENCE_NUTRITION_TEST,
            method_ref="GB28050", valid_until=VALID_UNTIL, occurred_at=ts(2),
        )
        self.svc.confirm_evidence(
            QA, evidence_id=tr, formula_id=formula_id, kind=EVIDENCE_TRIAL_RESULT,
            method_ref="TR-09", valid_until=VALID_UNTIL, occurred_at=ts(2),
        )
        return nt, tr

    def approve_nutrition(self, claim_id="cl-low-sugar", formula_id="mooncake", scope=("华东",), occurred_at=None):
        nt, tr = "ev-nt", "ev-tr"
        return self.svc.approve_claim(
            REG,
            claim_id=claim_id,
            formula_id=formula_id,
            text="低糖莲蓉月饼",
            market_scope=set(scope),
            evidence_set=(nt, tr),
            occurred_at=occurred_at or ts(3),
        )

    def lock(self, lot_id="lot-1", formula_id="mooncake", version=1, produced=None, regions=("华东",), qty=100):
        return self.svc.lock_lot(
            PLAN,
            lot_id=lot_id,
            formula_id=formula_id,
            formula_version=version,
            produced_at=produced or ts(5),
            regions=set(regions),
            inventory_batches=("inv-1",),
            quantity=qty,
            occurred_at=produced or ts(5),
        )


class FullChainTests(ServiceTestBase):
    def test_full_chain_allows_claim_and_emits_ordered_events(self):
        self.propose()
        self.confirm_pair()
        self.approve_nutrition()
        self.lock()
        evaluation = self.svc.evaluate_lot_claims(lot_id="lot-1", on_date=ts(10), region="华东")
        self.assertEqual(("低糖莲蓉月饼",), evaluation.allowed)
        self.assertEqual((), evaluation.denied)
        self.assertEqual(
            ["FORMULA_VERSIONED", "EVIDENCE_ACCEPTED", "EVIDENCE_ACCEPTED", "CLAIM_APPROVED", "LOT_LOCKED"],
            [event["event_type"] for event in self.svc.events],
        )
        for event in self.svc.events:
            self.assertEqual([], validate_event(event, self.schema))

    def test_deny_explains_region_and_expired_evidence(self):
        self.propose()
        self.confirm_pair()
        self.approve_nutrition()
        self.lock()
        wrong_region = self.svc.evaluate_lot_claims(lot_id="lot-1", on_date=ts(10), region="华北")
        self.assertEqual((), wrong_region.allowed)
        self.assertIn("地区不在放行范围内", wrong_region.denied[0].reasons)
        expired = self.svc.evaluate_lot_claims(
            lot_id="lot-1", on_date=datetime(2027, 4, 1, tzinfo=TZ), region="华东"
        )
        self.assertIn("证据已过期：ev-nt", expired.denied[0].reasons)
        self.assertIn("证据已过期：ev-tr", expired.denied[0].reasons)


class SeparationOfDutyTests(ServiceTestBase):
    def test_roles_are_enforced(self):
        self.propose()
        with self.assertRaises(DomainError) as ctx:
            self.svc.confirm_evidence(
                RND, evidence_id="ev-x", formula_id="mooncake", kind=EVIDENCE_NUTRITION_TEST,
                method_ref="m", valid_until=VALID_UNTIL, occurred_at=ts(2),
            )
        self.assertEqual("permission_denied", ctx.exception.code)

    def test_same_person_cannot_complete_chain(self):
        self.propose()
        same_person_quality = Actor("rd-1", Role.QUALITY)
        with self.assertRaises(DomainError) as ctx:
            self.svc.confirm_evidence(
                same_person_quality, evidence_id="ev-x", formula_id="mooncake",
                kind=EVIDENCE_NUTRITION_TEST, method_ref="m", valid_until=VALID_UNTIL, occurred_at=ts(2),
            )
        self.assertEqual("sod_conflict", ctx.exception.code)
        self.confirm_pair()
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_claim(
                Actor("rd-1", Role.REGULATORY), claim_id="cl-a", formula_id="mooncake",
                text="低糖莲蓉月饼", market_scope={"华东"}, evidence_set=("ev-nt", "ev-tr"), occurred_at=ts(3),
            )
        self.assertEqual("sod_conflict", ctx.exception.code)
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_claim(
                Actor("qa-1", Role.REGULATORY), claim_id="cl-a", formula_id="mooncake",
                text="低糖莲蓉月饼", market_scope={"华东"}, evidence_set=("ev-nt", "ev-tr"), occurred_at=ts(3),
            )
        self.assertEqual("sod_conflict", ctx.exception.code)


class EvidenceThresholdTests(ServiceTestBase):
    def test_nutrition_claim_requires_test_and_trial(self):
        self.propose()
        self.confirm_pair()
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_claim(
                REG, claim_id="cl-a", formula_id="mooncake", text="低糖莲蓉月饼",
                market_scope={"华东"}, evidence_set=("ev-tr",), occurred_at=ts(3),
            )
        self.assertEqual("threshold_not_met", ctx.exception.code)
        self.assertIn("缺少证据：营养检测", ctx.exception.details)

    def test_flavor_claim_has_lower_threshold(self):
        self.propose()
        self.assertEqual(ClaimTier.NUTRITION, classify_claim_text("减油五仁月饼"))
        self.assertEqual(ClaimTier.FLAVOR, classify_claim_text("清香莲蓉月饼"))
        claim = self.svc.approve_claim(
            REG, claim_id="cl-flavor", formula_id="mooncake", text="清香莲蓉月饼",
            market_scope={"华东"}, evidence_set=(), occurred_at=ts(2),
        )
        self.assertEqual(ClaimTier.FLAVOR, claim.tier)

    def test_expired_evidence_blocks_approval(self):
        self.propose()
        self.svc.confirm_evidence(
            QA, evidence_id="ev-old", formula_id="mooncake", kind=EVIDENCE_NUTRITION_TEST,
            method_ref="m", valid_until=ts(2), occurred_at=ts(2),
        )
        self.svc.confirm_evidence(
            QA, evidence_id="ev-tr", formula_id="mooncake", kind=EVIDENCE_TRIAL_RESULT,
            method_ref="m", valid_until=VALID_UNTIL, occurred_at=ts(2),
        )
        with self.assertRaises(DomainError) as ctx:
            self.svc.approve_claim(
                REG, claim_id="cl-a", formula_id="mooncake", text="低糖莲蓉月饼",
                market_scope={"华东"}, evidence_set=("ev-old", "ev-tr"), occurred_at=ts(3),
            )
        self.assertEqual("threshold_not_met", ctx.exception.code)
        self.assertIn("证据已过期：ev-old", ctx.exception.details)


class LotLockTests(ServiceTestBase):
    def test_production_must_fall_in_version_window(self):
        self.propose()
        with self.assertRaises(DomainError) as ctx:
            self.lock(produced=ts(30))
        self.assertEqual("window_mismatch", ctx.exception.code)
        with self.assertRaises(DomainError) as ctx:
            self.lock(version=9)
        self.assertEqual("not_found", ctx.exception.code)

    def test_locked_lot_is_not_rewritten_by_later_revision(self):
        self.propose()
        self.lock(lot_id="lot-1", produced=ts(5))
        self.propose(
            ingredients={
                "lotus": IngredientSpec("lotus", "一级莲蓉", 120.0),
                "walnut": IngredientSpec("walnut", "纸皮核桃", 30.0),
            },
            occurred_at=ts(6),
        )
        self.assertEqual(1, self.svc.lot("lot-1").formula_version)
        self.assertEqual([], self.svc.find_lots_using_ingredient("walnut"))
        self.assertEqual(["lot-1"], self.svc.find_lots_using_ingredient("lotus"))

    def test_claim_stamped_with_version_does_not_reach_older_lots(self):
        self.propose()
        self.lock(lot_id="lot-old", produced=ts(5))
        self.propose(occurred_at=ts(6))
        self.confirm_pair()
        self.approve_nutrition(occurred_at=ts(7))
        self.lock(lot_id="lot-new", version=2, produced=ts(10))
        old = self.svc.evaluate_lot_claims(lot_id="lot-old", on_date=ts(12), region="华东")
        self.assertEqual((), old.allowed)
        self.assertIn("声明对应配方版本与批次锁定版本不一致", old.denied[0].reasons)
        new = self.svc.evaluate_lot_claims(lot_id="lot-new", on_date=ts(12), region="华东")
        self.assertEqual(("低糖莲蓉月饼",), new.allowed)

    def test_claim_approved_after_production_is_denied(self):
        self.propose()
        self.confirm_pair()
        self.lock(lot_id="lot-1", produced=ts(5))
        self.approve_nutrition(occurred_at=ts(6))
        evaluation = self.svc.evaluate_lot_claims(lot_id="lot-1", on_date=ts(10), region="华东")
        self.assertIn("声明批准时间晚于批次生产时间", evaluation.denied[0].reasons)


class NarrowingTests(ServiceTestBase):
    def test_narrow_pauses_unsold_and_notifies_sold(self):
        self.propose()
        self.confirm_pair()
        self.approve_nutrition(scope=("华东", "华南"))
        self.lock(lot_id="lot-a", regions=("华东", "华南"), qty=100)
        self.lock(lot_id="lot-b", regions=("华南",), qty=50)
        self.svc.record_sale(lot_id="lot-a", qty=40)
        result = self.svc.narrow_claim(REG, claim_id="cl-low-sugar", removed_regions={"华南"}, occurred_at=ts(8))
        self.assertEqual(("华东",), result.remaining_scope)
        self.assertEqual({"lot-a", "lot-b"}, {pause.lot_id for pause in result.paused})
        self.assertEqual([("lot-a", 40)], [(note.lot_id, note.sold_qty) for note in result.notifications])
        south = self.svc.evaluate_lot_claims(lot_id="lot-a", on_date=ts(10), region="华南")
        self.assertIn("该声明未售包装已暂停", south.denied[0].reasons)
        east = self.svc.evaluate_lot_claims(lot_id="lot-a", on_date=ts(10), region="华东")
        self.assertEqual(("低糖莲蓉月饼",), east.allowed)
        self.assertEqual(2, self.svc.claim("cl-low-sugar").version)
        impacted = [e for e in self.svc.events if e["event_type"] == "IMPACT_PROPAGATED"]
        self.assertEqual({"lot-a", "lot-b"}, {e["aggregate_id"] for e in impacted})

    def test_narrow_rejects_regions_outside_scope(self):
        self.propose()
        self.confirm_pair()
        self.approve_nutrition()
        with self.assertRaises(DomainError) as ctx:
            self.svc.narrow_claim(REG, claim_id="cl-low-sugar", removed_regions={"华北"}, occurred_at=ts(8))
        self.assertEqual("invalid_scope", ctx.exception.code)


class SharedResourceTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.svc.set_resource_pool(factory="F1", print_quota=10000, ingredients={"maltitol": 500})

    def reserve(self, plan_id, print_units, ingredients):
        return self.svc.reserve_plan(
            PLAN, plan_id=plan_id, factory="F1", print_units=print_units, ingredients=ingredients
        )

    def test_confirm_avoids_double_commitment(self):
        self.reserve("p1", 6000, {"maltitol": 300})
        self.svc.confirm_plan(PLAN, plan_id="p1")
        self.reserve("p2", 5000, {})
        with self.assertRaises(DomainError) as ctx:
            self.svc.confirm_plan(PLAN, plan_id="p2")
        self.assertEqual("resource_shortage", ctx.exception.code)
        self.assertIn("印刷额度不足：需要5000，可用4000", ctx.exception.details)
        self.reserve("p3", 1000, {"maltitol": 300})
        with self.assertRaises(DomainError) as ctx:
            self.svc.confirm_plan(PLAN, plan_id="p3")
        self.assertIn("合格原料maltitol不足：需要300，可用200", ctx.exception.details)
        self.reserve("p4", 4000, {"maltitol": 200})
        self.svc.confirm_plan(PLAN, plan_id="p4")
        again = self.svc.confirm_plan(PLAN, plan_id="p1")
        self.assertEqual("committed", again.status)

    def test_release_frees_shared_quota(self):
        self.reserve("p1", 6000, {"maltitol": 300})
        self.svc.confirm_plan(PLAN, plan_id="p1")
        self.svc.release_plan(PLAN, plan_id="p1")
        self.reserve("p2", 9000, {"maltitol": 400})
        self.svc.confirm_plan(PLAN, plan_id="p2")
        self.assertEqual("committed", self.svc.plan("p2").status)


if __name__ == "__main__":
    unittest.main()
