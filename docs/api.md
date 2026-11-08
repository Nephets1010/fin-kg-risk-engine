# API 文档

基址：`http://localhost:8000`
交互式文档：`/docs`（Swagger UI）· `/redoc` · `/openapi.json`

鉴权：设置 `FIN_KG_API_KEY` 后，所有请求需携带 `X-API-Key` 头。

---

## 通用约定

### 错误信封

所有错误返回：

```json
{
  "code": "not_found",
  "message": "entity not found",
  "details": { "entity_id": "X" }
}
```

**所有错误路径共用同一个信封形状**——包括路由抛出的 `HTTPException`、平台自身的错误类、以及 FastAPI 的请求校验失败。三者都返回扁平的 `{code, message, details}`，客户端只需要一个解析器。

| `code` | HTTP | 含义 | 可重试 |
|---|---|---|---|
| `validation_error` | 422 | 参数不合法 | 否 |
| `not_found` | 404 | 实体/任务不存在 | 否 |
| `conflict` | 409 | 与当前状态冲突 | 否 |
| `unauthorized` | 401 | API Key 缺失或错误 | 否 |
| `rate_limited` | 429 | 超出请求预算 | 是 |
| `dependency_unavailable` | 503 | 外部依赖不可用 | 是 |
| `graph_store_error` | 503 | 图存储操作失败 | 是 |
| `ingestion_error` | 500 | 数据接入失败 | 否 |
| `rule_evaluation_error` | 500 | 规则求值失败 | 否 |
| `internal_error` | 500 | 未预期错误（Bug） | 否 |

### 响应头

每个响应都带 `X-Request-ID`，可用 `X-Request-ID` 请求头指定，便于日志关联。

---

## 图谱检索

### `GET /api/v1/graph/stats`

图谱规模统计。

```bash
curl -s http://localhost:8000/api/v1/graph/stats
```

```json
{
  "nodes": { "Company": 12, "Person": 8, "Event": 14, "Product": 4 },
  "relationships": { "GUARANTEE": 11, "HOLD": 9, "INVOLVED_IN": 14, "ISSUE": 4, "LEGAL_REP": 8 },
  "totalNodes": 38,
  "totalRelationships": 46
}
```

### `GET /api/v1/graph/entities`

分页查询实体。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `label` | string | `Company` | 节点标签 |
| `limit` | int | 20 | 每页数量（1~200） |
| `offset` | int | 0 | 偏移 |

```json
{
  "items": [
    { "id": "company-78bce297...", "name": "华信控股集团有限公司",
      "labels": ["Company"], "properties": { "industry": "综合投资", "riskScore": 9.5, ... } }
  ],
  "total": 12, "limit": 20, "offset": 0
}
```

### `GET /api/v1/graph/entities/{entity_id}`

实体详情，含完整属性与溯源。

**404**：`{"code": "not_found", "message": "entity not found"}`

### `GET /api/v1/graph/entities/{entity_id}/neighbourhood`

k 跳邻域路径。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `hops` | int | 2 | 跳数（1~6） |
| `relations` | string | `HOLD,GUARANTEE,RELATED,CONTROLS,INVESTS_IN` | 逗号分隔的关系类型，未知类型被忽略 |

```json
[
  {
    "nodeIds": ["company-a", "company-b"],
    "nodeNames": ["华信控股集团有限公司", "华信置业发展有限公司"],
    "relationTypes": ["GUARANTEE"],
    "hops": 1,
    "propagatedScore": null,
    "text": "华信控股集团有限公司 -> 华信置业发展有限公司"
  }
]
```

### `GET /api/v1/graph/entities/{entity_id}/paths`

两实体之间的路径。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `source` | string | 必填 | 起点实体 ID |
| `target` | string | 必填 | 终点实体 ID |
| `hops` | int | 4 | 最大跳数（1~8） |

---

## 风险

### `GET /api/v1/risk/entities/{entity_id}/score`

单主体的风险评分。响应包含**全部指标读数**，不是一个不透明数字。

```json
{
  "entityId": "company-ecc57aa...",
  "entityName": "恒基建筑工程有限公司",
  "entityType": "company",
  "score": 28.5,
  "level": "medium",
  "modelVersion": "baseline-1.0.0",
  "computedAt": "2026-11-08T10:57:45+08:00",
  "readings": [
    {
      "indicatorId": "IND-LIT-001",
      "indicatorName": "涉诉事件数",
      "version": "1.0.0",
      "category": "legal",
      "measurement": 1.0,
      "subScore": 25.0,
      "weight": 0.15,
      "unit": "次",
      "detail": "涉诉事件数 = 1 次"
    }
  ],
  "explanation": "恒基建筑工程有限公司 风险分 28.5（medium）。主要来源：强制执行金额占注册资本比 85分×权重0.15（贡献12.8）；对外担保余额占注册资本比 50分×权重0.20（贡献10.0）。"
}
```

`level` 取值：`low` / `medium` / `high` / `critical`。

### `GET /api/v1/risk/rank`

风险分排名，按分数降序。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `limit` | int | 20 | 返回条数（1~200） |
| `candidates` | int | 留空 | 只对风险分最高的 N 个候选重新评分（1~100000）。留空则全量评分 |

```json
{ "items": [ /* RiskScoreResponse */ ], "total": 12 }
```

**关于 `candidates`**：评分路径的复杂度是 O(企业数)。全量评分在 5000 家企业上约需 340ms，10 万家外推约 7 秒。指定 `candidates` 后只对存储风险分最高的 N 个候选重新评分，实测**加速 14 倍且 top-5 结果完全一致**（存储分本身就是上一次同样计算写回的）。不指定时保持全量评分，排名最准。

**响应字段**

| 字段 | 说明 |
|---|---|
| `score` | 基准分（加权指标综合），风控规则以此为准 |
| `modelScore` | 图神经网络模型分；未训练模型时为 `null`（不是 0） |
| `scoreGap` | 两个分值之差；差值大的主体值得人工复核 |
| `readings` | 全部指标读数与贡献度 |
| `explanation` | 一句话解释主要风险来源 |

### `GET /api/v1/risk/alerts`

全图规则预警。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `min_severity` | string | `info` | `info` / `warning` / `major` / `critical` |
| `limit` | int | 50 | 返回条数（1~500） |

```json
{
  "items": [
    {
      "alertId": "R-CHAIN-001:company-ecc57aa...",
      "ruleId": "R-CHAIN-001",
      "ruleName": "担保链传导预警",
      "severity": "critical",
      "status": "open",
      "entityId": "company-ecc57aa...",
      "entityName": "华信控股集团有限公司",
      "score": 27.0,
      "message": "华信控股集团有限公司 处于多层担保链中（7 条传导路径），最大受影响方：...",
      "evidence": {
        "indicators": [
          { "id": "IND-CHN-001", "name": "担保链深度", "measurement": 3.0,
            "subScore": 70.0, "weight": 0.1, "version": "1.0.0" }
        ],
        "level": "medium",
        "modelVersion": "baseline-1.0.0"
      },
      "transmissionPaths": [ /* PathResponse */ ],
      "triggeredAt": "2026-11-08T10:57:45+08:00"
    }
  ],
  "total": 3,
  "severities": { "critical": 1, "major": 2 }
}
```

### `GET /api/v1/risk/entities/{entity_id}/alerts`

某主体触发的全部预警。

### `GET /api/v1/risk/entities/{entity_id}/exposure`

风险敞口：传播后受影响的实体列表。

```json
{
  "entityId": "company-...",
  "entityName": "华信控股集团有限公司",
  "score": 27.0,
  "affected": [
    {
      "id": "company-...",
      "name": "华信置业发展有限公司",
      "hops": 1,
      "score": 24.3,
      "path": "华信控股集团有限公司 -[GUARANTEE]-> 华信置业发展有限公司"
    }
  ]
}
```

---

## 智能投研

### `POST /api/v1/research/ask`

自然语言问答。

```bash
curl -s -X POST http://localhost:8000/api/v1/research/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"华信控股集团有限公司的担保链"}'
```

```json
{
  "question": "华信控股集团有限公司的担保链",
  "answer": "华信控股集团有限公司 共有 7 条担保传导路径，最长 3 跳。示例：...",
  "intent": "guarantee_chain",
  "confidence": 0.9,
  "evidence": [
    {
      "kind": "path",
      "description": "担保链路径",
      "nodeIds": ["company-a", "company-b"],
      "nodeNames": ["华信控股集团有限公司", "华信置业发展有限公司"],
      "relationTypes": ["GUARANTEE"]
    }
  ],
  "data": { "company": "华信控股集团有限公司", "chains": [...], "chainCount": 7 },
  "plan": {
    "intent": "guarantee_chain",
    "entityName": "华信控股集团有限公司",
    "hops": 3, "limit": 20, "confidence": 0.9,
    "matchedKeywords": ["担保链"]
  }
}
```

**支持的意图：**

| `intent` | 触发词示例 |
|---|---|
| `guarantee_chain` | 担保链、担保穿透、代偿链 |
| `holding_structure` | 股权结构、股东、持股、实际控制 |
| `risk_assessment` | 风险、风险分、评级、危险 |
| `event_history` | 事件、诉讼、处罚、被执行、舆情、违约 |
| `related_parties` | 关联、关系、子公司、对外投资 |
| `exposure` | 敞口、暴露、受影响、传导 |
| `top_risked` | 最高风险、排名、最危险 |
| `entity_profile` | 概况、介绍、信息、资料 |

无法识别时返回 `intent: "unknown"` 并给出建议问法。

### `GET /api/v1/research/similar/{entity_id}`

结构可比案例。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `top_k` | int | 10 | 返回条数（1~50） |
| `min_similarity` | float | 0.3 | 相似度下限（0~1） |

```json
[
  {
    "entityId": "company-...",
    "entityName": "新盛实业有限公司",
    "similarity": 0.87,
    "dimensions": {
      "guaranteeExposure": 0.95,
      "holdingConcentration": 0.88,
      "eventDensity": 0.62,
      "degreeProfile": 0.91,
      "riskScore": 0.79
    },
    "riskScore": 18.75,
    "riskLevel": "low",
    "reason": "guaranteeExposure 相似度 0.95、degreeProfile 相似度 0.91"
  }
]
```

`dimensions` 说明**哪些维度相似**，而不只是给一个数字。

### `GET /api/v1/research/report/{entity_id}`

投研简报。

```json
{
  "entityId": "company-...",
  "entityName": "恒基建筑工程有限公司",
  "generatedAt": "2026-11-08",
  "markdown": "# 恒基建筑工程有限公司 投研简报\n\n## 主体概况\n...",
  "sections": [
    { "title": "主体概况", "content": "...", "bullets": [...], "data": {...} },
    { "title": "股权结构", ... },
    { "title": "风险画像", ... },
    { "title": "风险传导", ... },
    { "title": "风险事件", ... },
    { "title": "可比案例", ... },
    { "title": "关注要点", ... }
  ]
}
```

七个固定小节：主体概况 / 股权结构 / 风险画像 / 风险传导 / 风险事件 / 可比案例 / 关注要点。

---

## 任务

长任务遵循 **Init → Step → Poll**（见 [ADR-0007](adr/ADR-0007-long-running-jobs.md)）。

### `POST /api/v1/jobs/ingest`

启动数据接入任务。**返回 202**，不等待完成。

```json
{ "source": "csv", "since": "2026-10-01", "limit": 10000 }
```

```json
{
  "jobId": "a1b2c3d4...",
  "jobType": "ingest",
  "status": "pending",
  "createdAt": "2026-11-08T10:57:45+08:00",
  "updatedAt": "2026-11-08T10:57:45+08:00",
  "progress": 0, "total": 0,
  "result": null, "error": null
}
```

`source` 可选：`csv` / `synthetic` / `tushare`（需 `FIN_KG_TUSHARE_TOKEN`）。

### `GET /api/v1/jobs/{job_id}`

轮询任务状态。`status` 取值：`pending` / `running` / `succeeded` / `failed` / `cancelled`。

### `GET /api/v1/jobs/{job_id}/result`

任务结果。未完成时返回空结构。

```json
{
  "sourceIds": ["csv"],
  "fetched": 84, "accepted": 84, "rejected": 0, "deduplicated": 0,
  "nodesWritten": 38, "relationsWritten": 46,
  "durationSeconds": 0.031,
  "cleaning": { "total": 84, "accepted": 84, "rejected": 0, "deduplicated": 0,
                "coercedFields": 0, "errors": {} }
}
```

### `GET /api/v1/jobs?limit=20`

最近的任务列表。

---

## 运维

| 端点 | 说明 |
|---|---|
| `GET /health` | 存活检查 + 依赖状态 |
| `GET /ready` | 就绪检查 |
| `GET /metrics` | Prometheus 指标（text/plain） |

### Prometheus 指标

| 指标 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `fin_kg_http_requests_total` | Counter | method, path, status | 请求总数 |
| `fin_kg_http_request_duration_seconds` | Histogram | method, path | 请求延迟 |
| `fin_kg_ingestion_records_total` | Counter | source, outcome | 接入记录数 |
| `fin_kg_ingestion_duration_seconds` | Histogram | source | 接入耗时 |
| `fin_kg_risk_scores_computed_total` | Counter | model | 风险分计算次数 |
| `fin_kg_alerts_total` | Counter | rule, severity | 预警产生数 |
| `fin_kg_risk_propagations_total` | Counter | — | 传导次数 |
| `fin_kg_graph_nodes` | Gauge | label | 图节点数 |
| `fin_kg_graph_relationships` | Gauge | type | 图关系数 |
| `fin_kg_dependency_up` | Gauge | name | 依赖可达性（1/0） |
| `fin_kg_jobs_total` | Counter | job_type, status | 任务执行数 |
| `fin_kg_job_duration_seconds` | Histogram | job_type | 任务耗时 |

---

## 完整接口速查

```
GET    /                                     服务信息
GET    /health                               存活检查
GET    /ready                                就绪检查
GET    /metrics                              Prometheus 指标

GET    /api/v1/graph/stats                   图谱统计
GET    /api/v1/graph/entities                实体列表
GET    /api/v1/graph/entities/{id}           实体详情
GET    /api/v1/graph/entities/{id}/neighbourhood   邻域路径
GET    /api/v1/graph/entities/{id}/paths     两实体路径

GET    /api/v1/risk/entities/{id}/score      风险评分
GET    /api/v1/risk/rank                     风险排名
GET    /api/v1/risk/alerts                   预警列表
GET    /api/v1/risk/entities/{id}/alerts     主体预警
GET    /api/v1/risk/entities/{id}/exposure   风险敞口

POST   /api/v1/research/ask                  智能问答
GET    /api/v1/research/similar/{id}         可比案例
GET    /api/v1/research/report/{id}          投研简报

POST   /api/v1/jobs/ingest                   启动接入任务
GET    /api/v1/jobs/{id}                     任务状态
GET    /api/v1/jobs/{id}/result              任务结果
GET    /api/v1/jobs                          任务列表
```
