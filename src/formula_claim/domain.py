"""配方声明放行库：事件溯源的放行领域服务。

在 ``contracts`` 的基础事件之上承载业务规则：

- 角色分离：研发提案、质量确认证据、法规批准/收窄声明、工厂锁批次，
  契约层与本层双重强制，任何角色都不能独自完成全链放行；
- 证据门槛：低糖（GB 28050 固体 ≤5g/100g）、减油（比较声称 ≥25%）、
  药食同源（需目录依据文件）、一般风味（无证据门槛）四类分别评估；
- 批次锁版：``LOT_LOCKED`` 固化配方版本与包装位，后续配方修订不回写；
- 声明收窄：只暂停尚未销售（未关闭销售）的包装位，已售批次生成通知范围；
- 影响传播：检测过期、原料规格变更、收窄、召回都可定位受影响批次，
  候选集合在首次运行时快照，运行可从中断批次精确续跑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .capacity import CapacityLedger, CapacityError
from .contracts import validate_event

# GB 28050《预包装食品营养标签通则》：固体食品“低糖”声称门槛。
LOW_SUGAR_MAX_G_PER_100G = 5.0
# GB 28050 比较声称：脂肪含量与参考食品相比差异须 ≥25%。
REDUCED_OIL_MIN_PERCENT = 25.0

CLAIM_KINDS = ("low_sugar", "reduced_oil", "homologous", "flavor")
ROLES = ("rd", "quality", "regulatory", "factory")


class ReleaseError(ValueError):
    """业务规则拒绝。"""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def parse_dt(value: Any) -> datetime:
    """解析带时区的日期或时间；裸日期按当天结束计，保证证据当天仍有效。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return datetime.combine(value, time.max, tzinfo=timezone.utc)
    else:
        text = str(value)
        try:
            if len(text) == 10:
                return datetime.combine(date.fromisoformat(text), time.max, tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReleaseError("bad_datetime", f"无法解析时间: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseError("timezone_required", f"时间必须携带时区: {value}")
    return parsed


@dataclass(frozen=True)
class WordDecision:
    ref: str
    text: str
    kind: str
    allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AllowedWords:
    lot_id: str
    region: str
    servable: bool
    decisions: tuple[WordDecision, ...]

    @property
    def allowed_texts(self) -> tuple[str, ...]:
        return tuple(d.text for d in self.decisions if d.allowed)

    @property
    def denials(self) -> tuple[WordDecision, ...]:
        return tuple(d for d in self.decisions if not d.allowed)


def _formula_ref(payload: dict) -> tuple[str, int]:
    ref = payload["formula_ref"]
    return ref["formula_id"], int(ref["version"])


def evaluate_gate(kind: str, evidences: Iterable[dict], as_of: datetime) -> list[str]:
    """按声明种类评估证据门槛，返回中文缺失原因（空列表表示通过）。"""
    if kind == "flavor":
        return []
    records = list(evidences)
    valid = [e for e in records if parse_dt(e["valid_until"]) >= as_of]
    expired = [e for e in records if parse_dt(e["valid_until"]) < as_of]
    if kind == "low_sugar":
        if not records:
            return ["缺少经质量确认的糖含量检测证据（方法须可溯源，如 GB 5009.8）"]
        if not valid:
            until = min(parse_dt(e["valid_until"]).date().isoformat() for e in expired)
            return [f"低糖检测证据已过期（最近有效期至 {until}），需重新检测"]
        passing = [e for e in valid if float(e.get("value", 0)) <= LOW_SUGAR_MAX_G_PER_100G]
        if not passing:
            worst = min(float(e["value"]) for e in valid)
            return [f"糖含量 {worst:g}g/100g 超过低糖门槛 {LOW_SUGAR_MAX_G_PER_100G:g}g/100g"]
        return []
    if kind == "reduced_oil":
        if not records:
            return ["缺少脂肪检测及经典配方基准值，无法支撑减油比较声称"]
        if not valid:
            until = min(parse_dt(e["valid_until"]).date().isoformat() for e in expired)
            return [f"减油检测证据已过期（最近有效期至 {until}），需重新检测"]
        best = -1.0
        detail = ""
        for e in valid:
            value = float(e.get("value"))
            baseline = float(e.get("baseline_value"))
            if baseline <= 0:
                continue
            pct = (baseline - value) / baseline * 100.0
            if pct > best:
                best, detail = pct, f"{value:g} 对比基准 {baseline:g}g/100g，仅降低 {pct:.1f}%"
        if best < 0:
            return ["减油证据缺少有效的基准脂肪含量（baseline_value）"]
        if best < REDUCED_OIL_MIN_PERCENT:
            return [f"油脂降幅不足：{detail}，比较声称须 ≥{REDUCED_OIL_MIN_PERCENT:g}%"]
        return []
    if kind == "homologous":
        if not records:
            return ["缺少药食同源依据文件（卫健委公布的药食同源名单或备案依据）"]
        if not valid:
            return ["药食同源依据证据已过期，需质量重新确认"]
        if not any(str(e.get("basis_doc") or "").strip() for e in valid):
            return ["药食同源证据未登记依据文件（basis_doc）"]
        return []
    return [f"未知声明种类: {kind}"]


class ReleaseStore:
    """事件溯源存储：追加事件时校验契约与领域规则，并维护投影。"""

    def __init__(self, schema: dict, capacity_totals: dict[str, float] | None = None) -> None:
        self.schema = schema
        self.ledger = CapacityLedger(dict(capacity_totals or {}))
        self.events: list[dict] = []
        self._event_ids: set[str] = set()
        self._agg_versions: dict[str, int] = {}
        # 投影
        self.formulas: dict[str, dict] = {}
        self.specs: dict[str, dict] = {}
        self.evidences: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.lots: dict[str, dict] = {}
        self.runs: dict[str, list[dict]] = {}

    # ---- 重放 ----
    @classmethod
    def replay(cls, schema: dict, events: Iterable[dict], capacity_totals: dict[str, float] | None = None) -> "ReleaseStore":
        store = cls(schema, capacity_totals)
        for event in events:
            store.events.append(event)
            store._project(event)
        return store

    # ---- 追加与校验 ----
    def append(self, event: dict) -> dict:
        issues = validate_event(event, self.schema)
        if issues:
            text = "; ".join(f"{i.field}:{i.code}" for i in issues)
            raise ReleaseError("contract_violation", f"事件违反契约: {text}")
        if event["event_id"] in self._event_ids:  # 业务幂等
            return next(e for e in self.events if e["event_id"] == event["event_id"])
        self._check_domain(event)
        self.events.append(event)
        self._event_ids.add(event["event_id"])
        self._project(event)
        return event

    def _next_version(self, aggregate_id: str) -> int:
        return self._agg_versions.get(aggregate_id, 0) + 1

    def _event(self, event_type: str, aggregate_type: str, aggregate_id: str,
               actor: dict, payload: dict, *, event_id: str | None, occurred_at: datetime) -> dict:
        return {
            "event_id": event_id or f"{aggregate_id}-{self._next_version(aggregate_id)}-{uuid4().hex[:8]}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at.isoformat(),
            "version": self._next_version(aggregate_id),
            "actor": dict(actor),
            "payload": payload,
        }

    def _check_domain(self, event: dict) -> None:
        agg_id = event["aggregate_id"]
        expected_version = self._agg_versions.get(agg_id, 0) + 1
        if event["version"] != expected_version:
            raise ReleaseError(
                "version_conflict",
                f"聚合 {agg_id} 版本须为 {expected_version}，收到 {event['version']}",
            )
        as_of = parse_dt(event["occurred_at"])
        etype, body = event["event_type"], event["payload"]
        if etype == "FORMULA_VERSIONED":
            replaces = body.get("replaces")
            if replaces and (replaces["formula_id"], int(replaces["version"])) not in {
                (fid, v) for fid, f in self.formulas.items() for v in f["versions"]
            }:
                raise ReleaseError("unknown_replaces", f"被替代配方版本不存在: {replaces}")
            for item in body.get("ingredients", []):
                self._require_ingredient_shape(item)
        elif etype == "INGREDIENT_SPEC_CHANGED":
            prev = self.specs.get(agg_id)
            if prev and int(body["spec_version"]) <= prev["version"]:
                raise ReleaseError("spec_version_must_advance", "原料规格版本必须递增")
        elif etype == "EVIDENCE_ACCEPTED":
            fid, fv = _formula_ref(body)
            self._require_formula_version(fid, fv)
            if body["kind"] not in CLAIM_KINDS:
                raise ReleaseError("unknown_claim_kind", f"证据的声明种类未登记: {body['kind']}")
            parse_dt(body["valid_until"])
            if body["kind"] == "reduced_oil" and ("value" not in body or "baseline_value" not in body):
                raise ReleaseError("baseline_required", "减油证据必须包含 value 与 baseline_value")
        elif etype == "CLAIM_APPROVED":
            fid, fv = _formula_ref(body)
            self._require_formula_version(fid, fv)
            kind = body["kind"]
            if kind not in CLAIM_KINDS:
                raise ReleaseError("unknown_claim_kind", f"声明种类未登记: {kind}")
            scope = body["market_scope"]
            if not isinstance(scope, list) or not scope or not all(isinstance(r, str) and r for r in scope):
                raise ReleaseError("bad_market_scope", "market_scope 必须是非空地区码列表")
            missing = self._gate_failures(body["evidence_set"], fid, fv, kind, as_of)
            if missing:
                raise ReleaseError("evidence_gate_failed", "声明证据门槛未通过；" + "；".join(missing))
        elif etype == "CLAIM_NARROWED":
            claim = self.claims.get(agg_id)
            if not claim:
                raise ReleaseError("claim_not_found", f"声明不存在: {agg_id}")
            new_scope = set(body["market_scope"])
            if not new_scope <= claim["scope"]:
                raise ReleaseError(
                    "scope_only_narrows",
                    f"收窄范围 {sorted(new_scope)} 超出当前批准范围 {sorted(claim['scope'])}",
                )
        elif etype == "LOT_LOCKED":
            if agg_id in self.lots:
                raise ReleaseError("lot_already_locked", f"批次已锁定，不能重复锁定或改写: {agg_id}")
            self._check_lock(body, as_of)
        elif etype == "LOT_SALE_CLOSED":
            lot = self._require_lot(agg_id)
            if lot["state"] != "locked":
                raise ReleaseError("lot_not_lockable_for_sale", f"批次状态为 {lot['state']}，不能关闭销售")
        elif etype == "LOT_RECALLED":
            self._require_lot(agg_id)
        elif etype in ("CAPACITY_RESERVED", "CAPACITY_CONFIRMED"):
            self._check_capacity(etype, body)
        elif etype == "IMPACT_PROPAGATED":
            run = self.runs.get(agg_id, [])
            if body["batch_index"] != len(run):
                raise ReleaseError(
                    "impact_batch_gap",
                    f"传播批次须连续，下一批应为 {len(run)}，收到 {body['batch_index']}",
                )

    def _check_capacity(self, etype: str, body: dict) -> None:
        try:
            if etype == "CAPACITY_RESERVED":
                self.ledger.assert_reservable(body["pool"], float(body["quantity"]))
            else:
                self.ledger.assert_confirmed(body["pool"], body["plan_id"], float(body["quantity"]))
        except CapacityError as exc:
            code, _, message = str(exc).partition(":")
            raise ReleaseError(code, message or str(exc)) from exc

    def _check_lock(self, body: dict, as_of: datetime) -> None:
        fid, fv = body["formula_id"], int(body["formula_version"])
        formula = self._require_formula_version(fid, fv)
        if body.get("window") != formula["window"]:
            raise ReleaseError(
                "window_mismatch",
                f"生产窗口 {body.get('window')} 与配方窗口 {formula['window']} 不一致",
            )
        if not body.get("plant"):
            raise ReleaseError("plant_required", "锁定批次必须登记工厂")
        produced_at = parse_dt(body["produced_at"])
        if produced_at > as_of:
            raise ReleaseError("produced_in_future", "生产日期不能晚于锁定时间")
        regions = body.get("sale_regions")
        if not isinstance(regions, list) or not regions:
            raise ReleaseError("sale_regions_required", "批次必须登记销售区域")
        for package in body.get("packages", []):
            claim = self.claims.get(package["claim_id"])
            if not claim:
                raise ReleaseError("claim_not_found", f"包装位引用的声明不存在: {package['claim_id']}")
            if claim["formula_ref"] != (fid, fv):
                raise ReleaseError(
                    "claim_version_mismatch",
                    f"声明 {package['claim_id']} 批准自配方 {(claim['formula_ref'])}，"
                    f"不能贴在锁定配方 {(fid, fv)} 的批次上",
                )
            pkg_regions = set(package.get("regions", regions))
            if not pkg_regions <= set(regions):
                raise ReleaseError("package_region_outside_lot", "包装位区域超出批次销售区域")
            if not pkg_regions <= claim["scope"]:
                raise ReleaseError(
                    "claim_region_not_approved",
                    f"声明 {package['claim_id']} 未在 {sorted(pkg_regions - claim['scope'])} 获批",
                )
            missing = self._gate_failures(claim["evidence_set"], fid, fv, claim["kind"], produced_at)
            if missing:
                raise ReleaseError(
                    "evidence_gate_failed",
                    f"锁定时声明 {package['claim_id']} 证据不过关；" + "；".join(missing),
                )

    def _gate_failures(self, evidence_ids: list[str], fid: str, fv: int,
                       kind: str, as_of: datetime) -> list[str]:
        if kind == "flavor":
            return []
        records: list[dict] = []
        for eid in evidence_ids or []:
            event = self.evidences.get(eid)
            if not event:
                return [f"证据 {eid} 不存在或未被质量确认"]
            if _formula_ref(event) != (fid, fv):
                return [f"证据 {eid} 属于配方 {_formula_ref(event)}，不能用于 {(fid, fv)}"]
            records.append(event)
        return evaluate_gate(kind, records, as_of)

    def _require_formula_version(self, fid: str, fv: int) -> dict:
        formula = self.formulas.get(fid)
        if not formula or fv not in formula["versions"]:
            raise ReleaseError("formula_version_not_found", f"配方版本不存在: {fid} v{fv}")
        return formula

    def _require_lot(self, lot_id: str) -> dict:
        lot = self.lots.get(lot_id)
        if not lot:
            raise ReleaseError("lot_not_found", f"批次不存在: {lot_id}")
        return lot

    @staticmethod
    def _require_ingredient_shape(item: dict) -> None:
        for key in ("ingredient", "spec_id", "spec_version"):
            if not item.get(key) and not isinstance(item.get(key), int):
                raise ReleaseError("ingredient_shape", f"配方原料缺少 {key}")

    # ---- 投影 ----
    def _project(self, event: dict) -> None:
        agg_id = event["aggregate_id"]
        self._event_ids.add(event["event_id"])
        self._agg_versions[agg_id] = event["version"]
        etype, body = event["event_type"], event["payload"]
        if etype == "FORMULA_VERSIONED":
            formula = self.formulas.setdefault(agg_id, {"product": body["product"], "window": body["window"], "versions": {}})
            formula["versions"][event["version"]] = event
        elif etype == "INGREDIENT_SPEC_CHANGED":
            self.specs[agg_id] = {"ingredient": body["ingredient"], "version": int(body["spec_version"])}
        elif etype == "EVIDENCE_ACCEPTED":
            self.evidences[agg_id] = body
        elif etype == "CLAIM_APPROVED":
            self.claims[agg_id] = {
                "formula_ref": _formula_ref(body),
                "text": body["text"],
                "kind": body["kind"],
                "scope": set(body["market_scope"]),
                "evidence_set": list(body["evidence_set"]),
            }
        elif etype == "CLAIM_NARROWED":
            self.claims[agg_id]["scope"] = set(body["market_scope"])
        elif etype == "LOT_LOCKED":
            self.lots[agg_id] = {
                "id": agg_id,
                "formula_id": body["formula_id"],
                "formula_version": int(body["formula_version"]),
                "plant": body["plant"],
                "window": body.get("window"),
                "produced_at": parse_dt(body["produced_at"]),
                "regions": list(body["sale_regions"]),
                "packages": list(body.get("packages", [])),
                "state": "locked",
                "suspended": {},
                "notices": [],
            }
        elif etype == "LOT_SALE_CLOSED":
            self.lots[agg_id]["state"] = "sale_closed"
        elif etype == "LOT_RECALLED":
            self.lots[agg_id]["pre_recall_state"] = self.lots[agg_id]["state"]
            self.lots[agg_id]["state"] = "recalled"
            self.lots[agg_id]["recall_reason"] = body.get("reason", "")
        elif etype in ("CAPACITY_RESERVED", "CAPACITY_CONFIRMED"):
            self.ledger.apply(event)
        elif etype == "IMPACT_PROPAGATED":
            run = self.runs.setdefault(agg_id, [])
            run.append(event)
            for entry in body["affected"]:
                lot = self.lots[entry["lot_id"]]
                if entry["action"] == "suspend_labels":
                    for pkg in entry.get("packages", []):
                        slot = lot["suspended"].setdefault(pkg["ref"], set())
                        slot.update(entry.get("regions") or ["*"])
                else:
                    lot["notices"].append(entry)

    # ---- 命令 ----
    def version_formula(self, actor: dict, formula_id: str, product: str, window: str,
                        *, ingredients: list[dict] | None = None, replaces: dict | None = None,
                        event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        payload = {"product": product, "window": window}
        if ingredients is not None:
            payload["ingredients"] = ingredients
        if replaces is not None:
            payload["replaces"] = replaces
        event = self._event("FORMULA_VERSIONED", "formula_revision", formula_id,
                            actor, payload, event_id=event_id,
                            occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def change_ingredient_spec(self, actor: dict, spec_id: str, ingredient: str, spec_version: int,
                               *, note: str = "", event_id: str | None = None,
                               occurred_at: datetime | None = None) -> dict:
        event = self._event("INGREDIENT_SPEC_CHANGED", "ingredient_spec", spec_id, actor,
                            {"ingredient": ingredient, "spec_version": spec_version, "note": note},
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def accept_evidence(self, actor: dict, evidence_id: str, *, formula_ref: dict, kind: str,
                        method_ref: str, valid_until: str, value: float | None = None,
                        baseline_value: float | None = None, unit: str = "",
                        basis_doc: str = "", event_id: str | None = None,
                        occurred_at: datetime | None = None) -> dict:
        body: dict[str, Any] = {
            "formula_ref": formula_ref, "kind": kind, "method_ref": method_ref,
            "valid_until": valid_until, "unit": unit, "basis_doc": basis_doc,
        }
        if value is not None:
            body["value"] = value
        if baseline_value is not None:
            body["baseline_value"] = baseline_value
        event = self._event("EVIDENCE_ACCEPTED", "evidence_record", evidence_id, actor, body,
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def approve_claim(self, actor: dict, claim_id: str, *, formula_ref: dict, text: str, kind: str,
                      market_scope: list[str], evidence_set: list[str],
                      event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        body = {"formula_ref": formula_ref, "text": text, "kind": kind,
                "market_scope": market_scope, "evidence_set": evidence_set}
        event = self._event("CLAIM_APPROVED", "label_claim", claim_id, actor, body,
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def narrow_claim(self, actor: dict, claim_id: str, market_scope: list[str], reason: str,
                     event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        previous = sorted(self.claims[claim_id]["scope"])
        event = self._event("CLAIM_NARROWED", "label_claim", claim_id, actor,
                            {"market_scope": market_scope, "previous_scope": previous, "reason": reason},
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def reserve_capacity(self, actor: dict, pool: str, plan_id: str, quantity: float,
                         event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        event = self._event("CAPACITY_RESERVED", "shared_capacity", pool, actor,
                            {"pool": pool, "plan_id": plan_id, "quantity": quantity},
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def confirm_capacity(self, actor: dict, pool: str, plan_id: str, quantity: float,
                         event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        event = self._event("CAPACITY_CONFIRMED", "shared_capacity", pool, actor,
                            {"pool": pool, "plan_id": plan_id, "quantity": quantity},
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def lock_lot(self, actor: dict, lot_id: str, *, formula_id: str, formula_version: int,
                 plant: str, produced_at: str, sale_regions: list[str],
                 packages: list[dict], quantity: float = 0.0,
                 event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        formula = self.formulas[formula_id]
        body = {
            "formula_id": formula_id, "formula_version": formula_version, "plant": plant,
            "window": formula["window"], "produced_at": produced_at,
            "sale_regions": sale_regions, "packages": packages, "quantity": quantity,
        }
        event = self._event("LOT_LOCKED", "production_lot", lot_id, actor, body,
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def close_lot_sale(self, actor: dict, lot_id: str, reason: str = "",
                       event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        event = self._event("LOT_SALE_CLOSED", "production_lot", lot_id, actor, {"reason": reason},
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    def recall_lot(self, actor: dict, lot_id: str, reason: str,
                   event_id: str | None = None, occurred_at: datetime | None = None) -> dict:
        event = self._event("LOT_RECALLED", "production_lot", lot_id, actor, {"reason": reason},
                            event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc))
        return self.append(event)

    # ---- 查询：给定日期与地区允许使用的文字 ----
    def allowed_words(self, lot_id: str, region: str, as_of: datetime | str) -> AllowedWords:
        lot = self._require_lot(lot_id)
        if isinstance(as_of, str):
            as_of = parse_dt(as_of)
        decisions: list[WordDecision] = []
        for package in lot["packages"]:
            ref = package["ref"]
            text, kind = package["text"], package["kind"]
            pkg_regions = set(package.get("regions", lot["regions"]))
            suspended = lot["suspended"].get(ref, set())
            reasons: list[str] = []
            if lot["state"] == "recalled":
                reasons.append(f"批次已召回：{lot.get('recall_reason', '')}".rstrip("："))
            elif lot["state"] == "sale_closed":
                reasons.append("批次已结束销售/已投放市场，不再加印新包装；按通知范围处理")
            if "*" in suspended or region in suspended:
                reasons.append("该包装位已被影响传播暂停（收窄、检测过期、原料变更或召回）")
            if region not in lot["regions"]:
                reasons.append(f"批次不在 {region} 销售")
            elif region not in pkg_regions:
                reasons.append(f"该包装位未规划在 {region} 使用")
            else:
                claim = self.claims[package["claim_id"]]
                if region not in claim["scope"]:
                    reasons.append(f"声明已收窄，{region} 不再获批（当前范围 {sorted(claim['scope'])}）")
                elif lot["state"] == "locked":
                    fid, fv = lot["formula_id"], lot["formula_version"]
                    reasons.extend(self._gate_failures(claim["evidence_set"], fid, fv, kind, as_of))
            decisions.append(WordDecision(ref, text, kind, not reasons, tuple(reasons)))
        return AllowedWords(lot_id, region, region in lot["regions"], tuple(decisions))

    # ---- 影响传播 ----
    def impact_candidates(self, trigger: dict, as_of: datetime) -> list[dict]:
        """确定性地计算受影响批次（按 lot_id 排序），返回受影响条目列表。"""
        entries: list[dict] = []
        kind = trigger["type"]
        if kind == "recall" and trigger["lot_id"] not in self.lots:
            raise ReleaseError("lot_not_found", f"批次不存在: {trigger['lot_id']}")
        for lot_id in sorted(self.lots):
            lot = self.lots[lot_id]
            if kind == "recall":
                if lot_id == trigger["lot_id"] and lot["state"] != "recalled":
                    raise ReleaseError(
                        "lot_not_recalled",
                        f"批次 {lot_id} 尚未登记 LOT_RECALLED；召回须由法规先登记，再传播影响",
                    )
                entry = self._recall_entry(trigger, lot) if lot_id == trigger["lot_id"] else None
            elif kind == "claim_narrowed":
                entry = self._narrowed_entry(trigger, lot)
            elif kind == "evidence_expired":
                entry = self._expired_entry(as_of, lot)
            elif kind == "ingredient_changed":
                entry = self._ingredient_entry(trigger, lot)
            else:
                raise ReleaseError("unknown_trigger", f"未知影响触发器: {kind}")
            if entry:
                entries.append(entry)
        return entries

    @staticmethod
    def _recall_entry(trigger: dict, lot: dict) -> dict:
        # 召回前已结束销售 → 已到市场，生成通知范围；仍在库 → 暂停全部包装。
        in_storage = lot.get("pre_recall_state", lot["state"]) == "locked"
        action = "suspend_labels" if in_storage else "notify"
        return {"lot_id": lot["id"], "action": action, "regions": sorted(lot["regions"]),
                "packages": [{"ref": p["ref"], "text": p["text"], "kind": p["kind"]}
                             for p in lot["packages"]],
                "reason_code": "recall", "trigger": trigger}

    def _narrowed_entry(self, trigger: dict, lot: dict) -> dict | None:
        claim_id = trigger["claim_id"]
        removed = set(trigger.get("removed_regions", []))
        hit = [p for p in lot["packages"] if p["claim_id"] == claim_id and set(p.get("regions", lot["regions"])) & removed]
        if not hit or lot["state"] == "recalled":
            return None
        regions = sorted({r for p in hit for r in set(p.get("regions", lot["regions"])) & removed})
        action = "suspend_labels" if lot["state"] == "locked" else "notify"
        return {"lot_id": lot["id"], "action": action, "regions": regions,
                "packages": [{"ref": p["ref"], "text": p["text"], "kind": p["kind"]} for p in hit],
                "reason_code": "claim_narrowed", "trigger": trigger}

    def _expired_entry(self, as_of: datetime, lot: dict) -> dict | None:
        if lot["state"] == "recalled":
            return None
        hit: list[dict] = []
        regions: set[str] = set()
        for package in lot["packages"]:
            claim = self.claims[package["claim_id"]]
            if claim["kind"] == "flavor":
                continue
            failures = self._gate_failures(claim["evidence_set"], lot["formula_id"],
                                           lot["formula_version"], claim["kind"], as_of)
            if any("过期" in f for f in failures):
                hit.append({"ref": package["ref"], "text": package["text"], "kind": package["kind"]})
                regions |= set(package.get("regions", lot["regions"]))
        if not hit:
            return None
        action = "suspend_labels" if lot["state"] == "locked" else "notify"
        return {"lot_id": lot["id"], "action": action, "regions": sorted(regions),
                "packages": hit, "reason_code": "evidence_expired",
                "trigger": {"type": "evidence_expired", "as_of": as_of.date().isoformat()}}

    def _ingredient_entry(self, trigger: dict, lot: dict) -> dict | None:
        spec_id = trigger["spec_id"]
        spec = self.specs.get(spec_id)
        if not spec:
            return None
        version_event = self.formulas[lot["formula_id"]]["versions"][lot["formula_version"]]
        used = [i for i in version_event["payload"].get("ingredients", []) if i["spec_id"] == spec_id]
        if not used or all(int(i["spec_version"]) == spec["version"] for i in used):
            return None
        if lot["state"] == "recalled":
            return None
        action = "suspend_labels" if lot["state"] == "locked" else "notify"
        # 原料/过敏原规格变更可能波及任意包装位的配料与过敏原声明，因此全部列入。
        return {"lot_id": lot["id"], "action": action, "regions": sorted(lot["regions"]),
                "packages": [{"ref": p["ref"], "text": p["text"], "kind": p["kind"]}
                             for p in lot["packages"]],
                "reason_code": "ingredient_changed", "trigger": trigger}

    def propagate_impact(self, actor: dict, run_id: str, trigger: dict, as_of_: datetime | str,
                         *, batch_size: int = 50, max_batches: int | None = None,
                         event_id: str | None = None,
                         occurred_at: datetime | None = None) -> list[dict]:
        """运行或续跑一次影响传播，返回本次调用追加的 IMPACT_PROPAGATED 事件。

        候选集合在首批事件中快照（``candidates``），中断后重放事件即可从
        下一批继续；触发器同样在首批固化，续跑入参会被忽略。``max_batches``
        限定单次调用最多写入的批数，用于把长任务切成多次执行。
        """
        as_of = parse_dt(as_of_)
        existing = self.runs.get(run_id, [])
        if existing:  # 从中断位置继续：候选快照与触发器在首批固化
            first = existing[0]["payload"]
            candidates = list(first["candidates"])
            trigger = first["trigger"]
        else:
            candidates = [e["lot_id"] for e in self.impact_candidates(trigger, as_of)]
        entries_by_lot = {e["lot_id"]: e for e in self.impact_candidates(trigger, as_of)}
        done = sum(len(e["payload"]["affected"]) for e in existing)
        new_events: list[dict] = []
        index = len(existing)
        emitted = 0
        while done < len(candidates):
            chunk = candidates[done:done + batch_size]
            affected = [entries_by_lot[lot_id] for lot_id in chunk]
            finished = done + len(chunk) >= len(candidates)
            body: dict[str, Any] = {"run_id": run_id, "trigger": trigger, "batch_index": index,
                                    "affected": affected, "finished": finished}
            if index == 0:
                body["candidates"] = candidates
            ev = self._event("IMPACT_PROPAGATED", "impact_run", run_id, actor, body,
                             event_id=event_id if finished and event_id else None,
                             occurred_at=occurred_at or datetime.now(timezone.utc))
            self.append(ev)
            new_events.append(ev)
            done += len(chunk)
            index += 1
            emitted += 1
            if max_batches is not None and emitted >= max_batches:
                break
        return new_events

    def impact_status(self, run_id: str) -> dict:
        events = self.runs.get(run_id, [])
        if not events:
            return {"run_id": run_id, "started": False, "finished": False, "processed": 0, "total": 0}
        first = events[0]["payload"]
        processed = sum(len(e["payload"]["affected"]) for e in events)
        return {
            "run_id": run_id, "started": True,
            "finished": events[-1]["payload"].get("finished", False),
            "processed": processed, "total": len(first["candidates"]),
            "next_batch_index": len(events),
            "affected": [a for e in events for a in e["payload"]["affected"]],
        }


def load_schema(path: str | Path = "contracts/domain.schema.json") -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
