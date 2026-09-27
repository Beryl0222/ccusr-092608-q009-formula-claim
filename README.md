# 节令配方声明放行库

定义节令食品配方、检测证据、标签声明和生产批次之间的放行事件。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/formula_claim/`：基础契约校验、放行服务（职责分离、证据门槛、批次锁定、声明收窄、共享资源）、影响传播（断点续跑）与命令行入口。
- `tests/`：信封、时间、版本、事件载荷与放行规则、影响传播测试。
- `docs/domain.md`：领域对象、事件语义与放行规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m formula_claim.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
