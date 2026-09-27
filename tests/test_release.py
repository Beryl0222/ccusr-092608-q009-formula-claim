import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from formula_claim.capacity import CapacityLedger, CapacityError
from formula_claim.domain import (
    LOW_SUGAR_MAX_G_PER_100G,
    REDUCED_OIL_MIN_PERCENT,
    ReleaseError,
    ReleaseStore,
    evaluate_gate,
    load_schema,
    parse_dt,
)

CST = timezone(timedelta(hours=8))
RD = {"id": "rd-li", "role": "rd"}
QA = {"id": "qa-wang", "role": "quality"}
REG = {"id": "reg-zhao", "role": "regulatory"}
PLANT = {"id": "plant-sh1", "role": "factory"}


def at(y, m, d, h=9):
    return datetime(y, m, d, h, tzinfo=CST)


class Scenario:
    """构造一个完整的 v1 经典 / v2 健康配方放行场景。"""

    FID = "formula:莲蓉月饼"
    REF1 = {"formula_id": FID, "version": 1}
    REF2 = {"formula_id": FID, "version": 2}

    def __init__(self, totals=None):
        self.store = ReleaseStore(
            load_schema(ROOT / "contracts/domain.schema.json"),
            totals or {"pool:print": 1000.0, "pool:lotus": 500.0},
        )
        s = self.store
        s.change_ingredient_spec(RD, "spec:莲蓉", "白莲蓉", 1, event_id="sp1", occurred_at=at(2026, 7, 20))
        s.version_formula(RD, self.FID, "莲蓉月饼", "2026中秋",
                          ingredients=[{"ingredient": "白莲蓉", "spec_id": "spec:莲蓉", "spec_version": 1}],
                          event_id="f1", occurred_at=at(2026, 8, 1))
        s.version_formula(RD, self.FID, "莲蓉月饼", "2026中秋",
                          ingredients=[{"ingredient": "低糖白莲蓉", "spec_id": "spec:莲蓉", "spec_version": 1}],
                          replaces=self.REF1, event_id="f2", occurred_at=at(2026, 8, 3))
        s.accept_evidence(QA, "ev:sugar", formula_ref=self.REF2, kind="low_sugar",
                          method_ref="GB 5009.8-2023", valid_until="2027-06-30T23:59:59+08:00",
                          value=4.2, unit="g/100g", event_id="es", occurred_at=at(2026, 8, 18))
        s.accept_evidence(QA, "ev:oil", formula_ref=self.REF2, kind="reduced_oil",
                          method_ref="GB 5009.6-2016", valid_until="2026-11-30T23:59:59+08:00",
                          value=8.0, baseline_value=12.0, unit="g/100g",
                          event_id="eo", occurred_at=at(2026, 8, 18))
        s.accept_evidence(QA, "ev:homo", formula_ref=self.REF2, kind="homologous",
                          method_ref="文件评审", valid_until="2027-12-31T23:59:59+08:00",
                          basis_doc="药食同源物质目录", event_id="eh", occurred_at=at(2026, 8, 19))
        s.accept_evidence(QA, "ev:flavor", formula_ref=self.REF1, kind="flavor",
                          method_ref="感官评定", valid_until="2027-09-30T23:59:59+08:00",
                          event_id="ef", occurred_at=at(2026, 8, 19))
        s.approve_claim(REG, "claim:sugar", formula_ref=self.REF2, text="低糖莲蓉月饼",
                        kind="low_sugar", market_scope=["CN-SH", "CN-BJ"],
                        evidence_set=["ev:sugar"], event_id="cs", occurred_at=at(2026, 8, 22))
        s.approve_claim(REG, "claim:oil", formula_ref=self.REF2, text="减脂30%",
                        kind="reduced_oil", market_scope=["CN-SH", "CN-BJ"],
                        evidence_set=["ev:oil"], event_id="co", occurred_at=at(2026, 8, 22))
        s.approve_claim(REG, "claim:homo", formula_ref=self.REF2, text="药食同源陈皮山药",
                        kind="homologous", market_scope=["CN-SH"],
                        evidence_set=["ev:homo"], event_id="ch", occurred_at=at(2026, 8, 22))
        s.approve_claim(REG, "claim:flavor", formula_ref=self.REF1, text="古法莲蓉风味",
                        kind="flavor", market_scope=["CN-SH", "CN-BJ", "CN-GZ"],
                        evidence_set=["ev:flavor"], event_id="cf", occurred_at=at(2026, 8, 22))

    def v2_packages(self, regions=("CN-SH", "CN-BJ")):
        regions = list(regions)
        return [
            {"ref": "P1", "claim_id": "claim:sugar", "text": "低糖莲蓉月饼",
             "kind": "low_sugar", "regions": regions},
            {"ref": "P2", "claim_id": "claim:oil", "text": "减脂30%",
             "kind": "reduced_oil", "regions": regions},
            {"ref": "P3", "claim_id": "claim:homo", "text": "药食同源陈皮山药",
             "kind": "homologous", "regions": [r for r in regions if r == "CN-SH"]},
        ]

    def lock(self, lot_id, version, regions, packages=None, *, produced=None,
             when=None):
        when = when or at(2026, 9, 10)
        if packages is None:
            packages = [p for p in self.v2_packages(regions) if p["regions"]]
        produced = produced or when.replace(hour=6).isoformat()
        return self.store.lock_lot(
            PLANT, lot_id, formula_id=self.FID, formula_version=version, plant="上海一厂",
            produced_at=produced, sale_regions=regions, packages=packages, quantity=1000,
            event_id=f"lock-{lot_id}", occurred_at=when or at(2026, 9, 10))


class GateTests(unittest.TestCase):
    def ev(self, **kw):
        base = {"valid_until": "2027-01-01T00:00:00+08:00"}
        base.update(kw)
        return base

    def test_flavor_needs_no_evidence(self):
        self.assertEqual([], evaluate_gate("flavor", [], at(2026, 9, 1)))

    def test_low_sugar_thresholds(self):
        as_of = at(2026, 9, 1)
        self.assertTrue(evaluate_gate("low_sugar", [], as_of))
        expired = [self.ev(valid_until="2026-08-01T00:00:00+08:00", value=2.0)]
        self.assertIn("过期", evaluate_gate("low_sugar", expired, as_of)[0])
        over = [self.ev(value=LOW_SUGAR_MAX_G_PER_100G + 0.1)]
        self.assertIn("超过低糖门槛", evaluate_gate("low_sugar", over, as_of)[0])
        passed = [self.ev(value=LOW_SUGAR_MAX_G_PER_100G)]
        self.assertEqual([], evaluate_gate("low_sugar", passed, as_of))

    def test_reduced_oil_needs_quarter_drop(self):
        as_of = at(2026, 9, 1)
        self.assertTrue(evaluate_gate("reduced_oil", [], as_of))
        weak = [self.ev(value=10.0, baseline_value=12.0)]  # 仅降 16.7%
        self.assertIn(f"≥{REDUCED_OIL_MIN_PERCENT:g}%", evaluate_gate("reduced_oil", weak, as_of)[0])
        strong = [self.ev(value=9.0, baseline_value=12.0)]  # 降 25%
        self.assertEqual([], evaluate_gate("reduced_oil", strong, as_of))
        expired = [self.ev(valid_until="2026-08-01T00:00:00+08:00", value=8.0, baseline_value=12.0)]
        self.assertIn("过期", evaluate_gate("reduced_oil", expired, as_of)[0])

    def test_homologous_needs_basis_doc(self):
        as_of = at(2026, 9, 1)
        self.assertIn("依据文件", evaluate_gate("homologous", [], as_of)[0])
        self.assertIn("basis_doc", evaluate_gate("homologous", [self.ev()], as_of)[0])
        self.assertEqual([], evaluate_gate("homologous", [self.ev(basis_doc="目录")], as_of))


class SeparationTests(unittest.TestCase):
    def setUp(self):
        self.sc = Scenario()

    def assert_rejected(self, coro, code):
        with self.assertRaises(ReleaseError) as ctx:
            coro()
        self.assertEqual(code, ctx.exception.code)

    def test_each_step_has_fixed_role(self):
        s = self.sc.store
        self.assert_rejected(
            lambda: s.version_formula(QA, "f:x", "p", "w", event_id="x1", occurred_at=at(2026, 9, 1)),
            "contract_violation")
        self.assert_rejected(
            lambda: s.approve_claim(RD, "c:x", formula_ref=Scenario.REF2, text="t", kind="flavor",
                                    market_scope=["CN-SH"], evidence_set=[], event_id="x2",
                                    occurred_at=at(2026, 9, 1)),
            "contract_violation")
        self.assert_rejected(
            lambda: s.accept_evidence(REG, "e:x", formula_ref=Scenario.REF2, kind="flavor",
                                      method_ref="m", valid_until="2027-01-01T00:00:00+08:00",
                                      event_id="x3", occurred_at=at(2026, 9, 1)),
            "contract_violation")
        self.assert_rejected(
            lambda: s.recall_lot(PLANT, "lot:nope", "r", event_id="x4", occurred_at=at(2026, 9, 1)),
            "contract_violation")

    def test_no_single_actor_can_release_chain(self):
        # 即使同一个人 id 也无法靠一个角色串完全链：每一步的事件角色都是强约束。
        for actor in ({"id": "same-person", "role": r} for r in ("rd", "quality", "regulatory", "factory")):
            role = actor["role"]
            if role != "rd":
                self.assert_rejected(
                    lambda a=actor: self.sc.store.version_formula(
                        a, "f:new", "p", "w", event_id=f"dup-{role}", occurred_at=at(2026, 9, 1)),
                    "contract_violation")
            if role != "regulatory":
                self.assert_rejected(
                    lambda a=actor: self.sc.store.narrow_claim(
                        a, "claim:oil", ["CN-SH"], "r", event_id=f"n-{role}", occurred_at=at(2026, 9, 1)),
                    "contract_violation")


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.sc = Scenario()

    def test_claim_without_evidence_is_refused_with_reason(self):
        with self.assertRaises(ReleaseError) as ctx:
            self.sc.store.approve_claim(
                REG, "claim:nosugar", formula_ref=Scenario.REF2, text="低糖", kind="low_sugar",
                market_scope=["CN-SH"], evidence_set=[], event_id="x", occurred_at=at(2026, 8, 22))
        self.assertEqual("evidence_gate_failed", ctx.exception.code)
        self.assertIn("糖含量检测", ctx.exception.message)

    def test_weak_reduction_is_refused(self):
        s = self.sc.store
        s.accept_evidence(QA, "ev:weak", formula_ref=Scenario.REF2, kind="reduced_oil",
                          method_ref="GB", valid_until="2027-01-01T00:00:00+08:00",
                          value=10.0, baseline_value=12.0, event_id="ew", occurred_at=at(2026, 8, 20))
        with self.assertRaises(ReleaseError) as ctx:
            s.approve_claim(REG, "claim:weak", formula_ref=Scenario.REF2, text="减油",
                            kind="reduced_oil", market_scope=["CN-SH"], evidence_set=["ev:weak"],
                            event_id="cw", occurred_at=at(2026, 8, 22))
        self.assertEqual("evidence_gate_failed", ctx.exception.code)

    def test_evidence_cannot_cross_formula_versions(self):
        # v1 的风味证据不能拿去支持 v2 的健康声称（这里以低糖为例，证据绑定配方版本）。
        s = self.sc.store
        s.accept_evidence(QA, "ev:v1sugar", formula_ref=Scenario.REF1, kind="low_sugar",
                          method_ref="GB", valid_until="2027-01-01T00:00:00+08:00",
                          value=2.0, event_id="e1s", occurred_at=at(2026, 8, 20))
        with self.assertRaises(ReleaseError) as ctx:
            s.approve_claim(REG, "claim:x", formula_ref=Scenario.REF2, text="低糖",
                            kind="low_sugar", market_scope=["CN-SH"], evidence_set=["ev:v1sugar"],
                            event_id="cx", occurred_at=at(2026, 8, 22))
        self.assertIn("属于配方", ctx.exception.message)

    def test_narrow_only_contracts_scope(self):
        with self.assertRaises(ReleaseError) as ctx:
            self.sc.store.narrow_claim(REG, "claim:homo", ["CN-SH", "CN-BJ"], "扩张",
                                       event_id="n1", occurred_at=at(2026, 9, 1))
        self.assertEqual("scope_only_narrows", ctx.exception.code)


class LotTests(unittest.TestCase):
    def setUp(self):
        self.sc = Scenario()

    def test_old_formula_lot_cannot_carry_new_health_claim(self):
        old_pkgs = [{"ref": "P1", "claim_id": "claim:sugar", "text": "低糖莲蓉月饼",
                     "kind": "low_sugar", "regions": ["CN-SH"]}]
        with self.assertRaises(ReleaseError) as ctx:
            self.sc.lock("lot:old", 1, ["CN-SH"], old_pkgs)
        self.assertEqual("claim_version_mismatch", ctx.exception.code)

    def test_lock_pins_version_against_later_revision(self):
        self.sc.lock("lot:v2", 2, ["CN-SH", "CN-BJ"])
        s = self.sc.store
        s.version_formula(RD, Scenario.FID, "莲蓉月饼", "2026中秋",
                          ingredients=[{"ingredient": "又一改", "spec_id": "spec:莲蓉", "spec_version": 1}],
                          replaces=Scenario.REF2, event_id="f3", occurred_at=at(2026, 9, 15))
        # 已锁批次仍解析到 v2，文字照常可用，不被 v3 改写。
        words = s.allowed_words("lot:v2", "CN-SH", at(2026, 9, 20))
        self.assertEqual(3, len(words.allowed_texts))
        self.assertEqual(2, s.lots["lot:v2"]["formula_version"])

    def test_lock_rejects_unapproved_region(self):
        pkgs = [dict(p, regions=["CN-GZ"]) for p in self.sc.v2_packages() if p["ref"] == "P1"]
        with self.assertRaises(ReleaseError) as ctx:
            self.sc.lock("lot:gz", 2, ["CN-GZ"], pkgs)
        self.assertEqual("claim_region_not_approved", ctx.exception.code)

    def test_lock_rejects_when_evidence_expired_at_production(self):
        sc = Scenario()
        # 减油证据 11-30 到期；12 月生产的批次不能再锁该说法。
        with self.assertRaises(ReleaseError) as ctx:
            sc.lock("lot:dec", 2, ["CN-SH"], produced="2026-12-05T06:00:00+08:00",
                    when=at(2026, 12, 5))
        self.assertEqual("evidence_gate_failed", ctx.exception.code)
        self.assertIn("减油检测证据已过期", ctx.exception.message)

    def test_query_denials_explain_missing_evidence(self):
        self.sc.lock("lot:v2", 2, ["CN-SH", "CN-BJ"])
        s = self.sc.store
        s.narrow_claim(REG, "claim:oil", ["CN-SH"], "北京撤下", event_id="n1",
                       occurred_at=at(2026, 9, 22))
        words = s.allowed_words("lot:v2", "CN-BJ", at(2026, 9, 25))
        kinds = {d.ref: d for d in words.decisions}
        self.assertTrue(kinds["P1"].allowed)
        self.assertFalse(kinds["P2"].allowed)
        self.assertIn("已收窄", kinds["P2"].reasons[0])
        self.assertFalse(kinds["P3"].allowed)  # 同源从未在北京获批
        self.assertIn("CN-BJ", kinds["P3"].reasons[0])

    def test_version_conflict_and_idempotency(self):
        self.sc.lock("lot:v2", 2, ["CN-SH"])
        with self.assertRaises(ReleaseError) as ctx:
            self.sc.store.lock_lot(
                PLANT, "lot:v2", formula_id=Scenario.FID, formula_version=2, plant="上海一厂",
                produced_at=at(2026, 9, 10).isoformat(), sale_regions=["CN-SH"],
                packages=[p for p in self.sc.v2_packages(["CN-SH"]) if p["regions"]],
                quantity=1, event_id="lock-lot-v2-dup", occurred_at=at(2026, 9, 11))
        self.assertEqual("lot_already_locked", ctx.exception.code)
        # 直接构造跳号事件触发聚合版本冲突。
        stale = dict(self.sc.store.events[0])
        stale["event_id"] = "stale-version"
        stale["version"] = 99
        with self.assertRaises(ReleaseError) as ctx2:
            self.sc.store.append(stale)
        self.assertEqual("version_conflict", ctx2.exception.code)
        first = self.sc.store.events[0]
        self.assertIs(first, self.sc.store.append(first))  # 同 event_id 幂等返回


class CapacityTests(unittest.TestCase):
    def test_shared_pool_cannot_be_over_promised(self):
        ledger = CapacityLedger({"pool:print": 100.0})
        ledger.assert_reservable("pool:print", 60)
        ledger.apply({"event_type": "CAPACITY_RESERVED",
                      "payload": {"pool": "pool:print", "plan_id": "A", "quantity": 60}})
        ledger.assert_reservable("pool:print", 40)
        ledger.apply({"event_type": "CAPACITY_RESERVED",
                      "payload": {"pool": "pool:print", "plan_id": "B", "quantity": 40}})
        with self.assertRaises(CapacityError):
            ledger.assert_reservable("pool:print", 1)
        with self.assertRaises(CapacityError):
            ledger.assert_confirmed("pool:print", "A", 61)
        ledger.apply({"event_type": "CAPACITY_CONFIRMED",
                      "payload": {"pool": "pool:print", "plan_id": "A", "quantity": 60}})
        with self.assertRaises(CapacityError):  # 重复确认同一份额度
            ledger.assert_confirmed("pool:print", "A", 1)
        with self.assertRaises(CapacityError):  # B 未预留不能确认
            ledger.assert_confirmed("pool:print", "C", 10)

    def test_store_enforces_pool_at_append(self):
        sc = Scenario({"pool:small": 10.0})
        with self.assertRaises(ReleaseError) as ctx:
            sc.store.reserve_capacity(PLANT, "pool:small", "plan", 11, event_id="r1",
                                      occurred_at=at(2026, 9, 1))
        self.assertEqual("capacity_oversold", ctx.exception.code)


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.sc = Scenario()
        self.sc.lock("lot:A", 2, ["CN-SH", "CN-BJ"], when=at(2026, 9, 10))
        self.sc.lock("lot:B", 2, ["CN-SH", "CN-BJ"], when=at(2026, 9, 12))
        old = [{"ref": "PF", "claim_id": "claim:flavor", "text": "古法莲蓉风味",
                "kind": "flavor", "regions": ["CN-SH", "CN-BJ", "CN-GZ"]}]
        self.sc.lock("lot:C", 1, ["CN-SH", "CN-BJ", "CN-GZ"], old, when=at(2026, 9, 8))
        s = self.sc.store
        s.close_lot_sale(PLANT, "lot:A", "已售罄", event_id="ca", occurred_at=at(2026, 9, 20))
        s.narrow_claim(REG, "claim:oil", ["CN-SH"], "北京撤下", event_id="n1",
                       occurred_at=at(2026, 9, 22))

    def by_lot(self, entries):
        return {e["lot_id"]: e for e in entries}

    def test_narrowing_suspends_stock_and_notifies_sold(self):
        s = self.sc.store
        entries = s.impact_candidates(
            {"type": "claim_narrowed", "claim_id": "claim:oil", "removed_regions": ["CN-BJ"]},
            at(2026, 9, 25))
        hit = self.by_lot(entries)
        self.assertEqual({"lot:A", "lot:B"}, set(hit))
        self.assertEqual("notify", hit["lot:A"]["action"])
        self.assertEqual(["CN-BJ"], hit["lot:A"]["regions"])
        self.assertEqual("suspend_labels", hit["lot:B"]["action"])
        self.assertNotIn("lot:C", hit)  # 旧配方批次不含该声明

    def test_propagation_resumes_from_breakpoint(self):
        s = self.sc.store
        trigger = {"type": "claim_narrowed", "claim_id": "claim:oil", "removed_regions": ["CN-BJ"]}
        first = s.propagate_impact(QA, "run:1", trigger, at(2026, 9, 25),
                                   batch_size=1, max_batches=1)
        self.assertEqual(1, len(first))
        status = s.impact_status("run:1")
        self.assertEqual((1, 2), (status["processed"], status["total"]))
        self.assertFalse(status["finished"])
        rest = s.propagate_impact(QA, "run:1", trigger, at(2026, 9, 25),
                                  batch_size=1, max_batches=1)
        self.assertEqual(1, len(rest))
        self.assertEqual(1, rest[0]["payload"]["batch_index"])
        self.assertTrue(s.impact_status("run:1")["finished"])
        # 续跑后 B 批次北京减油位已被暂停。
        words = s.allowed_words("lot:B", "CN-BJ", at(2026, 9, 26))
        self.assertFalse(next(d for d in words.decisions if d.ref == "P2").allowed)
        self.assertEqual(["低糖莲蓉月饼"], list(words.allowed_texts))

    def test_batch_index_must_be_contiguous(self):
        s = self.sc.store
        s.propagate_impact(QA, "run:2", {"type": "claim_narrowed", "claim_id": "claim:oil",
                                         "removed_regions": ["CN-BJ"]},
                           at(2026, 9, 25), batch_size=1, max_batches=1)
        gap = dict(s.events[-1])
        gap["event_id"] = "forced-gap"
        gap["version"] = gap["version"] + 1
        gap["payload"] = dict(gap["payload"], batch_index=9)
        with self.assertRaises(ReleaseError) as ctx:
            s.append(gap)
        self.assertEqual("impact_batch_gap", ctx.exception.code)

    def test_expired_evidence_locates_lots(self):
        s = self.sc.store
        entries = s.impact_candidates({"type": "evidence_expired"}, at(2026, 12, 5))
        hit = self.by_lot(entries)
        self.assertEqual({"lot:A", "lot:B"}, set(hit))
        self.assertEqual(["P2"], [p["ref"] for p in hit["lot:B"]["packages"]])
        # 证据有效期间不受影响。
        self.assertEqual([], s.impact_candidates({"type": "evidence_expired"}, at(2026, 10, 1)))

    def test_ingredient_spec_change_pins_to_locked_version(self):
        s = self.sc.store
        s.change_ingredient_spec(RD, "spec:莲蓉", "新规格", 2, event_id="sp2",
                                 occurred_at=at(2026, 9, 24))
        hit = self.by_lot(s.impact_candidates(
            {"type": "ingredient_changed", "spec_id": "spec:莲蓉"}, at(2026, 9, 25)))
        # 三个批次配方都引用莲蓉 spec v1；规格升版后全部命中，动作按在库/已售区分。
        self.assertEqual({"lot:A", "lot:B", "lot:C"}, set(hit))
        self.assertEqual("notify", hit["lot:A"]["action"])
        self.assertEqual("suspend_labels", hit["lot:B"]["action"])
        # 再升一版但不产生重复暂停之外的副作用：C 的风味包装不列入健康包装。
        self.assertEqual(["PF"], [p["ref"] for p in hit["lot:C"]["packages"]])

    def test_recall_requires_registration_and_uses_pre_recall_state(self):
        s = self.sc.store
        with self.assertRaises(ReleaseError) as ctx:
            s.impact_candidates({"type": "recall", "lot_id": "lot:B"}, at(2026, 9, 25))
        self.assertEqual("lot_not_recalled", ctx.exception.code)
        s.recall_lot(REG, "lot:B", "异物风险", event_id="rc", occurred_at=at(2026, 9, 25))
        hit = self.by_lot(s.impact_candidates({"type": "recall", "lot_id": "lot:B"}, at(2026, 9, 25)))
        self.assertEqual("suspend_labels", hit["lot:B"]["action"])
        s.recall_lot(REG, "lot:A", "溯源关联", event_id="rca", occurred_at=at(2026, 9, 25))
        hit_a = self.by_lot(s.impact_candidates({"type": "recall", "lot_id": "lot:A"}, at(2026, 9, 25)))
        self.assertEqual("notify", hit_a["lot:A"]["action"])  # 召回前已售

    def test_replay_reconstructs_state_and_run(self):
        s = self.sc.store
        trigger = {"type": "claim_narrowed", "claim_id": "claim:oil", "removed_regions": ["CN-BJ"]}
        s.propagate_impact(QA, "run:9", trigger, at(2026, 9, 25), batch_size=1, max_batches=1)
        rebuilt = ReleaseStore.replay(load_schema(ROOT / "contracts/domain.schema.json"),
                                      list(s.events), {"pool:print": 1000.0, "pool:lotus": 500.0})
        status = rebuilt.impact_status("run:9")
        self.assertEqual(1, status["processed"])
        rebuilt.propagate_impact(QA, "run:9", trigger, at(2026, 9, 25))
        self.assertTrue(rebuilt.impact_status("run:9")["finished"])
        words = rebuilt.allowed_words("lot:B", "CN-SH", at(2026, 9, 26))
        self.assertEqual(3, len(words.allowed_texts))


class SampleStreamTests(unittest.TestCase):
    def test_committed_sample_stream_replays(self):
        events = json.loads((ROOT / "data/sample_stream.json").read_text(encoding="utf-8"))
        totals = json.loads((ROOT / "data/sample_pools.json").read_text(encoding="utf-8"))
        store = ReleaseStore.replay(load_schema(ROOT / "contracts/domain.schema.json"), events, totals)
        self.assertGreaterEqual(len(events), 24)
        words = store.allowed_words("lot:20260912-B", "CN-BJ", "2026-09-25")
        self.assertEqual(["低糖莲蓉月饼"], list(words.allowed_texts))
        status = store.ledger.status("pool:print:健康月饼盒-2026")
        self.assertEqual(100000.0, status.total)
        self.assertEqual(90000.0, status.reserved)


class ParseTests(unittest.TestCase):
    def test_bare_date_counts_as_end_of_day(self):
        parsed = parse_dt("2026-11-30")
        self.assertEqual(30, parsed.day)
        self.assertEqual(23, parsed.hour)
        self.assertTrue(parse_dt("2026-11-30T23:59:59+08:00").utcoffset() is not None)
        with self.assertRaises(ReleaseError):
            parse_dt("2026-11-30T10:00:00")  # 无时区仍拒绝


if __name__ == "__main__":
    unittest.main()
