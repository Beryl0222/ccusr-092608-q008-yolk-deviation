# 领域约定

记录蛋黄原料、加工批次、风味测量、投诉和库存处置的谱系事件。

聚合对象包括`raw_yolk_lot`、`process_batch`、`quality_observation`、`disposition_case`。事件类型包括`LOT_ACCEPTED`、`BATCH_TRANSFORMED`、`OBSERVATION_RECORDED`、`STOCK_HELD`、`DISPOSITION_APPROVED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `BATCH_TRANSFORMED`：载荷还需包含 `input_allocations`, `output_quantity`。
- `OBSERVATION_RECORDED`：载荷还需包含 `method_version`, `result`。
- `STOCK_HELD`：载荷还需包含 `scope`, `reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
