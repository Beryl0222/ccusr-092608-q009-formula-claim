"""生成 data/ 下的全链路联调样例（幂等：重复运行得到同一文件）。

场景：2026 中秋窗口，莲蓉月饼从经典配方 v1 切换到减糖/减油/药食同源 v2；
研发、质量、法规、工厂四类角色分别留痕；两个计划共享印刷额度与合格莲蓉
原料池；两个 v2 批次一个已售、一个仍在库，随后声明收窄。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from formula_claim.domain import ReleaseStore, load_schema

RD = {"id": "rd-li", "role": "rd"}
QA = {"id": "qa-wang", "role": "quality"}
REG = {"id": "reg-zhao", "role": "regulatory"}
PLANT = {"id": "plant-sh1", "role": "factory"}

CST = timezone(timedelta(hours=8))


def at(y: int, m: int, d: int, h: int = 9) -> datetime:
    return datetime(y, m, d, h, 0, tzinfo=CST)


def main() -> None:
    schema = load_schema(ROOT / "contracts/domain.schema.json")
    totals = {"pool:print:健康月饼盒-2026": 100000.0, "pool:ingredient:合格莲蓉-2026": 5000.0}
    store = ReleaseStore(schema, totals)
    fid = "formula:莲蓉月饼"
    ref1 = {"formula_id": fid, "version": 1}
    ref2 = {"formula_id": fid, "version": 2}

    # 研发：原料规格与两版配方（v2 替代 v1，原料锁定到具体规格版本）。
    store.change_ingredient_spec(RD, "spec:莲蓉", "白莲蓉", 1, note="入厂时的初始规格",
                                 event_id="spec-lotus-v1", occurred_at=at(2026, 7, 20))
    store.change_ingredient_spec(RD, "spec:陈皮山药", "陈皮山药复合料", 1,
                                 event_id="spec-chenpi-v1", occurred_at=at(2026, 7, 20))
    store.version_formula(RD, fid, "节令月饼·莲蓉", "2026中秋",
                          ingredients=[
                              {"ingredient": "白莲蓉", "spec_id": "spec:莲蓉", "spec_version": 1},
                              {"ingredient": "小麦粉", "spec_id": "spec:小麦粉", "spec_version": 1},
                          ], event_id="formula-v1", occurred_at=at(2026, 8, 1))
    store.version_formula(RD, fid, "节令月饼·莲蓉", "2026中秋",
                          ingredients=[
                              {"ingredient": "低糖白莲蓉", "spec_id": "spec:莲蓉", "spec_version": 1},
                              {"ingredient": "陈皮山药复合料", "spec_id": "spec:陈皮山药", "spec_version": 1},
                              {"ingredient": "小麦粉", "spec_id": "spec:小麦粉", "spec_version": 1},
                          ], replaces=ref1, event_id="formula-v2", occurred_at=at(2026, 8, 3))

    # 质量：小试与检测证据。减油证据有效期只到 2026-11-30，供“检测过期”演示。
    store.accept_evidence(QA, "ev:lab:低糖-001", formula_ref=ref2, kind="low_sugar",
                          method_ref="GB 5009.8-2023", valid_until="2027-06-30T23:59:59+08:00",
                          value=4.2, unit="g/100g", event_id="ev-low-sugar",
                          occurred_at=at(2026, 8, 18))
    store.accept_evidence(QA, "ev:lab:减油-001", formula_ref=ref2, kind="reduced_oil",
                          method_ref="GB 5009.6-2016", valid_until="2026-11-30T23:59:59+08:00",
                          value=8.0, baseline_value=12.0, unit="g/100g",
                          event_id="ev-reduced-oil", occurred_at=at(2026, 8, 18))
    store.accept_evidence(QA, "ev:doc:同源-001", formula_ref=ref2, kind="homologous",
                          method_ref="文件评审 FR-2026-018", valid_until="2027-12-31T23:59:59+08:00",
                          basis_doc="卫健委按照传统既是食品又是中药材物质目录（2023 调整版）",
                          event_id="ev-homologous", occurred_at=at(2026, 8, 19))
    # 经典配方只登记风味证据；风味描述无证据门槛，留一条评审记录便于追溯。
    store.accept_evidence(QA, "ev:doc:风味-经典", formula_ref=ref1, kind="flavor",
                          method_ref="感官评定 QS-2026-031", valid_until="2027-09-30T23:59:59+08:00",
                          event_id="ev-flavor-classic", occurred_at=at(2026, 8, 19))

    # 法规：按不同证据门槛批准声明，并限定地区。
    store.approve_claim(REG, "claim:低糖", formula_ref=ref2, text="低糖莲蓉月饼",
                        kind="low_sugar", market_scope=["CN-SH", "CN-BJ"],
                        evidence_set=["ev:lab:低糖-001"], event_id="claim-low",
                        occurred_at=at(2026, 8, 22))
    store.approve_claim(REG, "claim:减油", formula_ref=ref2, text="减脂30%（对比经典配方）",
                        kind="reduced_oil", market_scope=["CN-SH", "CN-BJ"],
                        evidence_set=["ev:lab:减油-001"], event_id="claim-oil",
                        occurred_at=at(2026, 8, 22))
    store.approve_claim(REG, "claim:同源", formula_ref=ref2,
                        text="添加陈皮、山药（药食同源）", kind="homologous",
                        market_scope=["CN-SH"], evidence_set=["ev:doc:同源-001"],
                        event_id="claim-homo", occurred_at=at(2026, 8, 22))
    store.approve_claim(REG, "claim:经典风味", formula_ref=ref1, text="古法莲蓉风味",
                        kind="flavor", market_scope=["CN-SH", "CN-BJ", "CN-GZ"],
                        evidence_set=["ev:doc:风味-经典"], event_id="claim-flavor",
                        occurred_at=at(2026, 8, 22))

    # 印刷额度与合格原料由两个生产计划共享：先预留再确认。
    store.reserve_capacity(PLANT, "pool:print:健康月饼盒-2026", "plan:中秋A线", 60000,
                           event_id="cap-print-a", occurred_at=at(2026, 8, 25))
    store.reserve_capacity(PLANT, "pool:print:健康月饼盒-2026", "plan:中秋B线", 30000,
                           event_id="cap-print-b", occurred_at=at(2026, 8, 25))
    store.reserve_capacity(PLANT, "pool:ingredient:合格莲蓉-2026", "plan:中秋A线", 3000,
                           event_id="cap-lotus-a", occurred_at=at(2026, 8, 25))
    store.reserve_capacity(PLANT, "pool:ingredient:合格莲蓉-2026", "plan:中秋B线", 1500,
                           event_id="cap-lotus-b", occurred_at=at(2026, 8, 25))
    store.confirm_capacity(QA, "pool:print:健康月饼盒-2026", "plan:中秋A线", 60000,
                           event_id="cap-print-a-ok", occurred_at=at(2026, 8, 28))
    store.confirm_capacity(QA, "pool:ingredient:合格莲蓉-2026", "plan:中秋A线", 3000,
                           event_id="cap-lotus-a-ok", occurred_at=at(2026, 8, 28))

    # 工厂：锁定批次即固化配方版本与包装位。
    v2_packages = [
        {"ref": "P-低糖", "claim_id": "claim:低糖", "text": "低糖莲蓉月饼",
         "kind": "low_sugar", "regions": ["CN-SH", "CN-BJ"]},
        {"ref": "P-减油", "claim_id": "claim:减油", "text": "减脂30%（对比经典配方）",
         "kind": "reduced_oil", "regions": ["CN-SH", "CN-BJ"]},
        {"ref": "P-同源", "claim_id": "claim:同源", "text": "添加陈皮、山药（药食同源）",
         "kind": "homologous", "regions": ["CN-SH"]},
    ]
    store.lock_lot(PLANT, "lot:20260910-A", formula_id=fid, formula_version=2, plant="工厂·上海一厂",
                   produced_at="2026-09-10T06:00:00+08:00", sale_regions=["CN-SH", "CN-BJ"],
                   packages=v2_packages, quantity=30000, event_id="lot-a",
                   occurred_at=at(2026, 9, 10, 8))
    store.lock_lot(PLANT, "lot:20260912-B", formula_id=fid, formula_version=2, plant="工厂·上海一厂",
                   produced_at="2026-09-12T06:00:00+08:00", sale_regions=["CN-SH", "CN-BJ"],
                   packages=v2_packages, quantity=20000, event_id="lot-b",
                   occurred_at=at(2026, 9, 12, 8))
    # 旧配方批次只能使用 v1 批准的风味描述，不能按商品名套用新健康说法。
    store.lock_lot(PLANT, "lot:20260908-C", formula_id=fid, formula_version=1, plant="工厂·上海一厂",
                   produced_at="2026-09-08T06:00:00+08:00",
                   sale_regions=["CN-SH", "CN-BJ", "CN-GZ"],
                   packages=[{"ref": "P-风味", "claim_id": "claim:经典风味",
                              "text": "古法莲蓉风味", "kind": "flavor",
                              "regions": ["CN-SH", "CN-BJ", "CN-GZ"]}],
                   quantity=10000, event_id="lot-c", occurred_at=at(2026, 9, 8, 8))

    # A 批次已全部投放并关闭销售；B 批次仍在库。
    store.close_lot_sale(PLANT, "lot:20260910-A", reason="中秋档首发已售罄",
                         event_id="lot-a-closed", occurred_at=at(2026, 9, 20))

    # 法规收窄“减油”说法：撤回北京地区。只允许收窄，不得借修订扩张。
    store.narrow_claim(REG, "claim:减油", ["CN-SH"], reason="北京补测材料未在窗口内补齐",
                       event_id="claim-oil-narrow", occurred_at=at(2026, 9, 22))

    # 供应商莲蓉规格升版：触发“原料变更”影响传播时精确定位仍引用 v1 的批次。
    store.change_ingredient_spec(RD, "spec:莲蓉", "低糖白莲蓉（粒度细化）", 2,
                                 note="供应商工艺调整，过敏原声明不变",
                                 event_id="spec-lotus-v2", occurred_at=at(2026, 9, 24))

    out_dir = ROOT / "data"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "sample_stream.json").write_text(
        json.dumps(store.events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "sample_pools.json").write_text(
        json.dumps(totals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    triggers = {
        "trigger_narrowed.json": {"type": "claim_narrowed", "claim_id": "claim:减油",
                                  "removed_regions": ["CN-BJ"]},
        "trigger_expired.json": {"type": "evidence_expired"},
        "trigger_spec_changed.json": {"type": "ingredient_changed", "spec_id": "spec:莲蓉"},
        "trigger_recall.json": {"type": "recall", "lot_id": "lot:20260912-B"},
    }
    for name, body in triggers.items():
        (out_dir / name).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
    print(f"wrote {len(store.events)} events")


if __name__ == "__main__":
    main()
