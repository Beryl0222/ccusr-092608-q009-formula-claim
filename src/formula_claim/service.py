"""配方声明放行服务。

在契约事件之上实现：岗位职责分离、分级证据门槛、批次配方锁定、
声明收窄（暂停未售包装、圈定已售通知范围）与共享资源防重复承诺。
所有状态推进都会落成契约事件，事件版本按聚合从 1 开始递增。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from .contracts import validate_event
from .model import (
    CLAIM_TIER_REQUIREMENTS,
    EVIDENCE_KIND_LABELS,
    Actor,
    ClaimTier,
    DomainError,
    EvidenceRecord,
    FormulaRevision,
    IngredientSpec,
    LabelClaim,
    ProductionLot,
    Role,
    classify_claim_text,
)


@dataclass(frozen=True)
class ClaimPause:
    """声明收窄后，某批次未售包装在指定地区被暂停。"""

    lot_id: str
    claim_id: str
    regions: tuple[str, ...]


@dataclass(frozen=True)
class SaleNotification:
    """声明收窄后，已售批次需要纳入通知范围。"""

    lot_id: str
    claim_id: str
    regions: tuple[str, ...]
    sold_qty: int


@dataclass(frozen=True)
class NarrowingResult:
    claim_id: str
    remaining_scope: tuple[str, ...]
    paused: tuple[ClaimPause, ...]
    notifications: tuple[SaleNotification, ...]


@dataclass(frozen=True)
class DeniedClaim:
    claim_id: str
    text: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ClaimEvaluation:
    """给定日期与地区，一批产品允许与拒绝使用的文案。"""

    lot_id: str
    region: str
    on_date: str
    allowed: tuple[str, ...]
    denied: tuple[DeniedClaim, ...]


@dataclass
class ResourcePool:
    """某工厂由多个计划共享的印刷额度与合格原料。"""

    print_quota: int
    ingredients: dict[str, int]


@dataclass
class PlanReservation:
    plan_id: str
    factory: str
    print_units: int
    ingredients: dict[str, int]
    status: str = "reserved"  # reserved / committed / released


def _aware_iso(value: datetime, field_name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainError("timezone_required", f"{field_name} 必须携带时区")
    return value.isoformat()


class ReleaseService:
    """配方声明放行库的应用服务。"""

    def __init__(self, schema: Mapping[str, object]) -> None:
        self._schema = schema
        self._events: list[dict] = []
        self._seq = 0
        self._versions: dict[tuple[str, str], int] = {}
        self._formulas: dict[str, list[FormulaRevision]] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._claims: dict[str, LabelClaim] = {}
        self._lots: dict[str, ProductionLot] = {}
        self._pools: dict[str, ResourcePool] = {}
        self._plans: dict[str, PlanReservation] = {}

    # ------------------------------------------------------------------
    # 只读访问
    # ------------------------------------------------------------------
    @property
    def events(self) -> tuple[dict, ...]:
        return tuple(self._events)

    def revision(self, formula_id: str, version: int | None = None) -> FormulaRevision:
        revisions = self._formulas.get(formula_id)
        if not revisions:
            raise DomainError("not_found", f"配方不存在：{formula_id}")
        if version is None:
            return revisions[-1]
        for rev in revisions:
            if rev.version == version:
                return rev
        raise DomainError("not_found", f"配方版本不存在：{formula_id}@{version}")

    def evidence(self, evidence_id: str) -> EvidenceRecord:
        record = self._evidence.get(evidence_id)
        if record is None:
            raise DomainError("not_found", f"证据不存在：{evidence_id}")
        return record

    def claim(self, claim_id: str) -> LabelClaim:
        claim = self._claims.get(claim_id)
        if claim is None:
            raise DomainError("not_found", f"声明不存在：{claim_id}")
        return claim

    def lot(self, lot_id: str) -> ProductionLot:
        lot = self._lots.get(lot_id)
        if lot is None:
            raise DomainError("not_found", f"批次不存在：{lot_id}")
        return lot

    def plan(self, plan_id: str) -> PlanReservation:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise DomainError("not_found", f"计划不存在：{plan_id}")
        return plan

    # ------------------------------------------------------------------
    # 配方与证据
    # ------------------------------------------------------------------
    def propose_formula(
        self,
        actor: Actor,
        *,
        formula_id: str,
        factory: str,
        window_start: datetime,
        window_end: datetime,
        ingredients: Mapping[str, IngredientSpec],
        allergens: tuple[str, ...] = (),
        homology_basis: tuple[str, ...] = (),
        substitutions: Mapping[str, str] | None = None,
        occurred_at: datetime,
    ) -> FormulaRevision:
        """研发提出配方新版本；已锁定批次保持原版本不被改写。"""
        self._require_role(actor, Role.RND)
        start_iso = _aware_iso(window_start, "window_start")
        end_iso = _aware_iso(window_end, "window_end")
        if window_end <= window_start:
            raise DomainError("invalid_window", "生产窗口结束时间必须晚于开始时间")
        version = len(self._formulas.get(formula_id, [])) + 1
        revision = FormulaRevision(
            formula_id=formula_id,
            version=version,
            factory=factory,
            window_start=window_start,
            window_end=window_end,
            ingredients=dict(ingredients),
            allergens=tuple(allergens),
            homology_basis=tuple(homology_basis),
            substitutions=dict(substitutions or {}),
            proposed_by=actor.actor_id,
            proposed_at=occurred_at,
        )
        self._formulas.setdefault(formula_id, []).append(revision)
        self._emit(
            "FORMULA_VERSIONED",
            "formula_revision",
            formula_id,
            {
                "factory": factory,
                "formula_version": version,
                "window_start": start_iso,
                "window_end": end_iso,
                "ingredients": [
                    {"ingredient_id": spec.ingredient_id, "specification": spec.specification, "quantity": spec.quantity}
                    for spec in sorted(revision.ingredients.values(), key=lambda s: s.ingredient_id)
                ],
                "allergens": sorted(revision.allergens),
                "homology_basis": sorted(revision.homology_basis),
                "substitutions": dict(sorted(revision.substitutions.items())),
                "proposed_by": actor.actor_id,
            },
            occurred_at,
        )
        return revision

    def confirm_evidence(
        self,
        actor: Actor,
        *,
        evidence_id: str,
        formula_id: str,
        kind: str,
        method_ref: str,
        valid_until: datetime,
        occurred_at: datetime,
    ) -> EvidenceRecord:
        """质量人员确认检测或试制证据；确认人不得是配方提出人。"""
        self._require_role(actor, Role.QUALITY)
        revision = self.revision(formula_id)
        if actor.actor_id == revision.proposed_by:
            raise DomainError("sod_conflict", "检测确认人与配方提出人不得为同一人")
        if evidence_id in self._evidence:
            raise DomainError("conflict", f"证据标识已存在：{evidence_id}")
        valid_iso = _aware_iso(valid_until, "valid_until")
        record = EvidenceRecord(
            evidence_id=evidence_id,
            formula_id=formula_id,
            kind=kind,
            method_ref=method_ref,
            valid_until=valid_until,
            confirmed_by=actor.actor_id,
        )
        self._evidence[evidence_id] = record
        self._emit(
            "EVIDENCE_ACCEPTED",
            "evidence_record",
            evidence_id,
            {
                "formula_id": formula_id,
                "kind": kind,
                "method_ref": method_ref,
                "valid_until": valid_iso,
                "confirmed_by": actor.actor_id,
            },
            occurred_at,
        )
        return record

    # ------------------------------------------------------------------
    # 声明放行与收窄
    # ------------------------------------------------------------------
    def approve_claim(
        self,
        actor: Actor,
        *,
        claim_id: str,
        formula_id: str,
        text: str,
        tier: ClaimTier | None = None,
        market_scope: set[str] | frozenset[str],
        evidence_set: tuple[str, ...] | list[str] = (),
        occurred_at: datetime,
    ) -> LabelClaim:
        """法规人员按证据门槛放行文案；放行人不得与提出人、确认人重复。"""
        self._require_role(actor, Role.REGULATORY)
        if claim_id in self._claims:
            raise DomainError("conflict", f"声明标识已存在：{claim_id}")
        revision = self.revision(formula_id)
        if not market_scope:
            raise DomainError("invalid_scope", "放行地区不能为空")
        if actor.actor_id == revision.proposed_by:
            raise DomainError("sod_conflict", "法规放行人与配方提出人不得为同一人")
        tier = tier or classify_claim_text(text)
        required = CLAIM_TIER_REQUIREMENTS[tier]
        details: list[str] = []
        available_kinds: set[str] = set()
        for evidence_id in evidence_set:
            record = self._evidence.get(evidence_id)
            if record is None:
                raise DomainError("not_found", f"证据不存在：{evidence_id}")
            if record.formula_id != formula_id:
                details.append(f"证据与配方不匹配：{evidence_id}")
                continue
            if record.confirmed_by == actor.actor_id:
                raise DomainError("sod_conflict", "法规放行人与检测确认人不得为同一人")
            if record.status != "accepted":
                details.append(f"证据未处于已接受状态：{evidence_id}")
            elif record.valid_until < occurred_at:
                details.append(f"证据已过期：{evidence_id}")
            else:
                available_kinds.add(record.kind)
        for kind in sorted(required - available_kinds):
            details.append(f"缺少证据：{EVIDENCE_KIND_LABELS.get(kind, kind)}")
        if details:
            raise DomainError("threshold_not_met", "声明证据门槛未满足", tuple(details))
        claim = LabelClaim(
            claim_id=claim_id,
            formula_id=formula_id,
            text=text,
            tier=tier,
            market_scope=frozenset(market_scope),
            evidence_set=tuple(evidence_set),
            approved_by=actor.actor_id,
            approved_at=occurred_at,
            formula_version=revision.version,
        )
        self._claims[claim_id] = claim
        self._emit(
            "CLAIM_APPROVED",
            "label_claim",
            claim_id,
            {
                "formula_id": formula_id,
                "text": text,
                "tier": tier.value,
                "market_scope": sorted(claim.market_scope),
                "evidence_set": list(claim.evidence_set),
                "approved_by": actor.actor_id,
                "formula_version": revision.version,
                "claim_version": claim.version,
            },
            occurred_at,
        )
        return claim

    def narrow_claim(
        self,
        actor: Actor,
        *,
        claim_id: str,
        removed_regions: set[str] | frozenset[str],
        occurred_at: datetime,
    ) -> NarrowingResult:
        """收窄声明地区：只暂停未售包装，已售批次生成通知范围。"""
        self._require_role(actor, Role.REGULATORY)
        claim = self.claim(claim_id)
        removed = frozenset(removed_regions)
        if not removed:
            raise DomainError("invalid_scope", "收窄地区不能为空")
        if not removed <= claim.market_scope:
            raise DomainError("invalid_scope", "收窄地区超出原放行范围")
        claim.market_scope -= removed
        claim.version += 1
        self._emit(
            "CLAIM_APPROVED",
            "label_claim",
            claim_id,
            {
                "formula_id": claim.formula_id,
                "text": claim.text,
                "tier": claim.tier.value,
                "market_scope": sorted(claim.market_scope),
                "evidence_set": list(claim.evidence_set),
                "approved_by": actor.actor_id,
                "formula_version": claim.formula_version,
                "claim_version": claim.version,
            },
            occurred_at,
        )
        paused: list[ClaimPause] = []
        notifications: list[SaleNotification] = []
        for lot in self._lots_of(claim.formula_id):
            hit = sorted(lot.regions & removed)
            if not hit:
                continue
            pause, notification = self._pause_for_lot(lot, claim.claim_id, tuple(hit))
            if pause is not None:
                paused.append(pause)
            if notification is not None:
                notifications.append(notification)
            self._emit(
                "IMPACT_PROPAGATED",
                "production_lot",
                lot.lot_id,
                {
                    "reason": "声明收窄",
                    "claim_id": claim.claim_id,
                    "regions": hit,
                    "packaging_paused": pause is not None,
                    "sale_notified": notification is not None,
                },
                occurred_at,
            )
        return NarrowingResult(
            claim_id=claim_id,
            remaining_scope=tuple(sorted(claim.market_scope)),
            paused=tuple(paused),
            notifications=tuple(notifications),
        )

    # ------------------------------------------------------------------
    # 生产批次
    # ------------------------------------------------------------------
    def lock_lot(
        self,
        actor: Actor,
        *,
        lot_id: str,
        formula_id: str,
        formula_version: int,
        produced_at: datetime,
        regions: set[str] | frozenset[str],
        inventory_batches: tuple[str, ...] = (),
        quantity: int,
        occurred_at: datetime,
    ) -> ProductionLot:
        """按配方版本锁定生产批次；生产时间须落在该版本工厂窗口内。"""
        self._require_role(actor, Role.PLANNER)
        if lot_id in self._lots:
            raise DomainError("conflict", f"批次标识已存在：{lot_id}")
        revision = self.revision(formula_id, formula_version)
        _aware_iso(produced_at, "produced_at")
        if not (revision.window_start <= produced_at <= revision.window_end):
            raise DomainError(
                "window_mismatch",
                f"生产时间不在配方版本窗口内：{formula_id}@{formula_version}",
            )
        if quantity <= 0:
            raise DomainError("invalid_quantity", "批次数量必须为正数")
        if not regions:
            raise DomainError("invalid_scope", "销售区域不能为空")
        lot = ProductionLot(
            lot_id=lot_id,
            formula_id=formula_id,
            formula_version=formula_version,
            factory=revision.factory,
            produced_at=produced_at,
            regions=frozenset(regions),
            inventory_batches=tuple(inventory_batches),
            quantity=quantity,
            unsold_qty=quantity,
        )
        self._lots[lot_id] = lot
        self._emit(
            "LOT_LOCKED",
            "production_lot",
            lot_id,
            {
                "formula_id": formula_id,
                "formula_version": formula_version,
                "factory": revision.factory,
                "produced_at": produced_at.isoformat(),
                "regions": sorted(lot.regions),
                "inventory_batches": list(lot.inventory_batches),
                "quantity": quantity,
            },
            occurred_at,
        )
        return lot

    def record_sale(self, *, lot_id: str, qty: int) -> ProductionLot:
        """同步外部销售事实，用于区分未售包装与已售批次。"""
        lot = self.lot(lot_id)
        if qty <= 0 or qty > lot.unsold_qty:
            raise DomainError("invalid_quantity", f"销售数量超出未售库存：{lot_id}")
        lot.sold_qty += qty
        lot.unsold_qty -= qty
        return lot

    # ------------------------------------------------------------------
    # 共享资源：印刷额度与合格原料
    # ------------------------------------------------------------------
    def set_resource_pool(self, *, factory: str, print_quota: int, ingredients: Mapping[str, int]) -> None:
        self._pools[factory] = ResourcePool(print_quota=print_quota, ingredients=dict(ingredients))

    def reserve_plan(
        self,
        actor: Actor,
        *,
        plan_id: str,
        factory: str,
        print_units: int,
        ingredients: Mapping[str, int],
    ) -> PlanReservation:
        self._require_role(actor, Role.PLANNER)
        if plan_id in self._plans:
            raise DomainError("conflict", f"计划标识已存在：{plan_id}")
        if print_units < 0 or any(qty < 0 for qty in ingredients.values()):
            raise DomainError("invalid_quantity", "计划用量不得为负数")
        plan = PlanReservation(
            plan_id=plan_id,
            factory=factory,
            print_units=print_units,
            ingredients=dict(ingredients),
        )
        self._plans[plan_id] = plan
        return plan

    def confirm_plan(self, actor: Actor, *, plan_id: str) -> PlanReservation:
        """确认计划占用共享资源；幂等，且不得与他人已确认量重复承诺。"""
        self._require_role(actor, Role.PLANNER)
        plan = self.plan(plan_id)
        if plan.status == "committed":
            return plan
        if plan.status == "released":
            raise DomainError("state", f"计划已释放，不能再次确认：{plan_id}")
        pool = self._pools.get(plan.factory)
        if pool is None:
            raise DomainError("not_found", f"共享资源池未配置：{plan.factory}")
        committed = [
            other
            for other in self._plans.values()
            if other.status == "committed" and other.factory == plan.factory and other.plan_id != plan_id
        ]
        details: list[str] = []
        available_print = pool.print_quota - sum(other.print_units for other in committed)
        if plan.print_units > available_print:
            details.append(f"印刷额度不足：需要{plan.print_units}，可用{available_print}")
        for ingredient, qty in sorted(plan.ingredients.items()):
            used = sum(other.ingredients.get(ingredient, 0) for other in committed)
            available = pool.ingredients.get(ingredient, 0) - used
            if qty > available:
                details.append(f"合格原料{ingredient}不足：需要{qty}，可用{available}")
        if details:
            raise DomainError("resource_shortage", "共享资源不足，无法确认计划", tuple(details))
        plan.status = "committed"
        return plan

    def release_plan(self, actor: Actor, *, plan_id: str) -> PlanReservation:
        self._require_role(actor, Role.PLANNER)
        plan = self.plan(plan_id)
        plan.status = "released"
        return plan

    # ------------------------------------------------------------------
    # 文案可用性评估
    # ------------------------------------------------------------------
    def evaluate_lot_claims(self, *, lot_id: str, on_date: datetime, region: str) -> ClaimEvaluation:
        """判断一批产品在给定日期和地区允许使用的文字，拒绝时说明缺少的证据。"""
        lot = self.lot(lot_id)
        date_iso = _aware_iso(on_date, "on_date")
        allowed: list[str] = []
        denied: list[DeniedClaim] = []
        for claim in self._claims_of(lot.formula_id):
            reasons: list[str] = []
            if region not in claim.market_scope:
                reasons.append("地区不在放行范围内")
            if region in lot.packaging_holds.get(claim.claim_id, set()):
                reasons.append("该声明未售包装已暂停")
            if lot.under_recall:
                reasons.append("批次处于召回状态")
            reasons.extend(sorted(lot.hold_reasons))
            if claim.approved_at > lot.produced_at:
                reasons.append("声明批准时间晚于批次生产时间")
            if claim.formula_version != lot.formula_version:
                reasons.append("声明对应配方版本与批次锁定版本不一致")
            valid_kinds: set[str] = set()
            for evidence_id in claim.evidence_set:
                record = self._evidence.get(evidence_id)
                if record is None:
                    reasons.append(f"证据未登记：{evidence_id}")
                elif record.status != "accepted":
                    reasons.append(f"证据已失效：{evidence_id}")
                elif record.valid_until < on_date:
                    reasons.append(f"证据已过期：{evidence_id}")
                else:
                    valid_kinds.add(record.kind)
            required = CLAIM_TIER_REQUIREMENTS[claim.tier]
            for kind in sorted(required - valid_kinds):
                reasons.append(f"缺少证据：{EVIDENCE_KIND_LABELS.get(kind, kind)}")
            if reasons:
                denied.append(DeniedClaim(claim_id=claim.claim_id, text=claim.text, reasons=tuple(reasons)))
            else:
                allowed.append(claim.text)
        return ClaimEvaluation(
            lot_id=lot_id,
            region=region,
            on_date=date_iso,
            allowed=tuple(allowed),
            denied=tuple(denied),
        )

    # ------------------------------------------------------------------
    # 影响定位与落点（供后台传播调用）
    # ------------------------------------------------------------------
    def find_lots_using_evidence(self, evidence_id: str) -> list[str]:
        """精确定位依赖某证据的声明所覆盖的批次。"""
        record = self.evidence(evidence_id)
        affected: set[str] = set()
        for claim in self._claims_of(record.formula_id):
            if evidence_id not in claim.evidence_set:
                continue
            for lot in self._lots_of(record.formula_id):
                if lot.regions & claim.market_scope:
                    affected.add(lot.lot_id)
        return sorted(affected)

    def find_lots_using_ingredient(self, ingredient_id: str) -> list[str]:
        """按批次各自锁定的配方版本精确定位含某原料的批次。"""
        affected = [
            lot.lot_id
            for lot in self._lots.values()
            if ingredient_id in self.revision(lot.formula_id, lot.formula_version).ingredients
        ]
        return sorted(affected)

    def mark_evidence_expired(self, *, evidence_id: str, occurred_at: datetime, job_id: str) -> None:
        record = self.evidence(evidence_id)
        record.status = "expired"
        self._emit(
            "IMPACT_PROPAGATED",
            "evidence_record",
            evidence_id,
            {"job_id": job_id, "reason": "检测过期", "formula_id": record.formula_id},
            occurred_at,
        )

    def apply_evidence_expiry(
        self,
        *,
        lot_id: str,
        evidence_id: str,
        occurred_at: datetime,
        job_id: str,
    ) -> tuple[tuple[ClaimPause, ...], tuple[SaleNotification, ...]]:
        """对单个批次落实检测过期影响：暂停未售包装、圈定已售通知。"""
        lot = self.lot(lot_id)
        record = self.evidence(evidence_id)
        paused: list[ClaimPause] = []
        notifications: list[SaleNotification] = []
        for claim in self._claims_of(lot.formula_id):
            if evidence_id not in claim.evidence_set:
                continue
            hit = tuple(sorted(lot.regions & claim.market_scope))
            if not hit:
                continue
            pause, notification = self._pause_for_lot(lot, claim.claim_id, hit)
            if pause is not None:
                paused.append(pause)
            if notification is not None:
                notifications.append(notification)
        self._emit(
            "IMPACT_PROPAGATED",
            "production_lot",
            lot_id,
            {
                "job_id": job_id,
                "reason": "检测过期",
                "evidence_id": evidence_id,
                "paused_claims": sorted({pause.claim_id for pause in paused}),
                "notified_claims": sorted({note.claim_id for note in notifications}),
            },
            occurred_at,
        )
        return tuple(paused), tuple(notifications)

    def apply_ingredient_change(self, *, lot_id: str, ingredient_id: str, occurred_at: datetime, job_id: str) -> None:
        lot = self.lot(lot_id)
        lot.hold_reasons.add(f"原料变更待评估：{ingredient_id}")
        self._emit(
            "IMPACT_PROPAGATED",
            "production_lot",
            lot_id,
            {"job_id": job_id, "reason": "原料变更", "ingredient_id": ingredient_id},
            occurred_at,
        )

    def apply_recall(self, *, lot_id: str, reason: str, occurred_at: datetime, job_id: str) -> None:
        lot = self.lot(lot_id)
        lot.under_recall = True
        self._emit(
            "IMPACT_PROPAGATED",
            "production_lot",
            lot_id,
            {"job_id": job_id, "reason": f"召回：{reason}"},
            occurred_at,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _require_role(self, actor: Actor, *roles: Role) -> None:
        if actor.role not in roles:
            labels = "、".join(role.value for role in roles)
            raise DomainError("permission_denied", f"仅{labels}岗位可执行该操作")

    def _claims_of(self, formula_id: str) -> list[LabelClaim]:
        return sorted(
            (claim for claim in self._claims.values() if claim.formula_id == formula_id),
            key=lambda claim: claim.claim_id,
        )

    def _lots_of(self, formula_id: str) -> list[ProductionLot]:
        return sorted(
            (lot for lot in self._lots.values() if lot.formula_id == formula_id),
            key=lambda lot: lot.lot_id,
        )

    def _pause_for_lot(
        self,
        lot: ProductionLot,
        claim_id: str,
        regions: tuple[str, ...],
    ) -> tuple[ClaimPause | None, SaleNotification | None]:
        pause = None
        notification = None
        if lot.unsold_qty > 0:
            lot.packaging_holds.setdefault(claim_id, set()).update(regions)
            pause = ClaimPause(lot_id=lot.lot_id, claim_id=claim_id, regions=regions)
        if lot.sold_qty > 0:
            notification = SaleNotification(
                lot_id=lot.lot_id,
                claim_id=claim_id,
                regions=regions,
                sold_qty=lot.sold_qty,
            )
        return pause, notification

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        occurred_at: datetime,
    ) -> dict:
        key = (aggregate_type, aggregate_id)
        version = self._versions.get(key, 0) + 1
        self._versions[key] = version
        self._seq += 1
        event = {
            "event_id": f"evt-{self._seq:06d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": _aware_iso(occurred_at, "occurred_at"),
            "version": version,
            "payload": payload,
        }
        issues = validate_event(event, self._schema)
        if issues:
            raise DomainError(
                "contract_violation",
                "事件未通过契约校验",
                tuple(f"{issue.field}:{issue.code}" for issue in issues),
            )
        self._events.append(event)
        return event
