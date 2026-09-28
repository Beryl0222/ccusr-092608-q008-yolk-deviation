# 领域约定：咸蛋黄风味偏差追因簿

记录蛋黄原料、加工批次、风味测量、留样、投诉和库存处置的谱系事件，使任一成品都能
逆向计算到原料分摊，任一投诉都能逐生产路径排除原因或暴露证据缺口。

## 设计原则

1. **事件即事实，不可变**。所有结论（放行、豁免、冻结、处置）都是追加事件；复检只能
   新增 `OBSERVATION_RECORDED`，禁止改写既有观察（同一观察聚合只允许一条事件）。
2. **数量必须守恒**。拆批、混批、抽样消耗、报废都要给出输入、输出与来源占比
   （`fraction`，同一道工序各来源之和为 1）。投入 = 产出 + 登记损耗，超出
   0.5% 容差即记 `quantity_not_conserved`，节点被污染并向下游传播，污染批次不得放行。
3. **决定与执行分离**。班组长不能批准自己负责工序的偏差豁免
   （`granted_by != step_owner`）；库存只能由质量负责人在确认处置范围后冻结，
   冻结后、处置决定前禁止移动。
4. **规则按时点生效**。历史放行只按放行当日已生效的阈值与事件快照的 `rule_version`
   判断，新阈值不翻案；尚无放行结论的在制批次用现行阈值重判。
5. **冻结基于证据，不基于猜测**。投诉追因先收窄候选、再逐路径比对证据，只有存在
   越限测量或数量污染的候选才进入建议冻结范围；纯证据缺口候选先补证。

## 聚合与事件

| 聚合 `aggregate_type` | 含义 |
| --- | --- |
| `raw_yolk_lot` | 蛋黄原料批次（含供应商、等级、证明） |
| `process_batch` | 在制批次，每经历一道工序产生新的批次标识（拆批可产生多个产出） |
| `quality_observation` | 一次测量/感官抽检（复检 = 新聚合的新事件） |
| `retained_sample` | 留样登记（同时记录抽样消耗量） |
| `stock_unit` | 放行入库的库存单元（成品最小可追溯单元） |
| `consumer_complaint` | 消费者反馈 |
| `disposition_case` | 处置案（冻结范围 + 处置决定） |

| 事件 `event_type` | 聚合 | 载荷要点 |
| --- | --- | --- |
| `LOT_ACCEPTED` | raw_yolk_lot | 供应商、至少一份证明、等级、数量/单位 |
| `BATCH_TRANSFORMED` | process_batch | `step`、`input_allocations[]`（来源、数量、占比）、`outputs[]`、`loss_quantity`、`equipment_program` |
| `SAMPLE_DRAWN` | retained_sample | 来源批次、工序、消耗量、是否留样 |
| `MATERIAL_SCRAPPED` | 批次/原料 | 来源、报废数量、原因码 |
| `OBSERVATION_RECORDED` | quality_observation | 对象、工序、方法/规则版本、带时区的观察时间、结果值、检验人 |
| `RELEASE_DECIDED` | process_batch | `RELEASED/REJECTED/CONDITIONAL`、快照 `rule_version`、决定人/时间、入库库存列表 |
| `DEVIATION_EXEMPTION_GRANTED` | process_batch | 越限观察、工序、批准人、工序负责人（二者必须不同） |
| `STOCK_MOVED` | stock_unit | 从/到位置、数量、移动时间 |
| `STOCK_HELD` | disposition_case | 冻结范围（投诉/批次/库存）、质量负责人 |
| `DISPOSITION_APPROVED` | disposition_case | `RECALL/HOLD_CONTINUED/RELEASE_WITH_NOTICE/DESTROY`、质量负责人 |
| `COMPLAINT_FILED` | consumer_complaint | 品名、门店、购买时间、症状 |

所有时间必须携带时区；每个聚合的 `version` 从 1 连续递增。结构校验不改写调用方输入；
守恒、污染传播、放行闸门、授权链由台账层负责。

## 谱系与逆向分摊

- 每个产出批次只有一条产出它的 `BATCH_TRANSFORMED`，形成从原料到成品的 DAG。
- 反查成品时，沿投入边逐段上行：来源占比 = 路径上各 `fraction` 之积（同一成品各原料
  之和为 1）；原料折合数量 = 成品数量 × 逐段出品率（投入合计/产出合计）之积。
- 正向影响：给定原料/在制批次，可列出所有下游批次、实际入库的库存单元及其当前位置
  （由 `STOCK_MOVED` 折叠得出）与冻结状态。

## 放行闸门

`RELEASED` / `CONDITIONAL` 放行时校验：

1. 成品批次及其上游不处于污染（欠量、错账、单位不一致）状态；
2. 生产路径上、放行时点之前的每条测量按**当日已生效阈值**计算，越限观察必须存在
   放行前由非工序负责人批准的有效豁免，否则 `release_blocked_by_unresolved_deviation`；
3. 入库数量同样受来源批次可用量约束，拒收批次不得登记入库。

## 投诉追因

输入 `COMPLAINT_FILED` 与阈值册：

1. **收窄**：品名一致，且购买时点该库存单元（按移动历史）正位于投诉门店；
2. **逐路径比对**：对原料分级（查原料）、解冻/腌制/烘烤（查经历过该工序的在制节点）、
   门店存放（查库存单元）逐项给出 `EXCLUDED` / `SUSPECTED` / `MISSING_EVIDENCE`，
   已放行路径用测量当日阈值，在制路径用现行阈值；
3. **缺口清单**：供应商证明、计划内留样、放行决定、数量守恒；
4. **冻结建议**：仅有越限证据或污染的候选批次对应库存；实际冻结必须由质量负责人用
   `STOCK_HELD` 落账。

输出直接回答：这条投诉排除了哪些原因、还缺哪项留样或测量、哪些门店和库存真正受影响。
