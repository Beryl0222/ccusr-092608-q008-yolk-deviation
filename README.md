# 咸蛋黄风味偏差追因簿

把蛋黄原料、供应商证明、感官抽检、盐分与水分测量、处理步骤、设备程序、在制品拆并、
留样、投诉和处置决定保留为一条可以逆向计算的谱系。拆批、混批、抽样消耗和报废都登记
输入输出及来源占比；数量无法守恒即污染下游并阻断放行；复检只追加新观察；班组长不能
批准自己工序的偏差豁免；新阈值只参与仍在处理的判断。

## 目录

- `contracts/domain.schema.json`：聚合、事件与载荷字段约定（结构层）。
- `data/rules.json`：检测阈值册（按生效日版本化）与证据计划（原因项、留样要求）。
- `data/sample.json`：端到端联调样例台账（好/坏两条生产路径、拆批混批、抽样报废、
  放行、门店移动、投诉、冻结与召回）。
- `src/yolk_deviation/`
  - `contracts.py`：单事件结构校验（不改写输入）。
  - `ledger.py`：台账装载、版本链、引用完整性、数量守恒闸门、污染传播、放行与授权链。
  - `lineage.py`：谱系 DAG、原料逆向分摊、正向库存影响。
  - `rules.py`：阈值时点判断（历史按当日、在制按现行）。
  - `investigation.py`：投诉候选收窄、逐路径证据/缺口、冻结范围建议。
  - `cli.py`：命令行入口。
- `tests/`：契约、守恒、谱系、放行闸门、规则时点、追加只读、投诉追因与授权测试。
- `docs/domain.md`：领域语义。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 命令行

```bash
# 整本台账核验（结构 + 守恒 + 放行闸门 + 授权链）
PYTHONPATH=src python3 -m yolk_deviation.cli check \
  contracts/domain.schema.json data/sample.json --rules data/rules.json

# 从成品批次或库存单元反查原料分摊与每条生产路径
PYTHONPATH=src python3 -m yolk_deviation.cli trace \
  contracts/domain.schema.json data/sample.json SU-B1 --rules data/rules.json

# 从原料/在制批次正向列出受影响库存及其当前位置/冻结状态
PYTHONPATH=src python3 -m yolk_deviation.cli impact \
  contracts/domain.schema.json data/sample.json LOT-B

# 投诉追因：候选收窄、原因排除/嫌疑、证据缺口、建议冻结范围
PYTHONPATH=src python3 -m yolk_deviation.cli investigate \
  contracts/domain.schema.json data/sample.json CMP-20260921-01 \
  --rules data/rules.json
```

核验通过输出 `valid`；发现问题时给出代码、事件、字段定位与中文说明，并返回非零状态。
单事件旧用法仍兼容：`python -m yolk_deviation.cli <schema.json> <event.json>`。

## 样例场景说明

- `WIP-BAKE-G1`（好路径）：LOT-A 单一来源，解冻 95 分钟、盐 4.2%、中心 88℃ 均合格，
  投诉追因中五项原因全部 `EXCLUDED`，不进冻结范围。
- `WIP-BAKE-B1`（坏路径）：80% LOT-B + 20% LOT-A 混批；解冻 150 分钟越限但已由质量
  负责人（非解冻班组长）豁免后附条件放行；9 月 20 日新盐分阈值（≤7.5%）使其腌制
  7.7% 在"仍在处理"视角下越限，但 9 月 13 日的历史放行按当日规则（≤8.0%）不翻案；
  门店 10.5℃ 越限，建议冻结 `SU-B1`，质量负责人确认后 `STOCK_HELD` 并 `RECALL`。
