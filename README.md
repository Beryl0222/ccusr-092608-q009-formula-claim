# 节令配方声明放行库

定义节令食品配方版本、原料规格、检测证据、标签声明、生产批次、共享容量与
影响传播之间的放行事件，并提供在事件之上运行的放行领域服务：

- **角色分离**：研发提案、质量确认检测、法规批准/收窄声明与召回、工厂锁批次，
  契约层强制每个事件的合法角色，任何人都不能一人完成全链放行；
- **分级证据门槛**：低糖（≤5g/100g）、减油（对比经典配方降幅 ≥25%）、
  药食同源（依据文件）、一般风味（无门槛）分别评估；
- **批次锁版不可变**：锁定时固化配方版本与包装位，后续配方修订不回写已锁批次；
- **收窄区分在售/已售**：只暂停仍在库的包装位，已售批次生成通知范围；
- **共享容量防重复承诺**：印刷额度与合格原料由多计划共享，预留/确认两阶段扣减；
- **允许文字查询**：给定批次、地区、日期返回可用文字，拒绝时逐条说明缺失证据；
- **影响传播可续跑**：检测过期、原料规格变更、收窄、召回精确定位受影响批次，
  候选集合快照后可从中断批次继续。

## 目录

- `contracts/domain.schema.json`：对象、事件、载荷字段与角色权限约定。
- `data/`：可直接校验的联调样例。`sample.json` 是单事件最小样例；
  `sample_stream.json` + `sample_pools.json` 是全链路事件流与容量池总额；
  `trigger_*.json` 是四类影响传播触发器。
- `src/formula_claim/`：
  - `contracts.py`：基础契约校验（不改写输入）。
  - `capacity.py`：共享容量账册（预留/确认、防超卖）。
  - `domain.py`：放行领域服务（事件溯源投影、门槛、锁版、查询、影响传播）。
  - `cli.py`：单事件契约校验；`release.py`：允许文字、容量、召回、影响传播命令。
- `scripts/build_sample.py`：重新生成 `data/` 下的全链路样例（幂等）。
- `tests/`：契约测试与放行服务测试（门槛、角色、锁版、收窄、容量、续跑）。
- `docs/domain.md`：领域对象、事件语义与规则详解。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests scripts
```

## 单事件契约校验

```bash
PYTHONPATH=src python3 -m formula_claim.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 放行库命令行

```bash
# 给定批次、地区、日期，判断允许使用的包装文字（拒绝时给出缺失证据）
PYTHONPATH=src python3 -m formula_claim.release allowed \
  data/sample_stream.json lot:20260912-B CN-BJ 2026-09-25 --pools data/sample_pools.json

# 查看共享容量池占用
PYTHONPATH=src python3 -m formula_claim.release pools \
  data/sample_stream.json --pools data/sample_pools.json

# 法规登记召回，再传播影响
PYTHONPATH=src python3 -m formula_claim.release recall \
  data/sample_stream.json lot:20260912-B "包装异物风险"

# 影响传播：分批执行（--max-batches），重复执行同一 --run 即断点续跑；
# 新事件追加回事件流文件。退出码 3 表示运行尚未全部完成。
PYTHONPATH=src python3 -m formula_claim.release impact \
  data/sample_stream.json --trigger data/trigger_narrowed.json \
  --run run-narrow-2026 --as-of 2026-09-25 --batch-size 50 --max-batches 5 \
  --pools data/sample_pools.json
```

重新生成样例：

```bash
python3 scripts/build_sample.py
```
