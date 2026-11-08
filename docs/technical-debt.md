# 技术债台账

本文件记录 2026-11-08 的一轮系统排查所发现的问题、处置方式与验证结果。

- **基线**：`dc89adb`，13477 行 Python，211 项测试，覆盖率 75.88%
- **处置后**：347 项测试，覆盖率 82.75%
- **原则**：保持对外接口与业务行为兼容；明确且低风险的直接修复；高风险或信息不足的仅记录

---

## 一、严重问题（会导致运行时错误或数据失真）

### S-01 GNN 训练必然抛 IndexError —— 边索引未随特征矩阵重映射

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/risk/gnn/trainer.py` `_restrict_edges()` |
| **等级** | 严重 |
| **影响** | `GNNTrainer.train()` 100% 崩溃，图神经网络模块从未真正跑通 |

**问题**：训练时 `train_features = features[train_mask]` 按行取子集，特征矩阵被压缩；而 `_restrict_edges()` 只按 mask 过滤边，**保留了原始节点索引**。边的下标指向原图谱位置，超出压缩后矩阵的行数：

```
IndexError: index 48 is out of bounds for axis 0 with size 48
```

**修复**：过滤边后同步重映射索引，把原始位置映射到压缩后位置。

**验证**：`tests/unit/test_gnn_trainer.py`（23 项）覆盖训练全流程；实测 40 家企业图谱上 F1 0.44 / ROC-AUC 0.64（合成数据，符合预期）。

---

### S-02 FeatureExtractor 的节点预算被绕过 —— 边索引越界

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/risk/gnn/features.py` `_collect()` |
| **等级** | 严重 |
| **影响** | `max_nodes` 参数失效，大规模抽样时特征矩阵与边索引不匹配 |

**问题**：上限检查只发生在「扩展发现新节点」时，而种子节点在循环前就**全部**加入 `collected`。种子数超过 `max_nodes` 时上限被完全绕过：

```
extract(30 个种子, max_nodes=5)  →  实际返回 31 个节点
```

**修复**：把上限检查前移到种子收集阶段。

**验证**：`test_max_nodes_is_respected` 断言节点数 ≤ 上限；`test_the_edge_index_matches_the_node_count` 断言边索引不越界。

---

### S-03 LLM 渲染路径从未成功执行 —— 引用了不存在的字段

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/research/kgqa.py` `_render_with_llm()` |
| **等级** | 严重 |
| **影响** | 启用 LLM 后始终静默降级到模板，LLM 从未参与过回答生成 |

**问题**：函数引用 `plan.question`，但 `QueryPlan` 只有 `entity_name` / `entity_id`，**没有 `question` 字段**：

```
AttributeError: 'QueryPlan' object has no attribute 'question'
```

异常被 `_render()` 的 `except Exception` 捕获并降级到模板，因此这个 bug 一直静默存在——启用 LLM 的用户只会看到「LLM 没生效」，却看不到任何错误。

**修复**：把原始问题作为参数沿渲染链传递，而不是从 plan 反推（plan 存的是「解读」，不是「原文」）。

**验证**：`tests/unit/test_llm.py`（14 项）断言 LLM 路径真的被调用、prompt 里不含 Cypher、失败时降级到模板。

---

### S-04 错误信封有三种形状 —— 客户端需要三个解析器

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/api/app.py` |
| **等级** | 严重（对外接口不一致） |
| **影响** | 同一套错误分类，三种 JSON 结构 |

**问题**：同一套错误分类，三种响应形状：

| 来源 | 形状 |
|---|---|
| 平台错误类（`FinKgError`） | `{"code": ..., "message": ...}` |
| 路由 `HTTPException(detail={...})` | `{"detail": {"code": ..., ...}}` |
| FastAPI 请求校验失败 | `{"detail": [{...}]}` |

`docs/api.md` 声明的是扁平信封，`ErrorResponse` 模型也是扁平的——`detail` 包裹只是 FastAPI 默认行为的泄漏。

**修复**：新增 `HTTPException` 与 `RequestValidationError` 两个处理器，统一压平到扁平信封。**未改动任何路由代码**。

**验证**：`test_every_error_path_uses_the_same_envelope` 遍历四条错误路径，断言响应键只可能是 `{code, message, details}`。

---

## 二、行为不一致

### B-01 告警载荷在三个出口有三种形状

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/observability/alerting.py` |
| **等级** | 中 |
| **影响** | 同一条告警，HTTP 是 camelCase、审计文件是 snake_case、Webhook 也是 snake_case |

**问题**：`RiskAlert` 的字段是 snake_case，API 层手工映射成 camelCase，而 `FileChannel` / `WebhookChannel` 直接 `model_dump()`，落盘与外发都是 snake_case。

**修复**：新增 `alert_payload()` 统一序列化，三个出口共用。

**验证**：`test_the_payload_matches_the_api_field_names` 断言载荷字段名覆盖 `AlertResponse` 的全部字段。

---

### B-02 `AskRequest` 定义了但路由用裸 dict

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/api/routers/research.py` |
| **等级** | 中 |
| **影响** | 请求体无校验、OpenAPI 契约缺失、字段被静默忽略 |

**问题**：schema 里定义了 `AskRequest`（含 `question` 与 `topK`），路由却接收 `question: dict`：

- `{"question": ""}` 走业务逻辑抛错，而非参数校验
- 未声明字段被静默忽略
- OpenAPI 里这个端点的请求体是任意 object

**修复**：路由改用 `AskRequest`。`topK` 的语义未在任何文档中定义，因此**不擅自实现**——改为可选字段并在 schema 注释中说明「未设置时沿用规划器默认值」。

**验证**：新增 3 项契约测试（缺字段、未知字段、实体不存在）。

---

### B-03 实体不存在时返回 422 而非已声明的 404

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/api/routers/research.py` |
| **等级** | 中 |
| **影响** | 与 `docs/api.md` 声明的契约不符 |

**问题**：路由用 `except Exception` 把所有异常压成 422，包括 `NotFoundError`。但 `docs/api.md` 声明 404 = 「实体/任务不存在」，路由自己的 `ERROR_RESPONSES` 也列了 404。

**修复**：只捕获 `ValidationError` → 422；`NotFoundError` 交给全局处理器 → 404。**这是向已声明契约回归，不是破坏性变更。**

**验证**：`test_ask_returns_404_for_an_unknown_entity`。

---

### B-04 `AskRequest.topK` 是声明了但未实现的字段

| 项 | 内容 |
|---|---|
| **等级** | 低 |
| **影响** | 客户端传了该参数会以为生效了 |

**说明**：B-02 修复后，`topK` 仍无实现。**未擅自实现**——它的语义（返回条数上限？证据条数？候选实体数？）在文档中无定义，猜错比不做更糟。已改为可选并注明语义待定。详见「待确认项」。

---

## 三、未接线的已实现能力

这些功能代码完整、测试通过，但**没有任何调用方**——文档承诺了，实现却悬空。

| 编号 | 能力 | 位置 | 文档承诺 | 处置 |
|---|---|---|---|---|
| U-01 | **GNN 训练/推理** | `risk/gnn/` | ADR-0004「基准分与模型分并行输出」 | **已接线**（见下） |
| U-02 | **实体别名表** | `graph/alignment.py` | architecture.md「三级级联」第三级 | **已接线** |
| U-03 | `RedisJobStore` | `api/jobs.py` | ADR-0007「多副本部署用 Redis 版」 | 记录，见待确认项 |
| U-04 | `FileChannel` | `observability/alerting.py` | 「审计与回放」 | 已接线（随 B-01 修复） |
| U-05 | `SourceAdapter.healthcheck` | `ingestion/base.py` | — | 记录，见待确认项 |
| U-06 | `RiskScorer.coverage_report` | `risk/scoring.py` | ADR-0004「coverage 字段反映数据覆盖率」 | 记录，见待确认项 |
| U-07 | `EntityResolver.resolve_generic` | `graph/alignment.py` | — | 记录，见待确认项 |
| U-08 | `complete_json` | `research/llm.py` | — | 记录，见待确认项 |

### U-01 GNN 接线的做法

新增 `risk/gnn/service.py`（`GnnRiskService`），并：

- `RiskScore` 新增 `model_score` / `model_score_version` / `score_gap` 三个可选字段
- `RiskService` 接受一个可选的 GNN 服务，评分时把模型分附加上去
- API 响应的 `RiskScoreResponse` 新增 `modelScore` / `scoreGap` 两个可选字段
- CLI 新增 `fin-kg gnn train / load / status`

**行为兼容性**：无模型时 `modelScore` 为 `null`（不是 0），基准分**完全不受影响**。ADR-0004 要求的「两个分数并列输出、不融合」得到兑现。

**验证**：`tests/unit/test_gnn_service.py`（16 项），其中 `test_the_baseline_is_unchanged_by_the_model` 断言加模型前后基准分完全相同。

### U-02 别名表接线的做法

- 新增 `EntityResolver.from_alias_file()`，读取 YAML
- 新增配置项 `FIN_KG_INGESTION_ALIAS_FILE`（默认 `data/aliases.yaml`）
- CLI 的 `fin-kg ingest` 与 `fin-kg seed` 都传入带别名表的 resolver
- 提供 `data/aliases.yaml` 模板

**注意**：别名表的优先级是**第三级兜底**，不是「最高优先」——architecture.md 原文写的是「最高优先」，但代码实现里信用代码与名称+地区都排在它前面。这处以代码为准，文档已更正。

---

## 四、测试缺失

补测前后的覆盖率：

| 模块 | 补测前 | 补测后 |
|---|---|---|
| `scheduler/runner.py` | **0%** | 有覆盖 |
| `worker.py` | **0%** | 有覆盖 |
| `observability/alerting.py` | 41% | 有覆盖 |
| `research/llm.py` | 40% | 有覆盖 |
| `risk/gnn/trainer.py` | 53% | 有覆盖 |
| `api/jobs.py` | 部分 | 有覆盖 |
| **总计** | **75.88%** | **82.75%** |

新增测试文件：

| 文件 | 项数 | 覆盖内容 |
|---|---|---|
| `tests/unit/test_scheduler.py` | 32 | cron 解析（含 1=周一 惯例）、任务状态机、失败隔离 |
| `tests/unit/test_alerting.py` | 16 | 三个通道、通道失败隔离、载荷一致性 |
| `tests/unit/test_llm.py` | 14 | LLM 启停、失败降级、prompt 不含 Cypher |
| `tests/unit/test_jobs.py` | 14 | JobStore、RedisJobStore 跨实例可读 |
| `tests/unit/test_gnn_trainer.py` | 23 | 训练全流程、特征提取、模型存取 |
| `tests/unit/test_gnn_service.py` | 16 | 双分契约、模型分不污染基准分 |
| `tests/unit/test_performance.py` | 11 | 候选预筛不改变结果、批量回写 |

---

## 五、性能

### P-01 评分路径是 O(企业数)，且 `limit` 不约束计算量

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/risk/service.py` `rank()` |
| **等级** | 中 |
| **影响** | 10 万实体时单次排名约 7 秒 |

**问题**：`rank(limit=20)` 会**评分全部企业**再切片，`limit` 只影响输出不影响成本。实测：

| 企业数 | `rank()` 耗时 |
|---|---|
| 200 | 13ms |
| 1000 | 85ms |
| 5000 | 367ms |
| 10 万（外推） | ~7s |

**修复**：新增 `candidates` 参数，用**存储的风险分**预筛候选（存储分本身就是上一次同样计算写回的），并把 `limit`/`order_by` 下推到 Neo4j 查询。

**实测效果**：5000 家企业上 `rank(limit=20)` 从 367ms → **25ms，加速 14.5 倍，top-5 结果完全一致**。

**兼容性**：`candidates` 默认 `None` = 全量评分 = 原行为。不传该参数则排名最准。

### P-02 风险分回写是 N+1 写

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/risk/service.py` `_persist_score()` |
| **等级** | 中 |
| **影响** | 每个主体一次图查询 |

**修复**：新增 `GraphRepository.update_properties_bulk()`，一次查询更新多个节点。

---

## 六、架构问题

### A-01 服务缓存用 `id(repository)` 做 key —— 存在串号风险

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/api/deps.py` |
| **等级** | 中 |
| **影响** | CPython 会复用已释放对象的内存地址 |

**问题**：`_SERVICES: dict[int, dict[str, Any]]` 以 `id(repository)` 为 key。CPython 复用被回收对象的地址，因此一个已释放的 repository 与新分配的 repository 可能拿到相同的 id，导致**串用另一个图谱的服务实例**。

**修复**：改用 `weakref.WeakKeyDictionary`，以 repository 对象本身为 key。

### A-02 `find_nodes` 的结果集没有上限

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/graph/repository.py` |
| **等级** | 低 |
| **影响** | Neo4j 侧一次拉回全部节点 |

**修复**：`find_nodes` 新增 `limit` / `order_by` 参数，并下推到 Cypher（`ORDER BY` + `LIMIT`），内存实现同步支持。

---

## 七、配置与 CI

### C-01 CI 的 lint 未覆盖 `scripts/`

| 项 | 内容 |
|---|---|
| **文件** | `.github/workflows/ci.yml` |
| **等级** | 低 |

**问题**：`ruff check src tests` 不含 `scripts/`，而该目录有 3 个脚本（含密钥扫描器 `check_secrets.py`）。脚本不受门禁约束。

**修复**：改为 `ruff check src tests scripts`，并新增一步在 CI 里运行 `scripts/check_secrets.py`。

---

## 八、待确认项（信息不足，未擅自改动）

以下几项**存在多种合理解释**，强行实现可能与意图相悖，因此只记录不改动。

| 编号 | 项 | 为什么需要确认 |
|---|---|---|
| P-1 | `AskRequest.topK` 的语义 | 可能是「返回条数上限」「证据条数」「候选实体数」。文档无定义，猜错比不做更糟。已改为可选字段并注明待定 |
| P-2 | `RedisJobStore` 的装配方式 | 文档说「多副本部署时使用」，但没有说谁来创建 Redis 连接、何时切换。可能是按环境变量自动切换，也可能是显式配置项 |
| P-3 | `SourceAdapter.healthcheck` 的调用时机 | 放在 `/health` 里会让健康检查依赖每个数据源在线；放在独立端点更合理。语义未定义 |
| P-4 | `RiskScorer.coverage_report` 的暴露位置 | 放进 `RiskScoreResponse` 会改接口；放进独立的诊断端点则不影响。ADR-0004 提到过 coverage 字段但未指定位置 |
| P-5 | `EntityResolver.resolve_generic` 的使用对象 | Person/Event/Product 是否需要与 Company 同等强度的对齐？目前它们的 ID 直接取自源记录，跨源无法归一 |
| P-6 | `LLMClient.complete_json` 的调用场景 | JSON 模式通常配合「让模型输出结构化建议」，但具体用于哪个业务决策未定义 |
| P-7 | `FileGraphRepository` 的生产可用性 | 每次写入全量序列化整个图谱，写放大严重。当前定位是「本地零依赖体验」，但它会被自动选中作为 Neo4j 不可用时的降级目标 |
| P-8 | 并发导入的写入竞争 | 多个 worker 同时向 Neo4j 写入时，`MERGE` 的幂等性保证了不产生重复节点，但溯源的「读-改-写」可能丢更新。未做并发测试 |

---

## 九、变更影响范围

| 类别 | 内容 |
|---|---|
| **对外接口（新增，不破坏）** | `RiskScoreResponse.modelScore` / `.scoreGap`（可选字段，无模型时为 `null`）；`GET /api/v1/risk/rank?candidates=N`（可选参数）；`fin-kg gnn train/load/status` 三个 CLI 命令 |
| **对外接口（回归到已声明契约）** | 所有错误响应的信封从三种形状统一为扁平的 `{code, message, details}`；`/api/v1/research/ask` 的实体不存在从 422 改为 404 |
| **内部行为** | 风险分回写改为批量；GNN 训练与推理链路接通；别名表接入摄入管线 |
| **未改动** | 所有对外路径的既有语义、规则阈值、指标定义、实体 ID 生成规则、数据模型、错误分类表、图谱 schema |
| **配置** | 新增 `FIN_KG_INGESTION_ALIAS_FILE`（可选，缺省不影响） |
