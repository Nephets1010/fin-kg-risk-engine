# ADR-0002：分层架构与依赖方向

- **状态**：已接受
- **日期**：2026-10-29

## 背景

系统同时包含 ETL、图计算、机器学习、HTTP 服务、定时任务五类工作。个人/小团队在 8~12 周内交付，最常见的失败模式不是技术选型错误，而是**模块边界模糊导致改动互相牵连** —— 改一个指标定义要动五个文件，改一次 API 契约要重跑数据管线。

## 决策

**采用严格的五层架构，依赖只能向内指。**

```
api          →  service  →  risk / research  →  graph  →  domain
scheduler    →  service  →  ...
ingestion    →  graph (repository 接口)  →  domain
domain       →  （零内部依赖）
```

各层职责：

| 层 | 职责 | 禁止 |
|---|---|---|
| `domain` | 实体、关系、指标目录、风险对象、溯源 | 任何 I/O、任何框架依赖 |
| `graph` | schema、repository 接口与实现、实体对齐、Cypher 库 | 依赖 risk / research / api |
| `ingestion` | 数据源适配、清洗、ETL 管线 | 直接写 Cypher（必须走 repository） |
| `risk` | 指标计算、评分、规则引擎、传导、GNN | 依赖 api / scheduler |
| `research` | KGQA、可比案例、研报生成 | 依赖 api / scheduler |
| `api` | HTTP 层、依赖注入、错误映射 | 业务逻辑 |
| `scheduler` | 定时编排 | 业务逻辑 |
| `observability` | 指标、告警分发 | 被业务逻辑反向依赖 |

## 关键机制

1. **`GraphRepository` 抽象**。上层只依赖接口，不依赖 Neo4j 驱动。这让 `InMemoryGraphRepository` 可以作为等价实现，在没有数据库时跑完整测试，并让 CI 保持秒级。
2. **`IndicatorEngine` 抽象**。指标的 Cypher 实现与本地实现可互换，保证评分逻辑在两种存储下一致。
3. **`domain` 层零依赖**。Pydantic 而已，任何一个业务规则都能用纯单元测试覆盖。

## 后果

**正面**

- 单元测试不需要数据库：211 项测试中 205 项是纯单元测试。
- 更换图存储只需新增一个 `GraphRepository` 实现。
- 业务规则（指标、阈值、规则）可以由风控人员而非开发人员维护。

**负面 / 代价**

- 抽象层本身有成本：多写一层接口、多一层间接。
- `IndicatorEngine` 的两个实现必须保持语义一致，需要人工同步（目前靠共享测试夹具约束）。
- 层数多，新人上手需要先读 ADR 才知道边界在哪。

## 验证方式

- `tests/unit/` 全部不依赖任何外部服务；CI 的 `test` job 不需要起容器。
- 依赖方向由代码评审保证，评审清单中包含「新增 import 是否跨越了层边界」。
- `tests/integration/test_api.py` 验证 HTTP 层到图层的完整链路。

## 撤销条件

如果项目规模收缩到单模块（例如只保留图谱查询），分层带来的间接成本超过收益，则合并 `risk` 与 `research` 到 `service`。
