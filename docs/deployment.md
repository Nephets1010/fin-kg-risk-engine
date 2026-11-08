# 部署与运维

---

## 1. 三种部署形态

| 形态 | 场景 | 命令 |
|---|---|---|
| **本地零依赖** | 学习、演示、快速验证 | `pip install -e ".[dev]"` → `fin-kg seed` |
| **Docker Compose** | 单机部署、答辩演示、团队内网 | `docker compose up -d --build` |
| **生产** | 多副本、高可用 | Compose 起依赖 + K8s/编排起 API 与 worker |

无论哪种形态，**密钥只从环境读取**。

---

## 2. Docker Compose（推荐起点）

### 2.1 启动

```bash
git clone https://github.com/Nephets1010/fin-kg-risk-engine.git
cd fin-kg-risk-engine

cp .env.example .env
# 编辑 .env，至少设置：
#   FIN_KG_NEO4J_PASSWORD=<一个强密码>

docker compose up -d --build
docker compose ps          # 等 neo4j / redis 变为 healthy
```

### 2.2 初始化

```bash
docker compose exec api fin-kg migrate              # 应用图谱 schema
docker compose exec api fin-kg ingest --source csv  # 载入示例数据
docker compose exec api fin-kg score --limit 10      # 看风险分
```

### 2.3 访问

| 服务 | 地址 | 说明 |
|---|---|---|
| API | http://localhost:8000 | |
| Swagger UI | http://localhost:8000/docs | |
| ReDoc | http://localhost:8000/redoc | |
| Prometheus 指标 | http://localhost:9100/metrics | |
| Neo4j 浏览器 | http://localhost:7474 | 账号见 `.env` |
| Bolt | bolt://localhost:7687 | |

### 2.4 服务组成

| 服务 | 作用 | 是否需要多副本 |
|---|---|---|
| `neo4j` | 图存储 | 否（Community 单实例） |
| `redis` | 事件流 / 任务状态 | 否 |
| `api` | HTTP 服务 | **是**（无状态，可水平扩展） |
| `worker` | 定时任务 | **否**（单例，否则任务会跑两遍） |

### 2.5 常用运维命令

```bash
docker compose logs -f api worker    # 跟踪日志
docker compose restart api           # 重启 API
docker compose down                  # 停止（保留数据卷）
docker compose down -v               # 停止并删除数据卷
docker compose exec api pytest tests # 在容器里跑测试
```

---

## 3. 配置

全部配置项见 [`.env.example`](../.env.example)。按前缀分组：

| 前缀 | 变量 | 说明 |
|---|---|---|
| `FIN_KG_` | `ENV` `LOG_LEVEL` `LOG_FORMAT` `API_HOST` `API_PORT` `WORKERS` `API_KEY` `CORS_ORIGINS` | 应用级 |
| `FIN_KG_NEO4J_` | `URI` `USER` `PASSWORD` `DATABASE` `MAX_CONNECTION_POOL` `CONNECTION_TIMEOUT` `BATCH_SIZE` | 图存储 |
| `FIN_KG_REDIS_` | `URL` `PASSWORD` `STREAM_MAXLEN` | 队列 |
| `FIN_KG_RISK_` | `MAX_HOPS` `DECAY` `ALERT_THRESHOLD` `CRITICAL_THRESHOLD` `RULES_PATH` | 风险引擎 |
| `FIN_KG_INGESTION_` | `BATCH_SIZE` `DATA_DIR` `CHECKPOINT_DIR` | 接入 |
| `FIN_KG_LLM_` | `ENABLED` `API_BASE` `API_KEY` `MODEL` `TIMEOUT` | 可选 LLM |
| `FIN_KG_` | `METRICS_ENABLED` `METRICS_PORT` `ALERT_WEBHOOK_URL` | 监控 |
| `FIN_KG_` | `SCHEDULER_ENABLED` `TIMEZONE` `JOB_*_CRON` | 调度 |

### 生产必填

```bash
FIN_KG_ENV=production
FIN_KG_NEO4J_PASSWORD=<强密码>       # 缺失或为开发默认值时服务拒绝启动
FIN_KG_API_KEY=<随机串>              # 开启接口鉴权
```

**生产模式下若 `FIN_KG_NEO4J_PASSWORD` 为空，或等于仓库自带的开发默认值
（`fin-kg-dev-password` / `neo4j` / `password` / `changeme` / `secret`），服务在启动阶段直接抛错**
—— 不允许「先起来再说」。这是刻意的：图存储缺失是一个部署错误，必须立刻可见，而不是静默服务一个空图；
而 compose 文件为了让本地 `docker compose up` 开箱即用而内置的默认密码，绝不能被带进生产。

### 密钥管理

| 场景 | 做法 |
|---|---|
| 单机 | `.env` 文件（权限 `chmod 600`），`.gitignore` 已排除 |
| Docker Compose | `.env` + `docker compose --env-file` |
| Kubernetes | Secret 挂载为环境变量，或用外部密钥管理（Vault / 云厂商 KMS） |
| CI | GitHub Actions Secrets，CI 中**不出现明文** |

密钥在日志中被自动脱敏：启动时所有已配置的密钥注册进脱敏表，JSON 与控制台日志输出前统一替换为 `***`。

---

## 4. 资源与容量

### 4.1 基准配置

| 组件 | 最小 | 推荐（10 万实体 / 50 万关系） |
|---|---|---|
| Neo4j | 2 vCPU / 4 GB | 4 vCPU / 8 GB（heap 2G，pagecache 4G） |
| Redis | 1 vCPU / 512 MB | 2 vCPU / 2 GB |
| API | 1 vCPU / 512 MB | 2 vCPU / 2 GB（4 workers） |
| worker | 1 vCPU / 1 GB | 2 vCPU / 2 GB |

`docker-compose.yml` 中的 Neo4j 配置：heap 1G、pagecache 512M。生产建议按上表调大。

### 4.2 导入吞吐

导入是批量 `MERGE`。瓶颈通常在 Neo4j 的写入而非 Python。

调优项：

| 变量 | 效果 |
|---|---|
| `FIN_KG_NEO4J_BATCH_SIZE` | 单批行数。默认 5000；内存紧张时降到 1000，网络延迟高时提到 10000 |
| `FIN_KG_INGESTION_BATCH_SIZE` | 客户端批次大小。默认 2000 |
| `FIN_KG_NEO4J_MAX_CONNECTION_POOL` | 连接池大小。默认 50 |

### 4.3 压测

```bash
python scripts/benchmark.py --companies 100000 --hops 3
```

输出：导入耗时、节点/关系数、k 跳遍历 P50/P95、风险评分耗时。

---

## 5. 监控

### 5.1 Prometheus

```yaml
scrape_configs:
  - job_name: fin-kg
    static_configs:
      - targets: ["api:9100"]
```

### 5.2 建议告警

| 告警 | 条件 | 含义 |
|---|---|---|
| API 不可用 | `up == 0` 持续 2 分钟 | 服务挂了 |
| 错误率升高 | `rate(fin_kg_http_requests_total{status=~"5.."}[5m]) > 0.05` | 有系统性错误 |
| 图存储不可达 | `fin_kg_dependency_up{name="graph_store"} == 0` | Neo4j 断了 |
| 接入停滞 | 两次 `fin_kg_ingestion_records_total` 增量在 30 分钟内为 0 | 数据没进来 |
| 预警激增 | `rate(fin_kg_alerts_total[1h])` 突增 3 倍 | 可能是一次真实风险事件，也可能是数据源异常 |

### 5.3 业务告警

高等级预警通过 `AlertDispatcher` 分发：

- **控制台** —— 始终开启，作为兜底
- **Webhook** —— 配置 `FIN_KG_ALERT_WEBHOOK_URL` 后启用，可对接企业微信 / 钉钉 / Slack
- **文件** —— `FileChannel`，追加 JSON Lines，用于审计与回放

---

## 6. 任务调度

| 任务 | 默认 cron | 作用 |
|---|---|---|
| `ingest` | `0 2 * * *` | 拉取最新数据 |
| `rescore` | `0 */6 * * *` | 重算全图风险分 |
| `propagate` | `*/15 * * * *` | 重算传导并推送预警 |
| `research` | `0 3 * * *` | 刷新高风险主体的投研材料 |

调度器是**单例** —— 两个 worker 会让每个任务跑两遍。扩容时保持 worker 为 1 个副本。

修改 cron：改环境变量 `FIN_KG_JOB_INGEST_CRON` 等，重启 worker 生效。

---

## 7. 升级与回滚

### 7.1 图谱 schema 迁移

```bash
fin-kg migrate
```

迁移是**幂等**的：所有语句带 `IF NOT EXISTS`，重复执行无副作用。迁移版本记录在 `:SchemaMigration` 节点上。

回滚：手工执行反向 Cypher。当前迁移均为新增约束与索引，**不破坏既有数据**，因此通常无需回滚。

### 7.2 应用升级

```bash
git pull
docker compose build
docker compose up -d
```

API 是无状态的，滚动重启不影响数据。

### 7.3 规则回滚

规则是 YAML，直接改 `rules/risk_rules.yaml` 并重启即可。规则 ID 保持稳定，历史告警仍可追溯到当时的规则版本。

---

## 8. 故障排查

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 启动报 `Neo4j is unavailable in production` | 未设置 `FIN_KG_NEO4J_PASSWORD` | 补上密钥 |
| 启动报 `Neo4j is unavailable (…); falling back` | Neo4j 未就绪 | `docker compose ps` 确认 healthy；开发模式会自动降级到本地文件图 |
| 指标读数全是 0 | 图谱为空 | 先 `fin-kg ingest --source csv` |
| 风险分不更新 | 调度器未运行 | `docker compose logs worker` |
| `fin-kg score` 报找不到实体 | 本地文件图为空 | `fin-kg seed` 或 `fin-kg ingest --source csv` |
| 401 | 设置了 `FIN_KG_API_KEY` 但请求未带 `X-API-Key` | 补上请求头 |
| 内存持续增长 | Neo4j heap 不足 | 调大 `NEO4J_server_memory_heap_max__size` |
| 导入很慢 | 批量太小或索引未生效 | 调大 `BATCH_SIZE`；确认 `fin-kg migrate` 已执行 |

---

## 9. 安全清单

上线前逐项确认：

- [ ] `FIN_KG_ENV=production`
- [ ] `FIN_KG_NEO4J_PASSWORD` 已设置且非默认值
- [ ] `FIN_KG_API_KEY` 已设置
- [ ] `.env` 权限为 `600`，且未被提交到版本控制
- [ ] Neo4j 端口（7474 / 7687）未暴露到公网，或已加访问控制
- [ ] Redis 已设置密码（`requirepass`）
- [ ] 若启用 LLM，`FIN_KG_LLM_API_KEY` 来自密钥管理而非代码库
- [ ] 数据源使用公开或已授权来源，遵守数据版权
- [ ] `fin-kg ask` 的输出仅供内部参考，不直接对外发布
