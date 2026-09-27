# 咸蛋黄风味偏差追因簿

记录蛋黄原料、加工批次、风味测量、投诉和库存处置的谱系事件，并通过事件重放强制数量守恒、回避批准与阈值钉版；任一成品可逆向展开原料分摊，任一投诉可收窄候选批次、比对证据缺口并核对处置范围。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `docs/domain.md`：领域对象、事件语义、守恒规则与投诉研判流程。
- `data/sample.json`：单事件联调样例。
- `data/ledger.json`：端到端账本样例（三张原料批、双生产路径、混批、阈值换版、投诉、冻结与召回）。
- `src/yolk_deviation/`
  - `contracts.py`：事件信封与载荷契约校验（不改写输入）。
  - `ledger.py`：事件重放、库存账本、守恒闸门、回避批准、阈值钉版与处置顺序。
  - `lineage.py`：原料分摊、上下游闭包、投诉研判与处置范围核对。
  - `cli.py`：命令行入口。
- `tests/`：契约、守恒、拆并批、豁免、阈值、只追加复检、研判与冻结范围测试。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 命令行

```bash
# 校验单个事件
PYTHONPATH=src python3 -m yolk_deviation.cli validate contracts/domain.schema.json data/sample.json

# 重放账本：守恒、库存、留样、回避、钉版、处置范围与冻结顺序
PYTHONPATH=src python3 -m yolk_deviation.cli check contracts/domain.schema.json data/ledger.json

# 从成品反查原料分摊、剩余库存与门店去向
PYTHONPATH=src python3 -m yolk_deviation.cli trace contracts/domain.schema.json data/ledger.json FG-0924-G

# 投诉研判：候选收窄、逐路径证据/缺口、受影响门店与库存
PYTHONPATH=src python3 -m yolk_deviation.cli triage contracts/domain.schema.json data/ledger.json CMP-20260926-001
```

账本有效时 `check` 输出 `valid ledger: … 无阻断`；发现问题时逐行给出事件标识、代码和中文说明，并返回非零状态。
