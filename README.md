# 咸蛋黄风味偏差追因簿

记录蛋黄原料、加工批次、风味测量、投诉和库存处置的谱系事件。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/yolk_deviation/`：基础契约校验与命令行入口。
- `tests/`：信封、时间、版本和事件载荷测试。
- `docs/domain.md`：领域对象与事件语义。

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
PYTHONPATH=src python3 -m yolk_deviation.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
