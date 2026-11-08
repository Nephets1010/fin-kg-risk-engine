# ADR-0008：CI 流水线与质量门禁

- **状态**：已接受
- **日期**：2026-10-29

## 背景

作品集项目的价值有一半来自「别人能跑起来、看得懂、验证过」。一个只在自己机器上能跑、没有测试、没有 CI 的仓库，在面试现场是无法自证的。

同时要防止一个常见的翻车点：**开源仓库里混进了凭证**。

## 决策

### CI 流水线结构

`.github/workflows/ci.yml` 定义 5 个 job：

| Job | 触发条件 | 内容 |
|---|---|---|
| `lint` | 始终 | ruff check + ruff format --check + mypy（后者 `continue-on-error`，因为渐进式收紧更实际） |
| `test` | 始终 | 在 Python 3.10 / 3.11 / 3.12 上跑全部测试 + 覆盖率 |
| `neo4j-integration` | 始终 | 用 service container 起 Neo4j 5.26，跑真实图库集成测试 |
| `build` | lint + test 通过后 | 构建容器镜像（不推送，用 GHA 缓存） |
| `security` | 始终 | 密钥正则扫描 + 检查 `.env` 未被追踪 |

### 依赖矩阵

Python 3.10 ~ 3.12，因为 `pyproject.toml` 声明 `requires-python = ">=3.10"`，企业环境里 3.10 仍很常见。多版本测试捕获的是「用了 3.11 才有的语法特性」这类问题。

### 覆盖率门槛

`pyproject.toml` 中 `fail_under = 70`。当前实际覆盖率高于此值。门槛设在 70% 而非 90%，是因为有几类代码天然难以覆盖：Neo4j 驱动的异常分支、外部 API 的错误处理。设一个够不着的门槛只会让人去写无意义的测试。

### 分层测试策略

- **单元测试**（`tests/unit/`）：纯逻辑，不需要任何外部服务。CI 的 `test` job 不起容器，10 秒内跑完 200+ 用例。
- **集成测试**（`tests/integration/`）：HTTP 全链路，用内存图库，仍不需要容器。
- **Neo4j 集成测试**（`tests/integration/test_neo4j_integration.py`）：需要真实 Neo4j，用 service container。环境变量未配置时**自动 skip**，因此本地开发不被阻塞。

这是 ADR-0002 分层架构的直接收益：绝大多数测试不依赖基础设施。

### 安全门禁

`security` job 用正则匹配常见密钥格式（`sk-`、`ghp_`、`AKIA`、PEM 私钥头），命中即失败。并显式检查 `.env` 不在版本控制中。

### 容器构建

多阶段构建：builder 阶段装依赖到 `/opt/venv`，runtime 阶段只拷贝 venv 与源码。运行用户为非 root（uid 10001）。镜像内不含 `.env`。

## 后果

**正面**

- 每个 PR 都得到「能跑吗、测试过吗、干净吗、有没有泄密」四个答案。
- 多 Python 版本覆盖了企业环境的实际差异。
- 容器镜像每次 PR 都构建，Dockerfile 不会腐化。
- 密钥扫描在推送时就拦住，而不是等到被发现。

**负面 / 代价**

- CI 需要 Neo4j service container，runner 每次多花约 1 分钟。
- `mypy` 暂时非阻断（`continue-on-error`），意味着类型问题不会拦住合并。这是刻意的权衡：先把类型标注做起来，再收紧门槛。
- 覆盖率门槛需要持续维护，指标目录每加一个指标就要补测试。

## 验证方式

- 本地：`make test` 与 CI 的 `test` job 跑同一套命令。
- CI 徽章在 README 首屏，状态即质量信号。
- `security` job 的两个检查可本地手动复现（命令写在 workflow 里）。

## 撤销条件

当 `mypy` 错误数清零且新增代码 100% 标注时，把 `mypy` 改为阻断式，并考虑把覆盖率门槛提到 80%。
