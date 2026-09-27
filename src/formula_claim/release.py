"""放行库命令行：查询允许文字、运行/续跑影响传播。

事件流文件是 JSON 数组（基础契约中的事件对象）；容量池总额通过
``--pools`` 指定为 ``{"pool_id": 总额}`` 的 JSON 文件。

用法::

    python -m formula_claim.release allowed <events.json> <批次> <地区> <日期> [--pools pools.json]
    python -m formula_claim.release pools   <events.json> --pools pools.json
    python -m formula_claim.release impact  <events.json> --trigger t.json --run run-1 \
        --as-of 2026-12-01 --batch-size 2 --pools pools.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .domain import ReleaseError, ReleaseStore, load_schema

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = ROOT / "contracts/domain.schema.json"


def _load_stream(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"事件流必须是 JSON 数组: {path}")
    return data


def _store(events_path: Path, pools_path: str | None) -> tuple[ReleaseStore, list[dict]]:
    events = _load_stream(events_path)
    totals = json.loads(Path(pools_path).read_text(encoding="utf-8")) if pools_path else {}
    store = ReleaseStore.replay(load_schema(DEFAULT_SCHEMA), events, totals)
    return store, events


def _cmd_allowed(args: argparse.Namespace) -> int:
    store, _ = _store(Path(args.events), args.pools)
    try:
        result = store.allowed_words(args.lot, args.region, args.as_of)
    except ReleaseError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        return 1
    output = {
        "lot_id": result.lot_id,
        "region": result.region,
        "servable": result.servable,
        "allowed_texts": list(result.allowed_texts),
        "decisions": [
            {"ref": d.ref, "text": d.text, "kind": d.kind, "allowed": d.allowed,
             "missing_evidence": list(d.reasons)}
            for d in result.decisions
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if result.allowed_texts or not result.denials else 1


def _cmd_pools(args: argparse.Namespace) -> int:
    if not args.pools:
        print("需要 --pools 指定容量池总额文件", file=sys.stderr)
        return 2
    store, _ = _store(Path(args.events), args.pools)
    totals = json.loads(Path(args.pools).read_text(encoding="utf-8"))
    output = [vars(store.ledger.status(pool)) for pool in sorted(totals)]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    store, events = _store(Path(args.events), args.pools)
    trigger = json.loads(Path(args.trigger).read_text(encoding="utf-8"))
    actor = {"id": args.actor, "role": args.role}
    try:
        new_events = store.propagate_impact(
            actor, args.run, trigger, args.as_of,
            batch_size=args.batch_size, max_batches=args.max_batches)
    except ReleaseError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        return 1
    if new_events:
        events.extend(new_events)
        Path(args.events).write_text(
            json.dumps(events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = store.impact_status(args.run)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["finished"] else 3


def _cmd_recall(args: argparse.Namespace) -> int:
    store, events = _store(Path(args.events), args.pools)
    actor = {"id": args.actor, "role": "regulatory"}
    try:
        store.recall_lot(actor, args.lot, args.reason)
    except ReleaseError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        return 1
    Path(args.events).write_text(
        json.dumps(store.events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"recorded": "LOT_RECALLED", "lot_id": args.lot},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="formula_claim.release")
    sub = parser.add_subparsers(dest="command", required=True)

    p_allowed = sub.add_parser("allowed", help="查询某批次在某日期/地区允许使用的文字")
    p_allowed.add_argument("events")
    p_allowed.add_argument("lot")
    p_allowed.add_argument("region")
    p_allowed.add_argument("as_of")
    p_allowed.add_argument("--pools")
    p_allowed.set_defaults(func=_cmd_allowed)

    p_pools = sub.add_parser("pools", help="查看共享容量池占用")
    p_pools.add_argument("events")
    p_pools.add_argument("--pools", required=True)
    p_pools.set_defaults(func=_cmd_pools)

    p_impact = sub.add_parser("impact", help="运行或续跑影响传播（事件追加回事件流）")
    p_impact.add_argument("events")
    p_impact.add_argument("--trigger", required=True)
    p_impact.add_argument("--run", required=True, help="传播运行标识；重复使用即断点续跑")
    p_impact.add_argument("--as-of", required=True)
    p_impact.add_argument("--batch-size", type=int, default=50)
    p_impact.add_argument("--max-batches", type=int, default=None,
                          help="本次最多写入的批数；再次执行同一 run 即从断点继续")
    p_impact.add_argument("--actor", default="qa-bot")
    p_impact.add_argument("--role", default="quality", choices=["quality", "regulatory"])
    p_impact.add_argument("--pools")
    p_impact.set_defaults(func=_cmd_impact)

    p_recall = sub.add_parser("recall", help="法规登记批次召回（随后用 impact 传播）")
    p_recall.add_argument("events")
    p_recall.add_argument("lot")
    p_recall.add_argument("reason")
    p_recall.add_argument("--actor", default="reg-zhao")
    p_recall.add_argument("--pools")
    p_recall.set_defaults(func=_cmd_recall)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
