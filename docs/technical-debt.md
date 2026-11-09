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
| U-04 | `FileChannel` | `observability/alerting.py` | 「审计与回放」 | 已接线（第六轮补，默认关闭，需 `FIN_KG_ALERT_FILE_PATH`） |
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

---

# 第三轮排查（2026-11-08 晚）

- **基线**：`54821f3`，15555 行 Python，347 项测试，覆盖率 82.58%
- **处置后**：377 项测试，覆盖率 82.90%
- **范围**：安全与配置、摄入幂等性、数据契约、并发、边界场景、资源管理
- **原则**：同前。每一项发现都先构造最小复现实际跑过，再判定成立与否

本轮的核心特征是：**四个缺陷都属于「代码完整、逻辑自洽、文档也写了，但从未真正生效」**。它们不产生异常，不产生告警，只在真实部署或重复运行时才显形——这正是单元测试全绿却仍有风险的原因。

---

## 十、第三轮发现的严重问题

### S-05 API Key 鉴权依赖从未挂载 —— 配置了密钥的部署仍是开放 API

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/api/deps.py:217` `require_api_key()`；四个业务路由的 `APIRouter(...)` |
| **等级** | 严重 |
| **影响** | 所有 `/api/v1/*` 端点无鉴权，风险评分、告警、研报数据全部公开可读 |
| **优先级** | P0 |

**问题**：`require_api_key` 被完整实现（读取 `X-API-Key`、比对配置、缺失时抛 401），并已在 `__all__` 中导出——但**没有任何路由引用它**。`grep -rn "require_api_key" src/fin_kg/api/` 只在 `deps.py` 自身命中。

这直接违背两处已声明的契约：

- `docs/adr/ADR-0006-security-baseline.md` 第 5 条：「`FIN_KG_API_KEY` 设置后，所有请求需带 `X-API-Key` 头」
- `docs/api.md` 第 6 行：同一表述；错误码表也列了 `unauthorized | 401`

**证伪**（设置 `FIN_KG_API_KEY=secret-key-123` 后实测）：

```
无 Key 访问 /api/v1/graph/stats  ->  200   （期望 401）
```

**修复**：把 `require_api_key` 作为 `APIRouter` 的 `dependencies` 挂到四个业务路由（graph / risk / research / jobs）上。

**兼容性**：未设置 `FIN_KG_API_KEY` 时 `require_api_key` 首行即 `return`，行为完全不变。`/`、`/health`、`/ready`、`/metrics` 保持免鉴权——运维探针不应持有密钥，否则容器编排的存活检查无法工作。

**验证**：`tests/unit/test_audit_round3.py` 的 `TestApiKeyEnforcement`（8 项），覆盖缺失 Key、错误 Key、正确 Key、运维端点免鉴权四种情形。

---

### S-06 内存图的关系写入不幂等 —— 重复导入产生幽灵关系

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/graph/repository.py:70` `GraphSnapshot.add_edge()` |
| **等级** | 严重 |
| **影响** | 每次重跑摄入，关系图谱翻倍；风险传导路径数与风险分随之虚高 |
| **优先级** | P0 |

**问题**：`add_node` 是幂等的（重复调用更新属性），但 `add_edge` 是**纯追加**：

```python
def add_edge(self, source, target, properties):
    edge = {"source": source, "target": target, **properties}
    self.edges.append(edge)          # ← 无条件追加
```

而 Neo4j 实现用的是 `MERGE (s)-[r:TYPE {source:..., target:...}]->(t)`，**是幂等的**，其类文档明确写着「All writes go through `MERGE` so the pipeline is idempotent and can be replayed after a failure」。两套实现语义不一致，内存实现单方面违反了幂等承诺。

**证伪**（同一份数据连续导入两次）：

```
第一次: 108 节点 / 123 关系     图谱规模 339   最高风险分 37.75
第二次: 108 节点 / 123 关系     图谱规模 462   最高风险分 38.75
```

关系数从 39 翻到 78（+100%），最高风险分被污染。

**修复**：`add_edge` 按 Neo4j 的合并键 `(source, target, relationType)` 查找已有边并更新属性，无则新增。

**兼容性**：合并键与 Neo4j 完全一致，两套实现语义对齐。首次写入行为不变。

**验证**：`TestIngestionIdempotency`（4 项），覆盖重复导入后图谱规模不变、风险分不变、不同类型关系不误合并、重复关系更新属性。

---

### S-07 生产模式接受仓库自带的开发默认密码

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/config/settings.py` `_enforce_production_secrets()` |
| **等级** | 严重 |
| **影响** | 生产部署若未覆盖 compose 默认值，将以公开在仓库中的已知密码运行图存储 |
| **优先级** | P1 |

**问题**：`docker-compose.yml` 给 `FIN_KG_NEO4J_PASSWORD` 提供了默认值 `fin-kg-dev-password`（为了让 `docker compose up` 开箱即用）。而生产校验只拒绝**空密码**：

```python
if not self.neo4j.password.get_secret_value():
    raise ValueError(...)
```

`docs/deployment.md` 的安全清单写着「`FIN_KG_NEO4J_PASSWORD` 已设置且**非默认值**」——文档承诺的防呆检查在代码里不存在。

**证伪**：

```
Settings(env='production', neo4j={'password': 'fin-kg-dev-password'})  ->  接受（不应接受）
```

**修复**：新增 `DEV_DEFAULT_PASSWORDS` 集合，生产模式下拒绝其中的弱默认值（`fin-kg-dev-password` / `neo4j` / `password` / `changeme` / `secret`）。

**兼容性**：仅影响 `env == "production"`；development / test / staging 不受影响，compose 的本地体验不变。

**验证**：`tests/unit/test_core.py` 新增 6 项（5 个弱默认值在生产被拒 + 非生产环境接受）。

---

## 十一、第三轮发现的中低风险问题

### B-05 本地指标引擎对未实现的指标静默失效

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/risk/indicators_engine.py` `LocalIndicatorEngine.measure()` |
| **等级** | 中 |
| **影响** | 新增指标在离线/本地模式下贡献恒为 0，易被误读为「该企业无此风险」 |

**问题**：`measure()` 对未识别的指标 ID 直接 `return None`，无任何日志。而 Cypher 实现会用 `measurement_cypher` 算出真实值。新增一个依赖 Cypher 的指标后，两套实现结果不同——这与 `docs/architecture.md` 的「指标语义改一处即改两处」声称相悖。

**修复（保守）**：**不改变返回行为**（仍返回 `None`，避免破坏现有契约），但补一条 `logger.warning` 说明该指标在本地存储下无实现。这是本轮唯一「修复后仍属待确认」的项——要让两套实现真正等价，需要为每个新指标补一份本地实现，属于新增功能而非缺陷修复，故未擅自实施。

**验证**：`TestLocalIndicatorEngineVisibility` 断言返回值不变且有告警。

---

### P-03 任务注册表无界增长

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/api/jobs.py` `JobStore` |
| **等级** | 中 |
| **影响** | 每 `POST /api/v1/jobs/ingest` 泄漏一个 Job，长驻进程内存单调增长 |

**问题**：`JobStore._jobs` 是普通字典，无容量上限、无 TTL、无淘汰逻辑。`create()` 只增不减。

**修复**：增加 `max_jobs`（默认 500）与 `ttl_seconds`（默认 86400）两个界限：

- 超龄淘汰：非 `running` 状态且闲置超过 TTL 的任务
- 容量淘汰：超出上限时按创建时间淘汰最旧的**已完成**任务
- `running` 任务不被 TTL 淘汰（其后台线程仍在写它）

**兼容性**：默认上限宽松，正常摄入节奏下不会触发；淘汰策略只移除已结束的任务，不影响进行中的作业。

**验证**：`TestJobStoreBounds`（5 项）。

---

## 十二、第三轮确认无问题的维度

以下维度经过实际验证**未发现问题**，一并记录以免后续重复排查：

| 维度 | 验证方式 | 结论 |
|---|---|---|
| 摄入幂等性（节点） | 重复导入后节点数不变 | 节点幂等，仅关系有问题（已修 S-06） |
| 风险传导的环处理 | 构造 A→B→C→A 担保环 | visited 集合生效，路径数有限，正确终止 |
| 并发写内存图 | 4 线程各写 50 个节点 | 无异常，节点数正确（CPython 字典操作原子性） |
| 关系数据契约 | 构造缺 `percent` 的 HOLD 关系 | domain 层严格拒绝，符合 `RELATION_SCHEMA` 声明 |
| 规则引擎畸形输入 | 空文件 / 非映射 / 空 condition | 三种畸形输入全部抛 `ValidationError` |
| 日志脱敏接线 | 检索 `register_secret_filter` 调用点 | API / worker / LLM 三处均已注册 |
| Cypher 标签白名单 | 检索 Neo4j 写入路径 | 关系类型写入前逐一校验，未知类型拒绝 |
| 图谱查询常量死代码 | AST 提取 15 个常量 + 全仓引用计数 | 0 个死代码 |
| 两套指标实现的覆盖面 | 比对 10 个指标 ID 的分发逻辑 | 10/10 全覆盖，无遗漏 |
| Neo4j 资源释放 | 检查 session 生命周期 | 使用 `with` 管理，无泄漏 |
| Prometheus 指标基数 | 检索全部 `labels()` 调用点 | 仅 `job_type`（有限集合），无高基数风险 |
| 路径遍历 | 检查 CSV 适配器与别名文件读取 | `glob("*.csv")` 不递归且按 stem 白名单过滤 |
| API 参数边界 | 枚举全部 Query 参数约束 | 全部带 `ge`/`le`，无无界参数 |
| LLM 重试策略 | 阅读 `llm.py` | `for attempt in range(2)`，实现与文档一致（初查 grep 未命中属误判） |
| 容器/依赖安全 | 检索镜像来源与依赖 | 官方镜像，依赖已锁版本 |

---

## 十三、第三轮待确认项

| 编号 | 项 | 为什么需要确认 |
|---|---|---|
| R-1 | 两套指标引擎的完整等价性 | 本地引擎需为每个新指标补一份实现。这属于「扩展点承诺」的兑现方式，文档未定义是自动生成还是手工维护。若接受「本地模式只支持已实现指标 + 明确告警」，则现状即可 |
| R-2 | `JobStore` 的上限与 TTL 取值 | 500 / 86400 是我按常规摄入节奏取的默认值。真实节奏未知（每分钟一次？每小时一次？），需要业务方给口径 |
| R-3 | `FileGraphRepository` 的降级定位 | Neo4j 不可用时它会被自动选中作为降级目标，但每次写入全量序列化整个图谱，写放大严重。是否应改为「降级即拒绝启动」 |
| R-4 | API Key 的粒度 | 当前是全局单密钥。是否需要按只读/写入区分权限，或支持多密钥轮换 |
| R-5 | 鉴权覆盖范围 | 当前 `/api/v1/*` 全覆盖。若未来有需要公开的只读端点（如对外披露的评级查询），需要明确豁免机制 |

---

## 十四、第三轮变更影响范围

| 类别 | 内容 |
|---|---|
| **对外接口（行为回归到已声明契约）** | 设置 `FIN_KG_API_KEY` 后 `/api/v1/*` 返回 401；未设置时行为完全不变。生产模式拒绝弱默认密码 |
| **对外接口（无变化）** | 全部响应结构、错误信封、字段名、端点路径与语义均未改动 |
| **内部行为** | 内存图的关系写入改为合并（与 Neo4j 对齐）；任务注册表增加容量与 TTL 界限；本地指标引擎对未实现指标补告警 |
| **未改动** | 风险评分模型与全部指标定义、规则文件、实体 ID 生成规则、图谱 schema、CLI 命令、Neo4j 写入路径、API 响应模型 |
| **配置** | 无新增配置项。`JobStore` 的两个界限是构造参数，默认值即可用 |
| **测试** | 347 → 377（新增 `tests/unit/test_audit_round3.py` 22 项 + `test_core.py` 6 项） |

---

# 第四轮补充（架构师独立审计 + 主 Agent 复核）

在第三轮之后，架构师以独立视角复查了架构、性能与可维护性维度，共报 19 条确认发现 + 6 条疑似。主 Agent 对每条做了实码复核，**其中三条经复核与原报有出入**，已在下文标注。本轮新修复 3 项。

## 十五、第四轮修复

### S-08 生产图存储上 KGQA / 相似案例 / GNN 全部静默空转

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/graph/repository.py`（新增 `neighbours()` 抽象方法）；`src/fin_kg/research/{kgqa,report,similarity}.py`；`src/fin_kg/risk/gnn/features.py`；`src/fin_kg/risk/indicators_engine.py` |
| **等级** | 严重 |
| **影响** | 生产（Neo4j）部署下，自然语言问答恒返回空、可比案例恒为空、图神经网络特征矩阵全零 |
| **优先级** | P0 |

**问题**：五处消费方直接读取 `getattr(repository, "snapshot", None)`。`GraphSnapshot` 是**内存实现的内部结构**，不是 `GraphRepository` 接口的一部分——`Neo4jGraphRepository` 根本没有这个属性。

代码用 `if snapshot is not None:` 做了保护，于是**不报错、不告警，只是返回空**。生产部署切到 Neo4j 后，三个核心功能静默失效。

**证伪**（用「实现了接口但没有 snapshot」的仓储替身实测）：

```
FakeNeo4j 有 snapshot: False
KGQA 答案: 风险分排名：1. 合成0号金融有限公司（0.0）；2. ...（全为 0.0）
GNN 特征矩阵形状: (10, 9) | 全零: True
相似案例数: 0
```

**修复**：在 `GraphRepository` 上新增 `neighbours(entity_id)` 抽象方法——内存实现从邻接表读，Neo4j 实现用 Cypher 查（`MATCH (n:Entity {id:$id})-[r]-() RETURN startNode(r).id, endNode(r).id, type(r), properties(r)`），并把 `type(r)` 投影为 `relationType`，使两套实现返回同构数据。五处消费方改为调用该接口，不再触碰实现细节。

**连带修复**：`LocalIndicatorEngine` 原先也依赖 snapshot，改为同样走接口——这样即便它被误用于 Neo4j 也不会静默返回 `None`。

**兼容性**：接口新增了一个抽象方法，三套仓储实现同步补齐。内存路径的返回结果与修复前逐字段一致（已用等价性测试固化）。

**验证**：`TestStoreAgnosticAccess`（5 项）——用一个「无 snapshot 属性」的仓储替身作为契约守卫，断言特征矩阵非全零、特征向量与内存实现逐位相等、相似案例列表相同、问答带证据。

---

### B-06 `/exposure` 的 `hops` 参数是死参数

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/api/routers/risk.py` `entity_exposure()`；`src/fin_kg/risk/service.py` `propagate()` |
| **等级** | 中 |
| **影响** | 调用方传 `hops=1` 与不传得到相同结果，参数形同虚设 |

**问题**：路由接收 `hops: int = Query(default=3, ge=1, le=6)`，但从未传给 `service.propagate(score)`——后者也没有这个参数。

**修复**：`RiskService.propagate` 增加 `max_hops` 参数，按 `hops` 字段过滤受影响实体。传播本身仍用配置的深度执行，因此窄查询不会得到不同的路径集，只是报告得更少。

**兼容性**：默认 `max_hops=None` 时行为与修复前完全一致。

---

### B-07 `alerts` 的文档与实际求值范围不符

| 项 | 内容 |
|---|---|
| **文件** | `docs/api.md` `GET /api/v1/risk/alerts` |
| **等级** | 低（文档债） |
| **影响** | 运维人员可能误以为告警覆盖全图 |

**问题**：文档写「全图规则预警」，但实现自上一轮性能优化起就只在**候选池**（按存储风险分排序后的前 `limit × 3`）上求值。代码行为是有意的性能取舍，错的是文档。

**修复**：改文档而非改代码——说明候选池机制、明确「排在候选池之外的主体不会产生告警」，并指向 CLI 批处理做全图穷举。

---

## 十六、架构师发现中经复核需要修正的三条

审计结论必须复核才能采信。以下三条**经主 Agent 实码验证后与原报有出入**：

| 原编号 | 原报 | 复核结果 |
|---|---|---|
| #2 | `graph/queries.py` 15 条 Cypher 常量全是死代码 | **不成立**。逐个统计引用后，只有 `GUARANTEED_BY` 在 `src` 与 `tests` 中均无引用；其余 14 条均有调用方。该常量已列入模块 `__all__`，属公开查询库 API，**保留不动** |
| #12 | `hops` 死参数 | **成立**，已修（见 B-06） |
| #8 | `alerts` 漏报主体 | **部分成立**。行为本身是有意的性能优化，不是缺陷；问题在文档表述，已修（见 B-07） |

---

## 十七、第四轮待确认项

| 编号 | 项 | 为什么需要确认 |
|---|---|---|
| S-1 | `alerts` 候选池是否需要「全图穷举」模式 | 候选池是性能取舍，但漏报在风控场景有代价。是否需要一个离线全量扫描任务（而非在线接口）需要业务判断 |
| S-2 | `neighbours()` 在 Neo4j 下的调用代价 | 每次调用是一次 Cypher 查询。相似案例检索会对每个候选主体调用一次，在 10 万实体下是 N+1 查询模式。需要确认是否改为批量邻域查询 |
| S-3 | `GUARANTEED_BY` 查询常量的去留 | 它未被调用，但在 `__all__` 中公开。删除是破坏性变更，需确认是否算公开 API |
| S-4 | `ALLOWED_RELATION_TYPES` 与 `ALLOWED_LABELS` 的漂移防护 | 白名单与领域枚举同源，但需确认是否有测试守护「新增 RelationType 时白名单自动更新」而非手工同步 |
| S-5 | `FileGraphRepository` 作为降级目标的写放大 | Neo4j 不可用时自动降级到它，每次写入全量序列化图谱。生产环境是否应改为拒绝启动 |

## 十八、第四轮变更影响范围

| 类别 | 内容 |
|---|---|
| **对外接口** | `RiskService.propagate` 新增可选 `max_hops`；`/api/v1/risk/exposure` 的 `hops` 参数现在真正生效。无破坏性变更 |
| **对外接口（新增）** | `GraphRepository.neighbours()` 抽象方法（内部接口，非 HTTP API） |
| **内部行为** | 五处消费方从「读实现细节」改为「读接口」；Neo4j 下 KGQA / 相似案例 / GNN 恢复可用 |
| **未改动** | 风险评分模型与指标定义、规则文件、实体 ID 生成、图谱 schema、HTTP 响应模型与字段、CLI 命令 |
| **文档** | `docs/api.md` 的 alerts 章节改为准确描述候选池语义 |
| **测试** | 377 → 382（新增 `TestStoreAgnosticAccess` 5 项） |

---

# 第五轮补充（QA 工程师实跑验证 + 主 Agent 复核）

QA 工程师以「每条发现都必须实跑验证」为标准复查了业务逻辑与边界场景，报出 18 条确认发现并附逐指标等价性比对表。主 Agent 对每条做了独立复核，**全部成立**，本轮修复 6 项。

这一轮的问题与前两轮不同：它们不是「从未生效」，而是**已在生效、但语义是错的**——这类缺陷单测全绿也发现不了，因为测试固化的就是错误的行为。

## 十九、第五轮修复

### S-09 两套指标引擎对「无股东」给出相反结论（相差 60 分）

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/domain/indicators.py` `IND-HLD-001` |
| **等级** | 严重 |
| **影响** | 同一主体在 Neo4j 与内存模式下风险分可差 60 分（权重 0.10，即综合分差 6 分） |

**问题**：Cypher 侧写的是 `coalesce(max(h.percent),0)`，本地侧在无股东时返回 `None`。

`IND-HLD-001` 的方向是 `LOWER_IS_RISKIER`（股权越分散越危险），阈值表首项是 `(0.0, 60.0)`。于是：

- Cypher 侧：无股东 → 0.0 → **60 分**（判定为「极度分散，高风险」）
- 本地侧：无股东 → `None` → **0 分**（判定为「无数据」）

`None` 那一侧才是对的——股东数据缺失不等于股权极度分散。而 Cypher 侧的写法还引入了**类型错误**：`max()` 无匹配时返回 `null`，`null >= date(...)` 在 Neo4j 中抛 `TypeError`，整个查询失败。

**修复**：去掉 `coalesce`，让 Cypher 在无股东时返回 `null`（无行），`GraphIndicatorEngine` 据此返回 `None`，与本地侧一致。

**验证**：`TestShareholdingSemantics`（3 项）。

---

### S-10 千分位金额被截断（差 1000 倍）

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/ingestion/cleaning.py` `parse_amount()` |
| **等级** | 严重 |
| **影响** | 金额小 1000 倍，比率类指标（担保额/注册资本）严重失真 |

**问题**：正则 `-?\d+(?:\.\d+)?` 遇到千分位逗号只匹配第一段：

```
'5,000万元'      -> 5.0        （应为 5000）
'12,345,678元'  -> 0.0012     （应为 1234.5678）
```

**修复**：解析前剥离千分位分隔符。正则用 `(?<=\d),(?=\d{3}(?!\d))`——要求逗号后恰好三位数字且其后不再跟数字，因此 `2024,10` 这类两位的组不会被误剥离，`1,2345` 这类畸形组也不会被静默接受。

**验证**：`TestThousandsSeparators`（8 项）。

---

### S-11 溯源合并的条件写反 —— 已存储的溯源永远丢失

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/ingestion/pipeline.py` `_merge_provenance()` |
| **等级** | 严重 |
| **影响** | 同一主体从多个来源被采集时，先入库的来源记录被后来的采集覆盖丢失 |

**问题**：

```python
known = {已存储项的指纹}          # 从 stored 提取
for item in stored_items(existing):
    if item.fingerprint() not in known:   # ← 恒为 False
        merged.append(item)
```

`known` 恰恰是从 `stored` 提取的，所以每个 `stored` 项的指纹都在 `known` 里，条件永远不成立——**已存储的溯源一条也合不进去**。

**证伪**：

```
合并后溯源条数: 1（预期 2）
  - srcA rec-1        ← 已存储的 srcB/rec-2 消失
```

**修复**：改为按指纹双向去重——保留新到达的、补充存储中独有的。

**验证**：`TestProvenanceMerge`（3 项）。

---

### S-12 事件日期与 `date()` 比较 —— Neo4j 上直接抛类型错误

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/domain/indicators.py`（5 个事件类指标） |
| **等级** | 严重 |
| **影响** | 生产环境下 5 个风险指标（涉诉、强制执行、行政处罚、评级下调、负面舆情）全部查询失败 |

**问题**：`occurredDate` 通过 `node_to_properties()` 存为 **ISO 字符串**（`'2026-06-15'`），而查询写的是 `e.occurredDate >= date($since)`。Neo4j 中字符串与 `date` 比较是类型错误，查询直接失败而非返回错误结果。

**修复**：6 处改为 `date(e.occurredDate) >= date($since)`，显式转换。

**验证**：`TestEventDateComparison`（6 项，静态断言转换存在）+ Neo4j 集成测试 `test_an_event_date_filter_does_not_raise`（CI 中在真实容器上跑）。

---

### S-13 单条畸形关系终止整次摄入

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/ingestion/pipeline.py` `_persist_relations()` |
| **等级** | 严重 |
| **影响** | 一条指向不存在节点的关系会让整批摄入失败，前面已处理的数据全部作废 |

**问题**：批次写入没有异常隔离，`upsert_relations(chunk)` 抛错即终止。

**修复**：批次失败时降级为逐条写入以隔离坏记录。但必须区分两类失败：

- **记录级错误**（端点不存在、类型未知）→ 跳过该条，其余继续
- **存储级错误**（连接失败、查询超时）→ **照常抛出**，绝不掩盖

后者是关键：若不区分，逐条重试会把 Neo4j 宕机伪装成「一批坏数据」。

**验证**：`TestRelationIsolation`（2 项）——分别断言坏记录被隔离、系统故障照常抛出。

---

### B-08 告警指纹包含消息文本 —— 跨轮次去重失效

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/risk/engine.py` `alert_fingerprint()` |
| **等级** | 中 |
| **影响** | 同一告警每轮指纹都变，文档承诺的「跨轮次去重」无法实现 |

**问题**：指纹材料是 `rule_id|entity_id|message`，而 message 内嵌当前分数与路径摘要，每轮都变。

**修复**：指纹只基于告警**身份**（rule_id + entity_id）。同一规则对同一主体只会产生一条告警，因此该组合是唯一键。

**验证**：`TestAlertFingerprint`（2 项）。

---

### B-09 受影响实体按路径重复上报

| 项 | 内容 |
|---|---|
| **文件** | `src/fin_kg/risk/propagation.py` `propagate()` |
| **等级** | 中 |
| **影响** | 同一企业通过两条链条被传染时，在敞口接口里出现两次 |

**问题**：`affected` 按路径逐条追加，而 `best_by_node`（按终点取最强路径）算完后**从未被使用**——是明确的死代码，也说明原设计意图就是去重。

**证伪**（A→B→D、A→C→D 两条链）：

```
路径数: 4 | affected 条数: 4
   B hops=1 score=80.0
   C hops=1 score=80.0
   D hops=2 score=48.0
   D hops=2 score=48.0      ← 重复
```

**修复**：`affected` 改为基于 `best_by_node` 构建，每个受影响实体一条，取最强路径。

**兼容性**：现有测试用集合断言，不受影响。`paths` 列表保持不变（仍保留全部路径），只有 `affected` 去重。

**验证**：`TestAffectedEntitiesAreUnique`（1 项）。

---

## 二十、第五轮待确认项

| 编号 | 项 | 为什么需要确认 |
|---|---|---|
| T-1 | 缺失指标对综合分的稀释 | 10 个指标中若有 3 个无数据，综合分是「按剩余 7 个加权」还是「按 10 个稀释」？当前实现是前者（无数据计 0 分且不重新归一化权重），后者更符合直觉但需要改评分模型 |
| T-2 | `alerts` 用存量风险分排序候选池 | 存量分是上一次运行的结果。若某主体上次分低但本次数据使其变高，它不会进入候选池，因此**不会产生告警**。是否应改为按「上次分 + 数据新鲜度」排序 |
| T-3 | 规则求值异常被吞成「规则未触发」 | `except RuleEvaluationError: continue` 会把「规则本身写错了」与「规则条件不满足」混为一谈。是否需要在响应中区分二者 |
| T-4 | `RiskAssessment.affected_entity_ids` 可能重复 | 与 B-09 同源，但评估对象的字段未一并去重。是否统一处理 |
| T-5 | `Evidence.indicators` 字段被丢弃 | 证据对象定义了指标列表但从不填充，KGQA 的「指标证据」恒为空。补齐需要定义「哪些指标算证据」 |
| T-6 | `neighbours()` 的 N+1 查询 | 相似案例检索对每个候选主体调用一次，10 万实体下是 N+1。是否改为批量邻域查询 |
| T-7 | 规则解析的 AttributeError | 规则 YAML 缺字段时抛的是 `AttributeError` 而非平台错误类，API 会返回 500 而非 422。修正需要新增一个异常类型 |
| T-8 | 风险分持久化的源/目标冗余 | Neo4j 关系的 MERGE 键含 source/target，与关系属性重复。可清理，但属破坏性变更 |

## 二十一、第五轮变更影响范围

| 类别 | 内容 |
|---|---|
| **对外接口（行为变更，已记录）** | `affected` 列表去重（同一实体只出现一次，取最强路径）。`paths` 不变 |
| **对外接口（回归到已声明契约）** | `alert_fingerprint` 的语义回归为「稳定身份」；`IND-HLD-001` 在 Neo4j 下不再误报高风险 |
| **对外接口（无变化）** | 所有 HTTP 路径、请求/响应模型与字段、错误信封、状态码 |
| **内部行为** | 金额解析支持千分位；溯源合并双向去重；事件日期在 Cypher 中显式转换；坏关系被隔离且系统故障不被掩盖 |
| **未改动** | 风险评分模型与权重、指标阈值表、规则文件、实体 ID 生成、图谱 schema、CLI 命令 |
| **测试** | 382 → 407（新增 `tests/unit/test_audit_round4.py` 25 项 + Neo4j 集成 3 项） |

---

# 第六轮排查（2026-11-09，工程基础设施与运行时组件）

- **基线**：`3efe488`，16473 行 Python，407 项测试，覆盖率 82.90%
- **处置后**：457 项测试，覆盖率 83.68%
- **分工**：架构师审 CI / 供应链 / Docker / 迁移 / Makefile / 部署；QA 审规则一致性 / RedisJobStore / 可观测性 / 并发 / CLI / 文档漂移；主 Agent 独立复核并实施
- **审计清单**：架构师 26 条、QA 37 条，全部经主 Agent 实跑复核

这一轮的问题集中在**「系统能跑，但某个环节从来没工作过」**。它们不在异常里、不在日志里，只在把那条链路真正走一遍时才显形。

## 二十二、已修复

### 严重问题

| 编号 | 问题 | 影响 | 修复 |
|---|---|---|---|
| **S-14** | **Docker 镜像无法启动 API 与 worker**：builder 阶段只 `COPY src/fin_kg/__init__.py`，setuptools 的 `packages.find` 因此只识别出顶层包，wheel 里只有 1 个 `.py`；真正的源码又落在 `/app/src`（不在 `sys.path`） | `docker compose up`、`make up`、CI 的 build job、所有镜像部署**全部不可用** | builder 复制完整 `src/`，runtime 从 venv 取包，不再 `COPY src/` |
| **S-01** | **critical 规则 `R-ENF-001` 永不触发**：要求 `IND-ENF-001 >= 50000`，而该指标是「倍数」口径、上限 5.0。同一规则的 description 已写成「倍数异常」，同批迁移的 `R-GUA-001` 已改为 `1.0`——这是漏改 | 强制执行金额异常这条 critical 预警实际是死的 | `value: 50000` → `1.0` |
| **S-17** | **Prometheus `path` 标签用具体 URL**，而 URL 含实体 ID | 每个实体一条时序；10 万实体 = 10 万条时序，抓取比被测服务还慢 | 改用路由模板 `/api/v1/graph/entities/{entity_id}`；未匹配 URL 归入 `unmatched` |
| **S-19** | **`fin_kg_dependency_up` 从未被赋值** | 文档告警规则「图存储不可达」永远拿不到数据 | 在 `/health` 的依赖检查里 `.set(1/0)` |
| **S-20** | **12 个声明指标中 7 个零写入点**，其中 2 个正是文档 §5.2 告警规则依赖的 `fin_kg_ingestion_records_total` 与 `fin_kg_alerts_total` | 「接入停滞」和「预警激增」这两条最该有的业务告警永远静默 | 接入全部 7 个；新增静态扫描守护测试 |
| **S-02** | **`company_industry` 索引在 `schema.py` 中缺失**（磁盘 `V004` 有、Python 定义没有），而 `docs/data-model.md` §6 声明了它 | 按行业筛选在生产上退化为全表扫描 | 补进 `schema.py` |

### 中风险

| 编号 | 问题 | 修复 |
|---|---|---|
| **S-03** | `migrations/` 目录与 `graph/schema.py` 双源真相且已分叉——**没有任何代码读那个目录**，README 与数据模型文档却都指向它 | 新增 `tests/unit/test_schema_consistency.py`（13 项）守护两者一致，并断言文档声明的索引确实被创建 |
| **S-04** | 磁盘 `V005` 的关系索引用有向模式 `-[r]->()`，比 Python 侧的无向模式 `-[r]-()` 覆盖范围小 | 改为无向模式 |
| **S-05** | `docs/data-model.md` 把属性名写成 `guaranteedAmount`，代码全用 `guaranteed_amount`；运维按文档查属性会找不到 | 两份语言版本均改正 |
| **S-08** | `FIN_KG_TUSHARE_TOKEN` 绕过 `Settings` 直读 `os.environ`——不受模型管辖、不脱敏 | 新增 `TushareSettings`（环境变量名不变，部署零影响），token 改为 `SecretStr` |
| **S-09** | `TushareSourceAdapter` 文档称「token 从不存储在实例上」，实际 `self._token = token` 存了 | 改正文档并加 `__repr__` 脱敏 |
| **S-11** | `Makefile` 的 `lint` / `format` 只覆盖 `src tests`，漏了 `scripts/`（CI 上一轮已修，Makefile 未同步） | 补齐 `scripts` |
| **S-13** | CI 5 个 job 全部缺 `timeout-minutes` | 按 job 特性补齐（5/15/10/15/5 分钟） |
| **S-15** | 生产镜像携带完整开发工具链（pytest / ruff / mypy / pytest-cov） | 改 `pip install .`，只装运行时依赖 |
| **S-16** | 镜像 `COPY data/ ./data/` 把本地运行产物 `local-graph.json` 打进生产镜像 | 只复制 `data/samples/` 与 `data/aliases.yaml` |
| **S-21** | 文档让 Prometheus 去抓 `api:9100`，但**没有任何进程监听 9100**——指标实际在 API 的 8000 | 文档、compose、Dockerfile 统一到 8000；`metrics_port` 标注为「预留、当前未接线」 |
| **S-22** | worker 容器继承镜像的 HTTP 健康检查去探 `:8000`，而 worker 不提供 HTTP 服务 | compose 里对该服务 `healthcheck: disable` |
| **S-23** | CI 的 build job 只构建镜像、不运行镜像——**S-14 正是靠这个逃逸的** | 加 `docker run` 烟囱测试，直接 import 三个入口点 |
| **S-26** | 规则加载时不校验指标 ID，拼错会静默加载、永不触发、零日志 | 加载期即抛 `ValidationError` |

### 低风险

| 编号 | 问题 | 修复 |
|---|---|---|
| **S-06** | README 声称「211 通过 / 6 跳过」、单元 184 / API 集成 27 / Neo4j 集成 6，实际已是 407 / 376 / 31 / 9 | 两份语言版本同步更正 |
| **S-07** | README 的项目结构树漏了 4 个文档 | 两份语言版本同步补全 |
| **S-10** | `FIN_KG_INGESTION_ALIAS_FILE` 与 `FIN_KG_GNN_LABEL_SEVERITY` 未在 `.env.example` 声明——部署方无从得知 | 补声明并加测试守护 |
| **S-12** | `Makefile` 头部注释称「lint 跑 ruff 和 mypy」，实际 mypy 在独立的 `typecheck` target | 改正注释 |
| **S-24** | CI 的 build job 只依赖 `lint` 与 `test`，安全扫描失败不阻止出镜像 | 依赖补全为 `[lint, test, security, neo4j-integration]` |
| **S-25** | `FileChannel` 定义了但 `from_settings` 从不挂载，而台账 U-04 声称「已接线」 | 加 `FIN_KG_ALERT_FILE_PATH` 配置项接线（默认关闭），并订正台账 |

## 二十三、待确认项（未擅自实施）

| 编号 | 项 | 为什么需要确认 |
|---|---|---|
| U-1 | `RedisJobStore` 从未接线，compose 的 redis 服务与 6379 端口暴露是装饰性的 | 接线前需先修它的 4 个前置缺陷（键过期、`_read` 吞异常、`create` 竞态、`get` 跨 worker 回退）。单副本部署下它没有价值，多副本下没有它会 404——取决于部署形态 |
| U-2 | 告警零抑制：同一告警每轮重复推送（实测 96 次/天） | 需要状态存储，而 Redis 尚未接线；在没有去重存储前加抑制会丢告警 |
| U-3 | 溯源并发的读-改-写丢更新 | 确定性可复现，但修复要么用 `update_properties` 原子化（依赖 Neo4j），要么引入锁——涉及写入路径的可靠性取舍 |
| U-4 | `get_node()` 返回内部 dict 的活引用 | 调用方的一次赋值即可改坏图谱。改为深拷贝有性能代价，需权衡 |
| U-5 | mypy 类型门禁被 `continue-on-error: true` 永久关闭 | 变成硬门禁前需要知道当前错误规模；本地 3.13 环境因 numpy stub 不兼容无法复现 CI（3.12）的结果 |
| U-6 | 11 处 GitHub Action 使用可变标签，未锁 commit SHA | 供应链加固。锁 SHA 需要定期维护 |
| U-7 | `python-multipart` 是唯一没有上界的运行时依赖 | 需要定一个上界，但也要确认 FastAPI 的实际兼容区间 |
| U-8 | `redis` / `tenacity` / `python-multipart` 三个运行时依赖零引用 | `redis` 应随 U-1 一起处理；另两个取决于是否计划启用相关能力 |
| U-9 | `ensure_schema()` 每次启动执行 22 次数据库往返 | 迁移记账节点没有约束与索引。优化需要改记账模型 |
| U-10 | compose 的 `env_file: .env` 是必需文件，缺失时连 `docker compose config` 都失败 | 是否该改成可选，影响首次部署体验 |
| U-11 | 12 个指标中 8 个从未被赋值（比例类需要真实数据才有意义） | 什么条件下这些指标应该有值，需要业务定义 |
| U-12 | 溯源指纹口径不一致（实现用 `sha256(sourceId\|recordId\|sourceUrl)`，文档与 ADR 写成 `provider`） | 指纹参与去重，改口径要迁移既有数据 |
| U-13 | 评分分布（中位数 / P95 / P99）未暴露 | 需要确认风控是否依赖这些分位值 |

## 二十四、第六轮变更影响范围

| 类别 | 内容 |
|---|---|
| **对外接口（行为变更，已记录）** | `R-ENF-001` 从「永不触发」变为「≥1 倍时触发」；`/metrics` 的 `path` 标签改为路由模板（序列数大幅下降） |
| **对外接口（回归到已声明契约）** | 镜像可启动；Prometheus 抓取目标可用；4 条文档化告警规则全部有数据源；`FileChannel` 按台账声称接上 |
| **对外接口（无变化）** | 所有 HTTP 路径、请求/响应模型与字段、错误信封、状态码、CLI 命令 |
| **内部行为** | 规则加载期校验指标 ID；密钥统一走 Settings；摄入 / 评分 / 传导 / 图规模写入指标 |
| **未改动** | 风险评分模型与权重、指标阈值表、实体 ID 生成、图谱 schema 的约束集合、Neo4j 写入路径 |
| **配置** | 新增 `FIN_KG_ALERT_FILE_PATH`（可选，缺省关闭）；`FIN_KG_METRICS_PORT` 标注为预留 |
| **测试** | 407 → 457（新增 `test_audit_round6.py` 23 项、`test_schema_consistency.py` 13 项、`test_metric_writers.py` 14 项） |
