# 领域约定

定义节令食品配方、检测证据、标签声明和生产批次之间的放行事件。

聚合对象包括`formula_revision`、`evidence_record`、`label_claim`、`production_lot`。事件类型包括`FORMULA_VERSIONED`、`EVIDENCE_ACCEPTED`、`CLAIM_APPROVED`、`LOT_LOCKED`、`IMPACT_PROPAGATED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `EVIDENCE_ACCEPTED`：载荷还需包含 `method_ref`, `valid_until`。
- `CLAIM_APPROVED`：载荷还需包含 `market_scope`, `evidence_set`。
- `LOT_LOCKED`：载荷还需包含 `formula_version`, `produced_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
