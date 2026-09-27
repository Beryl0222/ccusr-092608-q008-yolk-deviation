# 领域约定

记录蛋黄原料、加工批次、风味测量、投诉和库存处置的谱系事件。事件是只追加的事实；任何当前状态（库存、放行结论、处置范围）都由事件重放得到，不直接写结论。

聚合对象包括 `raw_yolk_lot`、`threshold`、`process_batch`、`quality_observation`、`shipment`、`complaint`、`disposition_case`。事件类型包括 `LOT_ACCEPTED`、`THRESHOLD_PUBLISHED`、`BATCH_TRANSFORMED`、`OBSERVATION_RECORDED`、`BATCH_RELEASED`、`DEVIATION_EXEMPTION_GRANTED`、`SHIPMENT_DISPATCHED`、`COMPLAINT_FILED`、`STOCK_HELD`、`DISPOSITION_APPROVED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `LOT_ACCEPTED`：`supplier` 供应商、`grade` 分级、`quantity`（含 `value` 与 `unit`）、可选 `certificates` 供应商证明编号。
- `THRESHOLD_PUBLISHED`：`metric` 指标（如 `salt_pct`、`water_pct`、`sensory_fishy`）、`limits` 判定区间（如 `{"min": …, "max": …}` 或 `{"max": …}`）、`effective_from` 生效时刻、`method_version` 对应方法版本。阈值按版本钉在每一次放行决定上（见"阈值钉版"）。
- `BATCH_TRANSFORMED`：一个工序步骤的全部进出账。`stage`（如 `grading`、`brining`、`thawing`、`baking`）、`step_id`、`unit` 计量单位；`input_allocations` 为来源列表，每项含 `source_batch`、`quantity`、可选 `source_step_id`；`outputs` 为去向列表，每项含 `batch_id`、`quantity`、可选 `role`（`product`/`sample`/`scrap`/`loss`，缺省为 `product`）。拆批即一项输入对应多项产品输出；混批即多项输入对应一项输出；抽样消耗与报废必须以 `sample`/`scrap`/`loss` 出现在同一事件中，不允许凭空消失。可选 `equipment_program`（设备程序，如烘烤曲线）、`params`（工序参数）。
- `OBSERVATION_RECORDED`：对原料批或在制品的一次观察。`target`（`{"aggregate_type": …, "aggregate_id": …}`）、`method_version`、`metric`、`result`、可选 `sample_consumed`（本次感官/理化抽检消耗的数量，须能在某一转换事件的 `sample` 输出中找到对应去向）。复检只追加新观察事件，不修改或撤回既往观察与放行。
- `BATCH_RELEASED`：`batch_id`、`threshold_versions`（本次判定实际使用的各指标阈值版本号）、`decided_by`、可选 `conclusion`。
- `DEVIATION_EXEMPTION_GRANTED`：对某工序某指标偏差的豁免。`batch_id`、`step_id`、`metric`（豁免覆盖的具体指标）、`responsible`（该工序负责班组长）、`granted_by`（批准人）、`reason`。
- `SHIPMENT_DISPATCHED`：`store_id`、`allocations`（每项 `batch_id` + `quantity`）、`sell_by`。门店是谱系的叶子去向。
- `COMPLAINT_FILED`：`store_id`、`purchased_at` 购买时间、`symptoms`（如 `fishy`、`hard`、`oily_gritty`）、可选 `sample_received` 样品编号。
- `STOCK_HELD`：`scope`（冻结范围，批次/门店/在制品的集合）、`reason`、`held_by`。
- `DISPOSITION_APPROVED`：`scope`、`decision`（如 `recall`、`release_hold`、`rework`、`scrap`）、`approved_by`，可携带 `complaint_id` 关联投诉。

## 账本守恒规则（重放校验）

任一 `BATCH_TRANSFORMED` 事件重放时必须同时满足，否则该事件记为阻断，下游一切步骤不得继续放行：

1. **单位一致**：事件内所有数量与来源批次的库存单位一致。
2. **进出守恒**：所有输入数量之和 = 产品输出 + `sample` + `scrap` + `loss` 之和（按给定容差比较）。
3. **库存充足**：每个来源的领取量不超过其当前可用产品库存；不得超领、不得预支。
4. **产物去向闭合**：每个输出批次获得相应数量的库存与来源占比；来源占比按输入量归一化并沿谱系传播（见下）。

## 来源占比与逆向计算

转换事件对每个输出记录其对每个上游原料批的占比：先按本次输入量得到直接来源占比，再与每个来源自身携带的原料分摊向量复合。于是任一成品批次都可以反查到每张原料批的分摊数量，任一发运到门店的成品也可以继续展开到原料层。正向则可由原料批枚举所有受影响在制品、成品批与门店。

## 观察只追加与回避批准

- 观察与放行都是追加事件：复检新增 `OBSERVATION_RECORDED`，历史读数保持不变。
- `DEVIATION_EXEMPTION_GRANTED` 必须满足 `granted_by != responsible`：班组长不能批准自己负责工序的偏差豁免；自批事件直接阻断。

## 阈值钉版（当日规则）

`BATCH_RELEASED` 必须显式给出 `threshold_versions`。判定只使用放行时刻已经生效（`effective_from` 不晚于放行时间）的最新阈值版本，版本号被钉入该放行事件。之后发布的新阈值只影响仍在处理、尚未放行的批次；历史放行不重判、不改写。

## 投诉研判

1. 根据样品编号（如有）、门店、购买时间与相关成品的 `sell_by` / 发运记录收窄候选成品批次。
2. 沿每条候选生产路径逆向展开，汇总各环节已有证据（供应商证明、分级、感官、盐分/水分、腌制批次、解冻、烘烤程序等）与仍然缺失的留样或测量。
3. 输出排除项（有合格证据覆盖的环节）、缺口（无观察、留样已消耗、方法缺失）以及各候选路径的原料分摊。
4. `DISPOSITION_APPROVED` 的 `scope` 只能落在研判覆盖的批次与门店内；冻结（`STOCK_HELD`）先于或随处置决定发生，范围不得超出溯源图实际可达的库存与门店。

相同事件标识的业务幂等、并发冲突隔离和外部分录由上层服务负责；本仓库定义可稳定交换的基础事实、重放守恒规则与溯源计算。
