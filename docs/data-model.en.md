# Knowledge graph data model

> Document language: [中文](data-model.md) | English

This document is the complete specification of [`src/fin_kg/domain/`](../src/fin_kg/domain/). The **sole authoritative source for model definitions is the code**; where this document and the code disagree, the code wins and this document must be corrected immediately.

---

## 1. General conventions

| Convention | Description |
|---|---|
| **ID** | Globally unique string. `Company`/`Person`/`Event`/`Product` use a deterministic hash `prefix-<first 20 chars of sha256>`; the generic `Entity` label carries every node |
| **Property naming** | camelCase (`eventType`, `creditCode`, `occurredDate`) |
| **Monetary unit** | **万元 (10k CNY)** — the unit used by the company registry (工商登记). Both sides of a ratio indicator must use the same dimension |
| **Date** | ISO 8601 string (`2026-03-15`) |
| **Timestamp** | ISO 8601 with timezone (`2026-11-08T10:57:45+08:00`) |
| **Labels** | Every node carries both `:Entity` and its specific label (`:Company` and so on) |

---

## 2. Node types

### 2.1 Company — legal entity

The risk anchor. Every risk calculation in the system ultimately lands on a Company.

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | string | ✓ | Deterministically generated |
| `name` | string | ✓ | Full registered name as filed with the company registry (工商登记) |
| `creditCode` | string | | Unified Social Credit Code (USCC), 18 characters, **UNIQUE constraint** |
| `regCapital` | float | | Registered capital (万元, i.e. 10k CNY) |
| `industry` | enum | | See §2.1.1 |
| `status` | enum | | See §2.1.2 |
| `foundedDate` | date | | Incorporation date |
| `region` | string | | Registered region |
| `riskScore` | float | | 0~100, written back by the risk engine |
| `riskLevel` | enum | | `low` / `medium` / `high` / `critical` |
| `riskModelVersion` | string | | Model version that produced this score |
| `riskComputedAt` | timestamp | | Moment of computation |
| `externalRefs` | map | | Mapping of external system IDs |
| `provenance` | string(JSON) | | Provenance records, see §5 |

#### 2.1.1 Industry enumeration

`finance` `real_estate` `manufacturing` `energy` `technology` `construction` `trade` `agriculture` `transport` `healthcare` `education` `culture_ent` `other`

When no industry code is supplied, `classify_industry()` classifies it with a keyword table (Chinese keywords: 金融 finance, 地产 real estate, 制造 manufacturing, 能源 energy, 科技 technology, 建筑 construction, 贸易 trade, 农业 agriculture, 交通 transport, 医疗 healthcare, 教育 education, 文娱 culture and entertainment).

#### 2.1.2 Registration status enumeration

| Value | Meaning | Classification keywords (Chinese, as matched by the classifier) |
|---|---|---|
| `active` | Active | 存续, 在业, 正常, 开业 |
| `suspended` | Suspended | 停业, 暂停, 冻结 |
| `cancelled` | Cancelled | 注销, 吊销, 撤销 |
| `revoked` | Revoked | same as above |
| `bankrupt` | Bankrupt | 破产, 清算, 重整 |
| `unknown` | Unknown | — |

### 2.2 Person — natural person

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | string | ✓ | |
| `name` | string | ✓ | |
| `role` | enum | | `legal_rep` / `director` / `supervisor` / `actual_controller` / `shareholder` / `executive` |
| `riskScore` | float | | 0~100 |
| `riskLevel` | enum | | |
| `idNumberHash` | string | | Hash of the identity document number (**plaintext is never stored**) |
| `externalRefs` | map | | |
| `provenance` | string(JSON) | | |

### 2.3 Event — event

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | string | ✓ | |
| `name` | string | ✓ | |
| `eventType` | enum | | See §2.3.1 |
| `severity` | enum | | `low`(0.2) / `medium`(0.45) / `high`(0.75) / `critical`(1.0) |
| `occurredDate` | date | | Date the event occurred |
| `amount` | float | | Amount at stake (万元, i.e. 10k CNY) |
| `court` | string | | Court that accepted the case |
| `caseNumber` | string | | Case number |
| `description` | string | | Event description |
| `provenance` | string(JSON) | | |

#### 2.3.1 Event type enumeration

`enforcement` (enforcement) `litigation` (litigation) `penalty` (administrative penalty) `arrears` (tax and payment arrears) `default` (default) `rating_downgrade` (rating downgrade) `equity_freeze` (equity freeze) `business_change` (registry change) `abnormal_operation` (abnormal operation) `negative_news` (adverse media) `other`

Classified by `classify_event_type()` using a keyword table.

### 2.4 Product — financial product

| Property | Type | Required | Description |
|---|---|---|---|
| `id` | string | ✓ | |
| `name` | string | ✓ | |
| `productType` | enum | | `bond` / `fund` / `equity` / `structured` / `abs` / `other` |
| `issuerId` | string | | ID of the issuing entity |
| `amount` | float | | Issue size (万元, i.e. 10k CNY) |
| `issueDate` | date | | |
| `maturityDate` | date | | |
| `couponRate` | float | | Coupon rate (0~1) |
| `rating` | string | | Credit rating |
| `provenance` | string(JSON) | | |

---

## 3. Relation types

| Type | From | To | Required properties | Optional properties | Transmissibility |
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

**Constraints**

- A missing required property → the write is rejected (`GraphStoreError`)
- A self-loop → the write is rejected
- `INVOLVED_IN` and `RELATED` are symmetric relations, so the direction must be normalised during ingestion

The **transmissibility** is the strength with which risk propagates along that edge. It is at the same time **the anchor of explainability**: the link with the smallest coefficient on a propagation path is exactly where the analyst should look first. See [ADR-0005](adr/ADR-0005-risk-propagation.md).

---

## 4. Risk indicator catalogue

10 indicators whose weights sum to 1.0 (guarded by a test).

| ID | Name | Dimension | Weight | Unit | Direction | Threshold → sub-score |
|---|---|---|---|---|---|---|
| `IND-LIT-001` | Number of litigation events | Judicial & compliance | 0.15 | count | Higher is riskier | 0→0, 1→25, 3→55, 10→85, 30→100 |
| `IND-ENF-001` | Enforcement amount as a ratio of registered capital | Judicial & compliance | 0.15 | × | Higher is riskier | 0→0, 0.1→30, 0.5→60, 2→85, 5→100 |
| `IND-PEN-001` | Number of administrative penalties | Judicial & compliance | 0.10 | count | Higher is riskier | 0→0, 1→35, 3→70, 6→100 |
| `IND-GUA-001` | Outstanding external guarantees as a ratio of registered capital | Financial | 0.20 | × | Higher is riskier | 0→0, 0.5→25, 1→50, 2→80, 5→100 |
| `IND-DEF-001` | Number of default events | Financial | 0.05 | count | Higher is riskier | 0→0, 1→70, 2→90, 4→100 |
| `IND-CHN-001` | Guarantee chain depth | Propagation | 0.10 | hops | Higher is riskier | 0→0, 1→20, 2→45, 3→70, 5→100 |
| `IND-HLD-001` | Ownership concentration | Governance | 0.10 | ratio | **Lower is riskier** | ≤0→60, ≤0.1→40, ≤0.2→20, ≤0.35→5, >0.35→0 |
| `IND-CTL-001` | Actual controller risk | Governance | 0.05 | points | Higher is riskier | 0→0, 30→40, 60→75, 85→100 |
| `IND-RTE-001` | Number of rating downgrades | Market | 0.05 | count | Higher is riskier | 0→0, 1→50, 2→80, 4→100 |
| `IND-NEG-001` | Adverse media intensity | Market | 0.05 | count | Higher is riskier | 0→0, 3→40, 10→70, 25→100 |

Composite score: `score = Σ(sub_score × weight) / Σ(weight)`. The denominator uses the actual sum of weights, so adding or removing an indicator does not make the magnitude of the score jump.

### 4.1 Indicator versioning

Every indicator has a `version`. Each alert records the version of every contributing indicator, so that "today's score" and "the score from six months ago" can be aligned indicator by indicator.

The version number **must be bumped** whenever an indicator definition changes.

### 4.2 Handling of missing data

- An indicator measurement of `None` → sub-score 0, and it is **not** counted in the denominator correction of the weighted average (the denominator uses the full weight sum)
- The `coverage` field reflects data coverage, so the analyst can judge how reliable a score is
- **Important**: an indicator with the `LOWER_IS_RISKIER` direction **must return `None` rather than `0`** when there is **no data**. For example, an ownership concentration of 0 means "extremely dispersed", which would give a company whose shareholder data is missing the highest governance risk score.

### 4.3 Ratio-based

`IND-ENF-001` and `IND-GUA-001` use a **ratio rather than an absolute amount**. The reason is comparability across scales: a 50 million CNY guarantee is a serious risk for a company with 100 million CNY of registered capital, and negligible for one with 10 billion.

---

## 5. Provenance

Every node and relation carries a `provenance` property whose value is a JSON string (Neo4j does not support properties of type list of map).

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

| Field | Description |
|---|---|
| `sourceType` | `registry` / `filing` / `court` / `news` / `market` / `manual` / `synthetic` / `derived` / `unknown` |
| `sourceId` | Logical source identifier, such as `csv:companies` |
| `recordId` | Identifier of the record within the source |
| `extractionMethod` | `api` / `crawl` / `csv_import` / `llm_extraction` / `rule_match` / `manual_entry` / `synthetic` / `inherited` |
| `confidence` | **Extraction** confidence (not the authority of the source) |
| `extractorVersion` | Version of the extractor |
| `retrievedAt` | When the platform learned the fact |
| `validTime` | When the fact holds true in reality (bitemporal model) |
| `provenanceHash` | Evidence fingerprint, `sha256(sourceId\|recordId\|sourceUrl)[:32]`, used for de-duplication |

**Synthetic data is always** `sourceType: "synthetic"`, `confidence: 0.5`, `extractionMethod: "synthetic"`, and therefore can never be mistaken for real data.

See [ADR-0003](adr/ADR-0003-provenance-model.md).

---

## 6. Indexes and constraints

| Type | Object | Purpose |
|---|---|---|
| UNIQUE | `Entity.id` | Global node identity |
| UNIQUE | `Company.id` `Person.id` `Event.id` `Product.id` | Per-type node identity |
| UNIQUE | `Company.creditCode` | **Entity alignment anchor**, preventing one legal entity from having several nodes |
| RANGE | `HOLD.percent` | Queries such as "who holds more than 30%" |
| RANGE | `GUARANTEE.guaranteedAmount` | Exposure aggregation |
| RANGE | `Event.occurredDate` | Indicator window scans |
| RANGE | `Event.eventType` | Filtering events by type |
| TEXT | `Company.name` `Person.name` `Product.name` | Name lookup |
| RANGE | `Company.riskScore` | Risk ranking |
| RANGE | `Company.industry` | Industry filtering |
| RANGE | `Entity.createdAt` | Incremental sync |
| TEXT | `relationType` of relations | Traversal by relation type |

The migration scripts live in [`migrations/`](../migrations/); all of them use `IF NOT EXISTS` and are idempotent.

---

## 7. Sample data

[`data/samples/`](../data/samples/) is a **synthetic** dataset, generated deterministically by [`scripts/generate_sample_data.py`](../scripts/generate_sample_data.py).

It deliberately constructs a penetrable guarantee chain to demonstrate the core capability:

```
C001 华信控股集团
 ├─GUARANTEE→ C002 华信置业
 │             ├─GUARANTEE→ C003 华信供应链
→ │             │             └─GUARANTEE→ C004 中信创业
→ │             └─GUARANTEE→ C007 恒基建筑
→ │                           └─GUARANTEE→ C008 远达物流
→ │                                         └─GUARANTEE→ C009 远达能源
 ├─GUARANTEE→ C006 新盛实业
 └─GUARANTEE→ C011 宏远医疗（已吊销）
```

The accompanying adverse events (litigation, enforcement, administrative penalty, rating downgrade, adverse media, default, tax arrears) are attached to different entities along the chain, so that:

- `华信控股`'s risk comes mainly from **guarantee exposure and chain depth**
- `宏远医疗`'s risk comes mainly from **judicial events**
- `恒基建筑`'s risk comes from **the two combined**

This contrast is the core highlight of the "guarantee chain penetration" demonstration.

---

## 8. Extending the model

### Adding a node type

1. Add a class in `domain/entities.py` that inherits `GraphNode` and sets `label`
2. Register it in `ENTITY_TYPE_TO_LABEL` / `LABEL_TO_ENTITY_TYPE`
3. Add the label to `ALLOWED_LABELS` in `graph/repository.py`
4. Add a unique constraint migration in `graph/schema.py`
5. Add a cleaning function in `ingestion/cleaning.py`
6. If it takes part in risk computation, add an indicator in `domain/indicators.py`

### Adding a relation type

1. Add an enum member to `RelationType` in `domain/relations.py`
2. Complete the definition in `RELATION_SCHEMA` (from, to, required properties, transmissibility, description)
3. `ALLOWED_RELATION_TYPES` follows automatically (derived from the enum)
4. If needed, add it to `DEFAULT_TRANSMISSION_RELATIONS` in `risk/propagation.py`

### Adding an indicator

1. Add an `IndicatorSpec` in `domain/indicators.py`
2. The weights must still sum to 1.0 (guarded by `validate_indicator_weights()`)
3. The thresholds must increase monotonically
4. If it is `LOWER_IS_RISKIER`, remember in `LocalIndicatorEngine` to **return `None` when there is no data**
