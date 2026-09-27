"""放行库领域对象、岗位角色与声明证据门槛策略。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class DomainError(Exception):
    """领域规则拒绝：携带稳定代码、中文说明与明细行。"""

    def __init__(self, code: str, message: str, details: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class Role(enum.Enum):
    """放行链路上的岗位。"""

    RND = "研发"
    QUALITY = "质量"
    REGULATORY = "法规"
    PLANNER = "计划"
    SYSTEM = "系统"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: Role


class ClaimTier(enum.Enum):
    """声明等级，决定放行所需的证据门槛。"""

    NUTRITION = "nutrition"
    HOMOLOGY = "homology"
    FLAVOR = "flavor"


EVIDENCE_NUTRITION_TEST = "nutrition_test"
EVIDENCE_TRIAL_RESULT = "trial_result"
EVIDENCE_HOMOLOGY_BASIS = "homology_basis"

EVIDENCE_KIND_LABELS: dict[str, str] = {
    EVIDENCE_NUTRITION_TEST: "营养检测",
    EVIDENCE_TRIAL_RESULT: "试制结果",
    EVIDENCE_HOMOLOGY_BASIS: "药食同源依据",
}

CLAIM_TIER_REQUIREMENTS: dict[ClaimTier, frozenset[str]] = {
    ClaimTier.NUTRITION: frozenset({EVIDENCE_NUTRITION_TEST, EVIDENCE_TRIAL_RESULT}),
    ClaimTier.HOMOLOGY: frozenset({EVIDENCE_HOMOLOGY_BASIS}),
    ClaimTier.FLAVOR: frozenset(),
}

_NUTRITION_KEYWORDS = ("低糖", "减糖", "无糖", "减油", "低油", "低脂")
_HOMOLOGY_KEYWORDS = ("药食同源", "食养")


def classify_claim_text(text: str) -> ClaimTier:
    """按文案关键词推断声明等级；放行时也可显式指定覆盖。"""
    if any(keyword in text for keyword in _NUTRITION_KEYWORDS):
        return ClaimTier.NUTRITION
    if any(keyword in text for keyword in _HOMOLOGY_KEYWORDS):
        return ClaimTier.HOMOLOGY
    return ClaimTier.FLAVOR


@dataclass(frozen=True)
class IngredientSpec:
    """原料规格与单批用量。"""

    ingredient_id: str
    specification: str
    quantity: float


@dataclass
class FormulaRevision:
    """按工厂与生产窗口保存的配方版本。"""

    formula_id: str
    version: int
    factory: str
    window_start: datetime
    window_end: datetime
    ingredients: dict[str, IngredientSpec]
    allergens: tuple[str, ...]
    homology_basis: tuple[str, ...]
    substitutions: dict[str, str]
    proposed_by: str
    proposed_at: datetime


@dataclass
class EvidenceRecord:
    """质量人员确认的检测或试制证据。"""

    evidence_id: str
    formula_id: str
    kind: str
    method_ref: str
    valid_until: datetime
    confirmed_by: str
    status: str = "accepted"  # accepted / expired / revoked


@dataclass
class LabelClaim:
    """法规人员放行的标签文案及其适用范围。"""

    claim_id: str
    formula_id: str
    text: str
    tier: ClaimTier
    market_scope: frozenset[str]
    evidence_set: tuple[str, ...]
    approved_by: str
    approved_at: datetime
    formula_version: int
    version: int = 1


@dataclass
class ProductionLot:
    """锁定配方版本的生产批次；锁定后不得被后续配方修订改写。"""

    lot_id: str
    formula_id: str
    formula_version: int
    factory: str
    produced_at: datetime
    regions: frozenset[str]
    inventory_batches: tuple[str, ...]
    quantity: int
    sold_qty: int = 0
    unsold_qty: int = 0
    packaging_holds: dict[str, set[str]] = field(default_factory=dict)  # claim_id -> 暂停地区
    hold_reasons: set[str] = field(default_factory=set)
    under_recall: bool = False
