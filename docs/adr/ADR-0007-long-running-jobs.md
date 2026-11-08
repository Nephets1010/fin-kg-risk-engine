# ADR-0007：长任务接口 —— Init / Step / Poll

- **状态**：已接受
- **日期**：2026-10-29

## 背景

数据接入与全图重算在生产规模下需要分钟到小时。HTTP 请求无法承载：

- 网关与客户端都有超时，长连接会被中断。
- 请求线程被占用，吞吐直接受损。
- 客户端断开后，服务端无法知道任务是否该继续。

## 决策

**长任务一律走 Init → Step → Poll 三段式，不使用同步 HTTP，也不使用 WebSocket。**

### 1. Init

```
POST /api/v1/jobs/ingest  {"source": "synthetic"}
→ 202 Accepted
  {"jobId": "a1b2...", "jobType": "ingest", "status": "pending", ...}
```

立即返回，任务在后台线程执行。

### 2. Step（状态轮询）

```
GET /api/v1/jobs/{jobId}
→ {"status": "running", "progress": 12000, "total": 50000, ...}
```

### 3. Poll（结果获取）

```
GET /api/v1/jobs/{jobId}/result
→ {"fetched": 50000, "nodesWritten": 50000, "relationsWritten": 120000, ...}
```

### 为什么不用 WebSocket

- 部署复杂度：WebSocket 需要额外的连接管理与负载均衡配置。
- 任务状态是**低频变化**的，轮询完全够用。
- 轮询可以走 CDN、可以被代理缓存、可以被任意 HTTP 客户端消费。

### 为什么不用消息队列做任务分发

Redis Stream 已经作为事件流存在于技术选型里，但把任务投递也压在队列上会带来「任务丢了但 API 返回了 202」的窗口。**同步创建任务记录、异步执行**的语义更简单，且失败可观测（任务记录里有 `error` 字段）。

### 任务状态存储

- `JobStore`（内存 + 锁）：默认实现，单进程部署够用。
- `RedisJobStore`：多 worker 部署时使用，状态写入 Redis，任何 worker 都能服务轮询请求。

选择依据由部署形态决定，而非配置项 —— 单机 Compose 用内存版，多副本用 Redis 版。

### 幂等与断点续跑

数据接入管线本身有 checkpoint 机制（`data/checkpoints/<source>.json`），记录已处理的游标与写入量。任务中断后重新触发，`resume=True`（默认）会跳过已完成的源，从断点继续。

## 后果

**正面**

- 客户端实现简单：提交 → 轮询 → 取结果，标准 HTTP 即可。
- 任务失败可观测：错误信息存在任务记录里，而不是消失在超时里。
- 无需引入长连接运维负担。

**负面 / 代价**

- 轮询有延迟：客户端需要设置合理的轮询间隔（建议 2~5 秒）。
- 内存版 JobStore 在多 worker 部署下会丢状态，因此生产多副本必须切到 Redis 版。
- 任务没有取消接口（当前只有 `cancelled` 状态字段，尚无触发路径）。

## 验证方式

- `tests/integration/test_api.py::TestJobEndpoints`：提交返回 202、状态可轮询、未知任务返回 404、结果接口。
- `tests/unit/test_ingestion.py::TestPipeline::test_resume_skips_completed_source`：断点续跑语义。

## 撤销条件

当任务量级需要跨机的优先级队列与公平调度时，把任务分发改为 Redis Stream 消费组，保留现有 HTTP 契约不变。
