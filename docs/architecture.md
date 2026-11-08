# 架构详解

本文补充 [README](../README.md) 的架构总览，说明模块边界、数据流与关键实现选择。设计决策的完整论证见 [`adr/`](adr/)。

---

## 1. 分层与依赖方向

```
┌──────────────────────────────────────────────────────────┐
│ api · scheduler · observability · cli · worker           │  交付层
└───────────────┬──────────────────────────┬───────────────┘
                │                          │
        ┌───────▼────────┐        ┌────────▼───────┐
        │  research      │        │      risk       │  应用层
        │ KGQA/简报/案例  │        │ 指标/评分/规则/  │
        └───────┬────────┘        │ 传导/GNN        │
                │                 └────────┬───────┘
                └──────────┬───────────────┘
                           │
                  ┌────────▼─────────┐
                  │      graph       │  存储抽象层
                  │ schema/repository│
                  │ /alignment/query │
                  └────────┬─────────┘
                           │
                  ┌────────▼─────────┐
                  │  ingestion       │  数据接入层
                  └────────┬─────────┘
                           │
                  ┌────────▼─────────┐
                  │      domain      │  纯模型层（零 I/O）
                  └──────────────────┘
```

**规则：依赖只能向内指。** 具体约束：

| 模块 | 可以依赖 | 禁止依赖 |
|---|---|---|
| `domain` | 仅 pydantic | 任何 I/O、任何框架 |
| `graph` | `domain` | `risk` / `research` / `api` |
| `ingestion` | `domain` / `graph`（仅 repository 接口） | 直接写 Cypher |
| `risk` | `domain` / `graph` | `api` / `scheduler` |
| `research` | `domain` / `graph` | `api` / `scheduler` |
| `api` | 全部下层 | 业务逻辑 |
| `scheduler` | `risk` / `ingestion` | 业务逻辑 |

### 为什么 `graph` 在 `risk` 之下

风险计算全部依赖图查询。若 `risk` 依赖 `api`，FastAPI 就成了业务逻辑的宿主，CLI 与 worker 无法复用 —— 这正是本项目在开发中实际踩到的问题：CLI 最初直接 import 了 `api.deps`，导致 FastAPI 的 `Depends` 对象被当成 repository 使用。修正方式是把「构建 repository」的逻辑抽到 `create_repository()`，CLI 与 worker 直接调用，API 通过依赖注入调用。**这是分层约束被违反时的真实症状。**

---

## 2. 数据流

### 2.1 数据接入

```
源适配器                清洗层              对齐层              存储层
┌────────────┐      ┌──────────────┐    ┌──────────────┐    ┌───────────┐
│ CSV        │      │ 字段映射      │    │ 信用代码       │    │ Neo4j     │
│ Tushare    │ ───► │ 类型强制      │ ─► │ 名称+地区     │ ─► │ MERGE     │
│ Synthetic  │      │ 日期归一      │    │ 别名表         │    │ 批量写    │
│ JSONL      │      │ 关键词分类    │    │ 模糊名兜底     │    │ 索引生效  │
└────────────┘      └──────────────┘    └──────────────┘    └───────────┘
     RawRecord           领域对象             规范 ID            图谱
```

**幂等性**：所有写入都是 `MERGE`（Neo4j）或按 ID 覆盖（文件图），并带 checkpoint。因此同一份输入重跑两次，图谱状态不变。

**对齐的三级级联**（`graph/alignment.py`）：

1. 统一社会信用代码 —— 权威标识
2. 规范化名称 + 注册地区 —— 唯一命中才接受，多义则拒绝
3. 别名表 —— 人工维护，最高优先

名称规范化会剥离「股份有限公司」「有限公司」等无区分度的后缀，全角转半角，去除括号与标点。

**新实体的 ID 是确定性的**：`company-<sha256前20位>`，由源键与地区派生。同一输入重跑不会产生重复节点。

### 2.2 风险计算

```
图谱 ──► 指标引擎 ──► 子分 ──► 加权 ──► 风险分 ──┬──► 回写图谱
                    (10 个指标)  (归一化)          │
                                                    ▼
                                              规则引擎 (YAML)
                                                    │
                                    ┌───────────────┴───────────────┐
                                    ▼                               ▼
                              风险传导 (BFS)                    预警生成
                                    │                               │
                                    ▼                               ▼
                              受影响主体 + 路径                  告警 + 证据
```

**指标引擎有两个实现**，语义必须一致：

| 实现 | 用途 | 何时使用 |
|---|---|---|
| `GraphIndicatorEngine` | 执行指标声明中的 Cypher | 生产（Neo4j） |
| `LocalIndicatorEngine` | 从内存快照计算同样语义 | 测试、离线、文件图 |

两者共享同一份 `IndicatorSpec` 定义，指标语义改一处即改两处。

### 2.3 智能投研

```
自然语言问题
    │
    ▼
意图分类（8 类）+ 实体/参数抽取
    │
    ▼
按意图取数（图谱查询）
    │
    ▼
模板渲染（+ 可选 LLM 润色）
    │
    ▼
答案 + 证据路径（节点 + 关系）
```

**关键点：证据路径来自图谱，不来自语言模型。** LLM 只负责把已经取回的事实写得更通顺，绝不生成数字、绝不生成 Cypher。

---

## 3. 关键实现选择

### 3.1 属性命名统一为 camelCase

`node_to_properties()` 把 Pydantic 的 snake_case 字段转成 camelCase（`event_type` → `eventType`），与 Cypher 查询、API 响应、内存图三者保持一致。

这个约定不是审美选择而是**正确性要求**：如果内存图存 `event_type` 而 Cypher 查 `eventType`，那么在测试中通过的行为在生产会静默失效 —— 指标读数全是 0，而没有任何报错。开发过程中确实发生过这个问题，由「内存图与 Neo4j 行为不一致」的检查发现。

### 3.2 金额单位统一为万元

工商登记的原始单位。所有 `regCapital`、`guaranteed_amount`、`amount` 均为**万元**。

理由：比率型指标要求分子分母同量纲。`担保余额 / 注册资本` 若两侧单位不同，比率毫无意义。

`parse_amount()` 负责归一：`"100万"` → `100.0`（万元），`"1.5亿"` → `15000.0`，`"2000元"` → `0.2`，裸数字按万元解读。

### 3.3 溯源以 JSON 字符串存储

Neo4j 不支持 list of map 属性，因此溯源序列化为 JSON 字符串。读取时由 `stored_items()` 反序列化回值对象。

详见 [ADR-0003](adr/ADR-0003-provenance-model.md)。

### 3.4 标签与关系类型白名单

Cypher 无法参数化标签。所有写入前校验标签/关系类型是否在白名单内，白名单与领域枚举同源，不会漂移。

详见 [ADR-0006](adr/ADR-0006-security-baseline.md)。

### 3.5 三种存储实现

| 实现 | 场景 | 特点 |
|---|---|---|
| `Neo4jGraphRepository` | 生产 | 真实图数据库，Cypher 服务端计算 |
| `FileGraphRepository` | 本地零依赖 | JSON 快照，读写全量，跨进程共享 |
| `InMemoryGraphRepository` | 单元测试 | 纯内存，最快，无 I/O |

三者实现同一个 `GraphRepository` 接口。`FileGraphRepository` 的存在是为了让「不装数据库也能跑通完整链路」这件事在 CLI 的多次调用之间成立。

---

## 4. 风险模型

### 4.1 评分

```
score = Σ(sub_score_i × weight_i) / Σ(weight_i)
```

- 分母用**实际权重和**，指标增减不引起量级跳变。
- 每个指标方向可正可反（股权集中度是「越低越险」）。
- 指标缺失时子分为 0，且 `coverage` 字段反映数据覆盖率。

### 4.2 传导

```
propagated(path) = source_score × Π(transmissibility_i) × decay^(hops-1)
```

传导系数写在边类型上（担保 1.0、持股 0.9、关联 0.3……），路径上最弱的一环即 `bottleneck`，是分析师的核查起点。

详见 [ADR-0005](adr/ADR-0005-risk-propagation.md)。

### 4.3 GNN

GraphSAGE 均值聚合 + 两层权重 + ReLU + 逻辑回归，NumPy 完整实现（含反向传播与类别加权损失）。

标签来源三选一：弱监督（规则反推）/ 公开数据集 / 人工 CSV，统一走 `LabelStore` 接口。

评估输出 precision / recall / F1 / ROC-AUC，并在样本不足时显式写入 warning。

详见 [ADR-0004](adr/ADR-0004-risk-scoring-model.md)。

---

## 5. 扩展点

| 想加什么 | 改哪里 | 需要动别的吗 |
|---|---|---|
| 新的风险指标 | `domain/indicators.py` 加 `IndicatorSpec` | 否，Cypher 与本地实现自动跟随 |
| 新的预警规则 | `rules/risk_rules.yaml` | 否 |
| 新的数据源 | `ingestion/sources.py` 加适配器 | 否，实现 `SourceAdapter.fetch` 即可 |
| 新的关系类型 | `domain/relations.py` 加枚举 + schema | 否，白名单自动跟随 |
| 新的问答意图 | `research/planner.py` 加意图 + `kgqa.py` 加 handler | 否 |
| 新的 API 端点 | `api/routers/` 加路由 | 否 |
| 换成别的图数据库 | 新增 `GraphRepository` 实现 | 否 |

这个「改一处即生效」的特性是分层架构的直接收益，也是本项目可维护性的核心。
