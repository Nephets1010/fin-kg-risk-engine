# ADR-0001：图存储选型 —— Neo4j Community

- **状态**：已接受
- **日期**：2026-10-29
- **决策者**：架构师

## 背景

系统需要支撑三类图查询：

1. **多跳路径检索** —— 「A 的担保链穿透到最终受损方」，需要 1~5 跳的可变长路径遍历。
2. **邻域聚合** —— 「谁持有这家公司 30% 以上股份」，需要按关系属性的过滤与聚合。
3. **子图抽样** —— GNN 推理需要在实体周围抽取 k 跳子图并构造特征矩阵。

同时面临两个硬约束：

- 目标规模 **10 万实体 / 50 万关系**（本项目交付档位），生产目标 百万实体 / 千万关系。
- 单机可跑、零外部 SaaS 依赖（作品集需要可复现、可演示）。

## 候选方案

| 方案 | 多跳查询 | 属性图过滤 | 部署成本 | 许可证 | 迁移到生产的路径 |
|---|---|---|---|---|---|
| **Neo4j Community** | 原生、可变长路径 | 原生 | 单容器即可 | GPLv3（Community） | 企业版 / Aura Cloud，语法完全兼容 |
| PostgreSQL + Apache AGE | 支持但性能随跳数衰减 | 原生 | 需装扩展 | Apache 2.0 | 与现有数仓同构 |
| NebulaGraph | 性能最强 | 需自行建模 | 集群较重 | Apache 2.0 | 语法与 Cypher 有差异 |
| 内存图（NetworkX / igraph） | 最快 | 弱 | 进程内 | BSD / MIT | 需重写全部持久化层 |
| 关系型表自建（邻接表） | 需递归 CTE，性能差 | 原生 | 低 | 无 | 无迁移价值 |

## 决策

**采用 Neo4j 5.x Community 作为图存储。**

理由：

1. **Cypher 的变长路径是核心需求**。`MATCH p=(a)-[:GUARANTEE*1..5]->(b)` 一行即可表达担保链穿透；用关系型表实现需要递归 CTE，且跳数增加时性能衰减明显。
2. **Neo4j 社区版是单实例的**，这一点在设计初期就明确接受：约束与索引策略（见 ADR-0004）保证千万关系下不会退化为全图扫描。
3. **生产迁移路径零改码**。Community → Enterprise → Aura Cloud 使用同一套 Cypher，repository 层的接口（`GraphRepository`）已经把驱动细节完全隔离，迁移只涉及改 `Neo4jGraphRepository.from_settings`。
4. **许可证可接受**。Community 版为 GPLv3，本项目以 Apache-2.0 开源；由于 Neo4j 以独立进程/服务方式部署、不与本项目代码链接，不构成衍生作品问题。若未来内嵌商业版，需重新评估。

## 后果

**正面**

- 「担保链穿透」这类业务问题有原生表达，不需要自研路径引擎。
- 图模式与业务语义一一对应，`RELATION_SCHEMA` 可以直接映射成 Neo4j schema。
- 社区版单机部署，作品集复现成本极低。

**负面 / 代价**

- 引入了一个必须运维的有状态服务，CI 里需要用 service container 起 Neo4j。
- Community 版单实例，无高可用；生产需要 Aura Cloud 或自建集群。
- Neo4j 的属性值类型有限制（例如不能存 list of map），这直接影响了溯源信息的存储方式（见 ADR-0003）。
- Cypher 不能参数化标签与关系类型，因此所有写入必须经过白名单校验（见 ADR-0006）。

## 验证方式

- `tests/integration/test_neo4j_integration.py` 在真实 Neo4j 上验证 schema 应用、节点合并、关系往返、多跳遍历与统计。
- `docker-compose.yml` 起一个 Neo4j 5.26 Community，CI 的 `neo4j-integration` job 对其跑测试。
- `fin-kg migrate` 幂等性由 `MIGRATIONS` 中所有语句均带 `IF NOT EXISTS` 保证。

## 撤销条件

出现以下任一情况时重新评估：

- 需要图写入的 QPS 超过单实例 Neo4j 的承载能力，且无法通过读写分离解决。
- 需要跨图库的联邦查询，且 Neo4j 不支持。
- 目标部署环境不允许引入独立数据库进程。
