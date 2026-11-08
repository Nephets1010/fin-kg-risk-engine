# ADR-0006：安全基线 —— 密钥不硬编码与写入白名单

- **状态**：已接受
- **日期**：2026-10-29

## 背景

这是一个会处理真实金融数据的系统。两个具体的技术风险：

1. **密钥泄漏**：Neo4j 密码、LLM API Key 被写进源码或提交进 Git。
2. **Cypher 注入**：Cypher 不支持参数化标签与关系类型，如果直接拼接用户输入，可能产生非预期的写入或读取。

## 决策

### 1. 密钥只从环境读取，且**没有默认值**

```python
class Neo4jSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FIN_KG_NEO4J_")
    password: SecretStr = SecretStr("")   # 空，不是 "neo4j"
```

- 所有敏感字段使用 `SecretStr`，`repr` 与日志中显示为 `**********`。
- `.env` 被 `.gitignore` 排除，仓库里只有 `.env.example`。
- **生产环境启动即校验**：`Settings` 的 `_enforce_production_secrets` 在 `env == "production"` 且 Neo4j 密码为空时直接抛错，不允许「先起来再说」。
- **日志脱敏**：`register_secret_filter` 把配置中的密钥注册进脱敏表，`JsonFormatter` 与 `ConsoleFormatter` 在输出前统一替换为 `***`。
- **CI 密钥扫描**：workflow 中的 `security` job 用正则扫描提交历史中的常见密钥模式，并检查 `.env` 未被追踪。

### 2. 标签与关系类型写入**白名单**

Cypher 不能参数化标签，因此必须拼接。为避免注入：

```python
ALLOWED_LABELS = frozenset({"Entity", "Company", "Person", "Event", "Product"})
ALLOWED_RELATION_TYPES = frozenset(r.value for r in RelationType)
```

写入前逐一校验，不在白名单内的标签/类型直接抛 `GraphStoreError`，**绝不进入查询字符串**。白名单与领域模型同源（`RelationType` 枚举），不会漂移；`test_whitelist_matches_the_domain` 守护这一点。

### 3. 用户输入永远不进 Cypher

所有面向用户的查询走命名参数（`$id`、`$since`、`$limit`）。Cypher 库（`fin_kg/graph/queries.py`）中的所有语句都是常量，参数化占位符只出现在参数里。

### 4. LLM 不能生成可执行 Cypher

KGQA 采用**意图分类 + 模板**而非自由文本转 Cypher：

- 问题先被分类到 8 个受支持的意图之一（`QueryIntent`）。
- 每个意图有固定的查询模板与参数。
- LLM（若启用）只用于**把已取回的事实润色成自然语言**，以及**消歧意图**。它从不接收 Cypher 生成任务，也从不执行查询。

好处：一条畸形问题最多得到「无法识别该问题类型」的提示，不会产生一条语法错误或越权的查询。

### 5. API 鉴权可选但可用

`FIN_KG_API_KEY` 设置后，所有请求需带 `X-API-Key` 头。未设置时不鉴权（便于本地开发），生产环境应在部署层强制设置。

### 6. 错误信息不泄漏内部细节

对外的错误信封只有 `code` / `message` / `details`（业务字段）。异常堆栈、内部 URL、原始驱动响应只进日志。

## 后果

**正面**

- 仓库里不存在任何可用凭证，克隆即可运行（开发模式）。
- 注入面被结构性消除，而不是靠转义。
- LLM 失效不影响系统可用性（模板降级）。

**负面 / 代价**

- 白名单机制要求新增关系类型时同步更新领域枚举 —— 略显繁琐，但这是刻意的摩擦。
- 生产环境启动即失败，需要运维先把密钥配好。
- 意图分类是有限集合，超出范围的问题无法回答。

## 验证方式

- `tests/unit/test_core.py::TestSettings`：生产环境缺密码必须报错；密钥脱敏生效。
- `tests/unit/test_graph.py::TestWhitelists`：白名单与领域模型一致。
- CI `security` job：密钥正则扫描 + `.env` 未追踪检查。
- `tests/unit/test_research.py`：意图分类覆盖 7 类问题 + 未知问题。

## 撤销条件

当平台需要接入更多关系类型时，扩展 `RelationType` 枚举即可 —— 白名单会自动跟随，因为它是同源的。
