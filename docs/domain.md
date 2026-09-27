# 领域约定

定义节令食品配方、检测证据、标签声明和生产批次之间的放行事件，以及在这些
事件之上运行的放行规则。所有发生时间都必须携带时区，版本号从 1 开始按聚合
递增，基础校验不会改写调用方输入。

## 角色与职责分离

每个事件都携带 `actor = {id, role}`，契约层 `actor_role_by_event` 与领域服务
双重强制，任何单一角色都不能完成全链放行：

| 角色 `role` | 职责 | 可发起事件 |
| --- | --- | --- |
| `rd`（研发） | 提出配方版本、原料规格变更 | `FORMULA_VERSIONED`、`INGREDIENT_SPEC_CHANGED`、容量预留 |
| `quality`（质量） | 确认小试与营养检测 | `EVIDENCE_ACCEPTED`、容量确认、影响传播 |
| `regulatory`（法规） | 决定可用说法与地区、收窄、召回 | `CLAIM_APPROVED`、`CLAIM_NARROWED`、`LOT_RECALLED`、影响传播 |
| `factory`（工厂） | 锁批次、关闭销售 | `LOT_LOCKED`、`LOT_SALE_CLOSED`、容量预留 |

## 聚合与事件

- `formula_revision`：`FORMULA_VERSIONED`（载荷 `product`、`window`、可选
  `ingredients`、`replaces`）。同一商品+窗口的后续版本通过 `replaces` 串起
  替代关系；每个原料项锁定 `spec_id` + `spec_version`。
- `ingredient_spec`：`INGREDIENT_SPEC_CHANGED`（`ingredient`、`spec_version`，
  版本必须递增），用于过敏原与合格原料变更追踪。
- `evidence_record`：`EVIDENCE_ACCEPTED`（`formula_ref`、`kind`、`method_ref`、
  `valid_until`，数值声称带 `value`/`baseline_value`，同源声称带 `basis_doc`）。
  证据绑定配方版本，不能跨版本使用。
- `label_claim`：`CLAIM_APPROVED`（`text`、`kind`、`market_scope`、
  `evidence_set`）与 `CLAIM_NARROWED`（只能把 `market_scope` 收窄为当前范围的
  子集，载荷保留 `previous_scope` 与 `reason`）。
- `production_lot`：`LOT_LOCKED`（`formula_id`、`formula_version`、`plant`、
  `produced_at`、`window`、`sale_regions`、`packages`）。锁定即固化配方版本与
  包装位，之后的配方修订不会回写已锁批次；批次不可重复锁定。销售结束用
  `LOT_SALE_CLOSED`，召回用 `LOT_RECALLED`（记录召回前状态）。
- `shared_capacity`：`CAPACITY_RESERVED` / `CAPACITY_CONFIRMED`，多个计划共享
  印刷额度与合格原料池，预留即扣减可用量，确认不得超过本计划预留量。
- `impact_run`：`IMPACT_PROPAGATED`（`run_id`、`trigger`、`batch_index`、
  `affected`、首批携带 `candidates` 快照、`finished`）。`batch_index` 必须连续。

## 证据门槛（按声明种类）

`kind` 取值 `low_sugar` / `reduced_oil` / `homologous` / `flavor`：

- **低糖**：固体食品糖含量 ≤ 5 g/100g（GB 28050），检测方法须可溯源，且
  查询/锁定日期不晚于 `valid_until`。
- **减油（比较声称）**：须同时给出当批脂肪 `value` 与经典配方基准
  `baseline_value`，相对降幅 ≥ 25%，证据在有效期内。
- **药食同源**：须登记卫健委药食同源名单等依据文件 `basis_doc` 且在有效期内。
- **一般风味**：无证据门槛，但仍须走法规批准并限定地区。

批准声明与锁定批次两个时点都会重新评估门槛；查询接口（
`ReleaseStore.allowed_words(lot, region, as_of)`）在批次仍在库时按查询日期
实时评估，过期检测会让对应文字在该日期不可用。

## 查询：给定日期与地区允许的文字

每个包装位返回一个判定：允许的文字列表，或逐条中文原因（声明已收窄、证据
过期、数值不达标、地区未获批/未规划、包装位已暂停、批次已售/召回等）。
旧配方批次只能携带绑定该配方版本批准的声明，按商品名取标签不会把新健康说法
贴到旧配方产品上。

## 声明收窄与已售批次

收窄触发影响传播（`trigger.type = claim_narrowed`）：

- 仍在库（`locked`）的批次 → `action = suspend_labels`，只暂停被撤地区的相关
  包装位，其他文字与地区不受影响；
- 已结束销售（`sale_closed`）的批次 → `action = notify`，生成通知范围（批次、
  地区、包装位、原因），不回收也不改写已售产品。

## 影响传播与断点续跑

触发器支持四类：`claim_narrowed`、`evidence_expired`（按 `as_of` 评估证据
有效期）、`ingredient_changed`（按锁定配方中引用的原料规格版本是否落后于
现行规格精确定位）、`recall`（批次须先由法规登记 `LOT_RECALLED`，动作按召回
前在库/已售区分）。

候选集合在运行首批事件中确定性快照（按 lot_id 排序），中断后重放事件再次调用
`propagate_impact` 即从下一个 `batch_index` 继续；`max_batches` 可把长任务切成
多次执行，`impact_status` 报告 `processed/total/next_batch_index`。

相同事件标识的业务幂等、并发冲突隔离和事件持久化由上层服务负责；本仓库定义
可稳定交换的事实与可直接复用的内存投影。
