# 知识图谱数据模型

本文是 [`src/fin_kg/domain/`](../src/fin_kg/domain/) 的完整规格说明。模型定义的**唯一权威来源是代码**，本文与代码不一致时以代码为准，并应立即修正本文。

---

## 1. 通用约定

| 约定 | 说明 |
|---|---|
| **ID** | 全局唯一字符串。`Company`/`Person`/`Event`/`Product` 用确定性哈希 `prefix-<sha256前20位>`；`Entity` 通用标签承载全部节点 |
| **属性命名** | camelCase（`eventType`、`creditCode`、`occurredDate`） |
| **金额单位** | **万元**（工商登记原始单位）。比率型指标的两侧必须同量纲 |
| **日期** | ISO 8601 字符串（`2026-03-15`） |
| **时间戳** | 带时区的 ISO 8601（`2026-11-08T10:57:45+08:00`） |
| **标签** | 每个节点同时带 `:Entity` 与具体标签（`:Company` 等） |

---

## 2. 节点类型

### 2.1 Company —— 法人主体

风险锚点。系统里所有风险计算最终都落到 Company 上。

| 属性 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✓ | 确定性生成 |
| `name` | string | ✓ | 工商登记全称 |
| `creditCode` | string | | 统一社会信用代码，18 位，**唯一约束** |
| `regCapital` | float | | 注册资本（万元） |
| `industry` | enum | | 见 §2.1.1 |
| `status` | enum | | 见 §2.1.2 |
| `foundedDate` | date | | 成立日期 |
| `region` | string | | 注册地 |
| `riskScore` | float | | 0~100，由风险引擎回写 |
| `riskLevel` | enum | | `low` / `medium` / `high` / `critical` |
| `riskModelVersion` | string | | 计算该分数的模型版本 |
| `riskComputedAt` | timestamp | | 计算时刻 |
| `externalRefs` | map | | 外部系统 ID 映射 |
| `provenance` | string(JSON) | | 溯源记录，见 §5 |

#### 2.1.1 行业枚举

`finance` `real_estate` `manufacturing` `energy` `technology` `construction` `trade` `agriculture` `transport` `healthcare` `education` `culture_ent` `other`

未提供行业代码时，由 `classify_industry()` 按关键词表归类（金融、地产、制造、能源、科技、建筑、贸易、农业、交通、医疗、教育、文娱）。

#### 2.1.2 登记状态枚举

| 值 | 含义 | 归类关键词 |
|---|---|---|
| `active` | 存续 | 存续、在业、正常、开业 |
| `suspended` | 停业 | 停业、暂停、冻结 |
| `cancelled` | 注销 | 注销、吊销、撤销 |
| `revoked` | 撤销 | 同上 |
| `bankrupt` | 破产 | 破产、清算、重整 |
| `unknown` | 未知 | — |

### 2.2 Person —— 自然人

| 属性 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✓ | |
| `name` | string | ✓ | |
| `role` | enum | | `legal_rep` / `director` / `supervisor` / `actual_controller` / `shareholder` / `executive` |
| `riskScore` | float | | 0~100 |
| `riskLevel` | enum | | |
| `idNumberHash` | string | | 证件号哈希（**不存明文**） |
| `externalRefs` | map | | |
| `provenance` | string(JSON) | | |

### 2.3 Event —— 事件

| 属性 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✓ | |
| `name` | string | ✓ | |
| `eventType` | enum | | 见 §2.3.1 |
| `severity` | enum | | `low`(0.2) / `medium`(0.45) / `high`(0.75) / `critical`(1.0) |
| `occurredDate` | date | | 事件发生日期 |
| `amount` | float | | 标的金额（万元） |
| `court` | string | | 受理法院 |
| `caseNumber` | string | | 案号 |
| `description` | string | | 事件描述 |
| `provenance` | string(JSON) | | |

#### 2.3.1 事件类型枚举

`enforcement`（强制执行）`litigation`（涉诉）`penalty`（行政处罚）`arrears`（欠税欠款）`default`（违约）`rating_downgrade`（评级下调）`equity_freeze`（股权冻结）`business_change`（工商变更）`abnormal_operation`（经营异常）`negative_news`（负面舆情）`other`

由 `classify_event_type()` 按关键词归类。

### 2.4 Product —— 金融产品

| 属性 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✓ | |
| `name` | string | ✓ | |
| `productType` | enum | | `bond` / `fund` / `equity` / `structured` / `abs` / `other` |
| `issuerId` | string | | 发行人实体 ID |
| `amount` | float | | 发行规模（万元） |
| `issueDate` | date | | |
| `maturityDate` | date | | |
| `couponRate` | float | | 票面利率（0~1） |
| `rating` | string | | 信用评级 |
| `provenance` | string(JSON) | | |

---

## 3. 关系类型

| 类型 | 起点 | 终点 | 必填属性 | 可选属性 | 传导系数 |
|---|---|---|---|---|---|
| `HOLD` | Company | Company | `percent` (0~1) | `since` `until` `shareholdingType` | 0.90 |
| `GUARANTEE` | Company | Company | `guaranteedAmount` | `since` `until` `guaranteeType` | **1.00** |
| `RELATED` | Company | Company | — | `relationType` `detail` | 0.30 |
| `CONTROLS` | Company | Company | — | `controlType` | 0.95 |
| `INVESTS_IN` | Company | Company | — | `amount` `since` `round` | 0.60 |
| `MERGER_OF` | Company | Company | — | `since` `ratio` | 0.75 |
| `SUPPLIER_OF` | Company | Company | — | `amount` `since` | 0.40 |
| `CUSTOMER_OF` | Company | Company | — | `amount` `since` | 0.40 |
| `SHAREHOLDER_OF` | Person | Company | `percent` | `since` | 0.85 |
| `LEGAL_REP` | Person | Company | — | `since` | 0.80 |
| `DIRECTOR_OF` | Person | Company | — | `since` `title` | 0.70 |
| `EMPLOYEE_OF` | Person | Company | — | `since` `title` | 0.50 |
| `ISSUE` | Company | Product | — | `amount` `issueDate` | 0.20 |
| `INVOLVED_IN` | Company | Event | — | `role` `amount` | 0.00 |
| `INVOLVED_IN_PRODUCT` | Event | Product | — | — | 0.00 |

**约束**

- 必填属性缺失 → 拒绝写入（`GraphStoreError`）
- 自环 → 拒绝写入
- `INVOLVED_IN` 与 `RELATED` 是对称关系，摄入时需归一方向

**传导系数**是风险沿该边传播的强度。它同时是**可解释性的锚点**：传导路径上系数最小的一环，就是分析师最该核查的位置。详见 [ADR-0005](adr/ADR-0005-risk-propagation.md)。

---

## 4. 风险指标目录

10 个指标，权重之和为 1.0（有测试守护）。

| ID | 名称 | 维度 | 权重 | 单位 | 方向 | 阈值 → 子分 |
|---|---|---|---|---|---|---|
| `IND-LIT-001` | 涉诉事件数 | 司法合规 | 0.15 | 次 | 越高越险 | 0→0, 1→25, 3→55, 10→85, 30→100 |
| `IND-ENF-001` | 强制执行金额占注册资本比 | 司法合规 | 0.15 | 倍 | 越高越险 | 0→0, 0.1→30, 0.5→60, 2→85, 5→100 |
| `IND-PEN-001` | 行政处罚次数 | 司法合规 | 0.10 | 次 | 越高越险 | 0→0, 1→35, 3→70, 6→100 |
| `IND-GUA-001` | 对外担保余额占注册资本比 | 财务 | 0.20 | 倍 | 越高越险 | 0→0, 0.5→25, 1→50, 2→80, 5→100 |
| `IND-DEF-001` | 违约事件数 | 财务 | 0.05 | 次 | 越高越险 | 0→0, 1→70, 2→90, 4→100 |
| `IND-CHN-001` | 担保链深度 | 传导 | 0.10 | 跳 | 越高越险 | 0→0, 1→20, 2→45, 3→70, 5→100 |
| `IND-HLD-001` | 股权集中度 | 治理 | 0.10 | 比率 | **越低越险** | ≤0→60, ≤0.1→40, ≤0.2→20, ≤0.35→5, >0.35→0 |
| `IND-CTL-001` | 实际控制人风险 | 治理 | 0.05 | 分 | 越高越险 | 0→0, 30→40, 60→75, 85→100 |
| `IND-RTE-001` | 评级下调次数 | 市场 | 0.05 | 次 | 越高越险 | 0→0, 1→50, 2→80, 4→100 |
| `IND-NEG-001` | 负面舆情热度 | 市场 | 0.05 | 条 | 越高越险 | 0→0, 3→40, 10→70, 25→100 |

### 4.1 指标版本化

每个指标有 `version`。每条预警记录贡献指标的版本号，因此「今天的分」与「半年前的分」可以逐指标对齐。

指标定义变更时**必须升版本号**。

### 4.2 缺失数据的处理

- 指标测量值为 `None` → 子分 0，**不计入**加权平均的分母修正之外（分母用全部权重）
- `coverage` 字段反映数据覆盖率，分析员可据此判断分数的可靠性
- **重要**：`LOWER_IS_RISKIER` 方向的指标在**无数据**时必须返回 `None` 而非 `0`。例如股权集中度返回 0 意味着「极端分散」，会让缺失数据的公司拿到最高治理风险分。

### 4.3 比率化

`IND-ENF-001` 与 `IND-GUA-001` 使用**比率而非绝对金额**。原因是跨规模可比性：5000 万担保对注册资本 1 亿的公司是严重风险，对注册资本 100 亿的公司微不足道。

---

## 5. 来源溯源

每一个节点和关系都带 `provenance` 属性，值为 JSON 字符串（Neo4j 不支持 list of map 属性）。

```json
[
  {
    "sourceType": "registry",
    "sourceId": "csv:companies",
    "recordId": "C001",
    "extractionMethod": "csv_import",
    "sourceUrl": null,
    "confidence": 1.0,
    "extractorVersion": null,
    "retrievedAt": "2026-11-08T10:57:45+08:00",
    "validTime": "2008-03-18",
    "contentHash": "C001",
    "provenanceHash": "9f2c1a7b4d3e8f6051a2b3c4d5e6f708"
  }
]
```

| 字段 | 说明 |
|---|---|
| `sourceType` | `registry` / `filing` / `court` / `news` / `market` / `manual` / `synthetic` / `derived` / `unknown` |
| `sourceId` | 逻辑源标识，如 `csv:companies` |
| `recordId` | 源内记录标识 |
| `extractionMethod` | `api` / `crawl` / `csv_import` / `llm_extraction` / `rule_match` / `manual_entry` / `synthetic` / `inherited` |
| `confidence` | **抽取**置信度（不是源的权威性） |
| `extractorVersion` | 抽取器版本 |
| `retrievedAt` | 平台获知该事实的时间 |
| `validTime` | 该事实在现实中成立的时间（双时间模型） |
| `provenanceHash` | 证据指纹，`sha256(sourceId\|recordId\|sourceUrl)[:32]`，用于去重 |

**合成数据恒为** `sourceType: "synthetic"`、`confidence: 0.5`、`extractionMethod: "synthetic"`，因此永远不会被误认为真实数据。

详见 [ADR-0003](adr/ADR-0003-provenance-model.md)。

---

## 6. 索引与约束

| 类型 | 对象 | 用途 |
|---|---|---|
| UNIQUE | `Entity.id` | 全局节点身份 |
| UNIQUE | `Company.id` `Person.id` `Event.id` `Product.id` | 各类节点身份 |
| UNIQUE | `Company.creditCode` | **实体对齐锚点**，防止同一法人多节点 |
| RANGE | `HOLD.percent` | 「谁持股超 30%」类查询 |
| RANGE | `GUARANTEE.guaranteedAmount` | 敞口聚合 |
| RANGE | `Event.occurredDate` | 指标窗口扫描 |
| RANGE | `Event.eventType` | 按类型过滤事件 |
| TEXT | `Company.name` `Person.name` `Product.name` | 名称检索 |
| RANGE | `Company.riskScore` | 风险排名 |
| RANGE | `Company.industry` | 行业筛选 |
| RANGE | `Entity.createdAt` | 增量同步 |
| TEXT | 关系的 `relationType` | 按关系类型遍历 |

迁移脚本见 [`migrations/`](../migrations/)，全部带 `IF NOT EXISTS`，幂等。

---

## 7. 示例数据

[`data/samples/`](../data/samples/) 是**合成**数据集，由 [`scripts/generate_sample_data.py`](../scripts/generate_sample_data.py) 确定性生成。

它刻意构造了一条可穿透的担保链，用于演示核心能力：

```
C001 华信控股集团
 ├─GUARANTEE→ C002 华信置业
 │             ├─GUARANTEE→ C003 华信供应链
 │             │             └─GUARANTEE→ C004 中信创业
 │             └─GUARANTEE→ C007 恒基建筑
 │                           └─GUARANTEE→ C008 远达物流
 │                                         └─GUARANTEE→ C009 远达能源
 ├─GUARANTEE→ C006 新盛实业
 └─GUARANTEE→ C011 宏远医疗（已吊销）
```

配套的 adverse events（涉诉、强制执行、行政处罚、评级下调、负面舆情、违约、欠税）分别挂在链上的不同主体上，使得：

- `华信控股` 的风险主要来自**担保敞口与链深度**
- `宏远医疗` 的风险主要来自**司法事件**
- `恒基建筑` 的风险来自**两者叠加**

这组对照正是「担保链穿透」演示的核心看点。

---

## 8. 扩展模型

### 加一个节点类型

1. 在 `domain/entities.py` 新增类，继承 `GraphNode`，设置 `label`
2. 在 `ENTITY_TYPE_TO_LABEL` / `LABEL_TO_ENTITY_TYPE` 注册
3. 在 `graph/repository.py` 的 `ALLOWED_LABELS` 加入标签
4. 在 `graph/schema.py` 加唯一约束迁移
5. 在 `ingestion/cleaning.py` 加清洗函数
6. 若参与风险计算，在 `domain/indicators.py` 加指标

### 加一个关系类型

1. 在 `domain/relations.py` 的 `RelationType` 加枚举
2. 在 `RELATION_SCHEMA` 补定义（起点、终点、必填属性、传导系数、说明）
3. `ALLOWED_RELATION_TYPES` 自动跟随（从枚举派生）
4. 若需要，在 `risk/propagation.py` 的 `DEFAULT_TRANSMISSION_RELATIONS` 加入

### 加一个指标

1. 在 `domain/indicators.py` 加 `IndicatorSpec`
2. 权重之和需仍为 1.0（`validate_indicator_weights()` 守护）
3. 阈值必须单调递增
4. 若为 `LOWER_IS_RISKIER`，在 `LocalIndicatorEngine` 中注意**无数据时返回 `None`**
