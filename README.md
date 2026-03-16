# Forum Memory Agent — Backend

知识论坛 + 记忆系统后端，基于 FastAPI + SQLModel + PostgreSQL（同步模式）。

## 快速启动

```bash
# 1. 安装依赖
pip install -e ".[dev]"

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，设置数据库连接和 LLM API Key

# 3. 启动 API 服务
uvicorn forum_memory.main:app --reload --port 8000

# 4. 启动 Dagster 调度（自动提取 + 生命周期管理）
dagster dev

# 5. 批量回填已有记忆到 ES（首次启用 ES 时运行）
python -m forum_memory.scripts.reindex_memories
```

## 技术栈

- **FastAPI** — Web 框架
- **SQLModel** ≥0.0.22 — ORM（基于 SQLAlchemy 2.0）
- **PostgreSQL** + psycopg2-binary — 数据源
- **Elasticsearch** ≥8.12 — 向量混合检索（BM25 + knn）
- **Dagster** — 异步任务编排（知识提取、生命周期自动化）
- **Pydantic v2** — 数据校验
- **PyJWT** — JWT 认证
- **slowapi** — 请求限流
- **LLM Provider** — 支持 OpenAI 和自定义 HTTP API（CustomProvider）

## 项目结构

```
forum_memory/
├── main.py                 # FastAPI 入口（启动时自动初始化 ES 索引）
├── config.py               # 配置（Pydantic Settings，env_prefix=FM_）
├── database.py             # 同步 Engine + Session
├── seed.py                 # 数据库初始化种子数据
├── models/                 # SQLModel 数据模型
│   ├── enums.py            # 所有枚举（ThreadStatus, MemoryStatus, Authority...）
│   ├── base.py             # UUID + Timestamp Mixin
│   ├── user.py / namespace.py / thread.py / memory.py
│   ├── namespace_moderator.py  # 板块管理员关联
│   ├── vote.py             # 投票记录
│   ├── extraction.py / feedback.py / operation_log.py / event.py
├── schemas/                # Pydantic 请求/响应模型
│   ├── admin.py / feedback.py / memory.py / namespace.py / thread.py / user.py
├── core/                   # 核心业务逻辑引擎
│   ├── state_machine.py    # 帖子状态机 + 权威映射
│   ├── quality.py          # 五因子质量评分
│   ├── audn.py             # AUDN 决策解析（Add/Update/Delete/None）
│   ├── extraction.py       # 提取辅助逻辑
│   ├── auth.py             # JWT Token 创建与验证
│   ├── background.py       # 后台线程池（fire-and-forget 任务）
│   └── prompts.py          # 所有 LLM Prompt 模板
├── providers/              # LLM 提供商抽象
│   ├── base.py             # 抽象基类（complete/embed/rerank）
│   ├── openai_provider.py  # OpenAI 实现
│   ├── custom_provider.py  # 自定义 HTTP API 实现
│   └── factory.py          # Provider 工厂
├── services/               # 业务服务层
│   ├── namespace_service.py / thread_service.py
│   ├── memory_service.py   # 含生命周期批量操作
│   ├── feedback_service.py
│   ├── search_service.py   # ES 混合检索 + LLM 查询重写 + Rerank
│   ├── extraction_service.py
│   ├── rag_service.py      # 外部 RAG 知识库查询
│   └── es_service.py       # Elasticsearch 客户端和索引管理
├── dagster/                # Dagster 任务编排
│   ├── definitions.py      # Dagster Definitions 入口
│   ├── assets.py           # 6 个 Job（提取/超时/生命周期/质量刷新/ES修复/评论计数）
│   ├── sensors.py          # 6 个 Sensor（事件驱动 + 定时调度）
│   └── resources.py        # DB Resource
├── scripts/                # 运维脚本
│   ├── reindex_memories.py     # ES 批量回填
│   ├── backfill_es_indices.py  # ES 索引批量创建
│   ├── fix_es_index_names.py   # 修复索引名映射
│   ├── import_topics.py        # 历史帖子批量导入
│   ├── migrate_add_indexed_at.py   # 迁移：添加 indexed_at 字段
│   ├── migrate_add_rag_context.py  # 迁移：添加 RAG 上下文字段
│   └── migrate_board_admin.py      # 迁移：添加板块管理员
├── uploads/                # 用户上传文件存储目录
└── api/                    # FastAPI 路由
    ├── deps.py             # 依赖注入（认证、权限检查）
    ├── rate_limit.py       # 请求限流配置
    ├── auth.py             # 认证路由（登录）
    ├── users.py            # 用户管理路由
    ├── namespaces.py       # 板块路由
    ├── threads.py          # 帖子路由
    ├── memories.py         # 记忆路由
    ├── feedback.py         # 反馈路由
    ├── admin.py            # 管理员路由（导入/质量告警/审计）
    └── uploads.py          # 文件上传路由
```

## API 端点

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/login` | 登录获取 JWT Token |

### 用户

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/users/me` | 当前用户信息 |
| GET | `/api/v1/users/me/managed-namespaces` | 当前用户管理的板块 |
| GET | `/api/v1/users` | 用户列表 |
| POST | `/api/v1/users` | 创建用户 |
| DELETE | `/api/v1/users/:id` | 删除用户 |

### 板块

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/v1/namespaces` | 板块列表 / 创建 |
| GET/PUT | `/api/v1/namespaces/:id` | 板块详情 / 更新 |
| DELETE | `/api/v1/namespaces/:id` | 板块软删除 |
| GET | `/api/v1/namespaces/:id/stats` | 板块统计 |
| GET | `/api/v1/namespaces/stats/aggregate` | 全局聚合统计 |
| PUT | `/api/v1/namespaces/:id/dictionary` | 黑话字典更新 |
| GET/POST | `/api/v1/namespaces/:id/moderators` | 板块管理员列表 / 添加 |
| DELETE | `/api/v1/namespaces/:id/moderators/:uid` | 移除板块管理员 |

### 帖子

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/v1/threads` | 帖子列表 / 创建 |
| GET | `/api/v1/threads/:id` | 帖子详情 |
| DELETE | `/api/v1/threads/:id` | 帖子软删除 |
| POST | `/api/v1/threads/:id/resolve` | 采纳关闭 |
| POST | `/api/v1/threads/:id/timeout-close` | 超时关闭 |
| POST | `/api/v1/threads/:id/ai-answer` | AI 自动回答 |
| GET | `/api/v1/threads/:id/ai-answer/stream` | AI 回答 SSE 流 |
| GET/POST | `/api/v1/threads/:id/comments` | 评论列表 / 添加 |
| DELETE | `/api/v1/threads/:id/comments/:cid` | 删除评论 |
| POST | `/api/v1/threads/:id/comments/:cid/upvote` | 评论点赞 |

### 记忆

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/v1/memories` | 记忆列表 / 创建 |
| POST | `/api/v1/memories/batch` | 批量创建记忆 |
| GET | `/api/v1/memories/tags` | 所有标签列表 |
| GET/PUT | `/api/v1/memories/:id` | 记忆详情 / 更新 |
| DELETE | `/api/v1/memories/:id` | 记忆软删除 |
| PUT | `/api/v1/memories/:id/restore` | 恢复已删除记忆 |
| PUT | `/api/v1/memories/:id/authority` | 权威等级变更 |
| POST | `/api/v1/memories/search` | 记忆搜索（ES 混合检索） |
| POST | `/api/v1/memories/extract/:thread_id` | 触发知识提取 |

### 反馈

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/memories/:id/feedback` | 提交反馈 |
| DELETE | `/api/v1/memories/:id/feedback` | 撤销反馈 |
| GET | `/api/v1/memories/:id/feedback` | 反馈列表 |
| GET | `/api/v1/memories/:id/feedback/summary` | 反馈汇总 |

### 管理员

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/admin/import-topics` | JSON 批量导入帖子 |
| POST | `/api/v1/admin/import-topics/upload` | ZIP/JSON 文件上传导入（异步） |
| GET | `/api/v1/admin/import-jobs/:id` | 导入任务状态查询 |
| GET | `/api/v1/admin/quality-alerts` | 质量告警列表 |
| POST | `/api/v1/admin/quality-alerts/:mid/dismiss` | 忽略质量告警 |
| GET | `/api/v1/admin/audit-logs` | 审计日志查询 |

### 文件上传

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/uploads` | 上传图片文件 |

## Dagster 编排

启动：`dagster dev`（workspace.yaml 已声明入口模块）

| Sensor | 触发频率 | Job | 说明 |
|--------|---------|-----|------|
| `source_extraction_sensor` | 30 秒 | `extract_memories_job` | 监听所有已注册来源的关闭事件，自动提取知识 |
| `thread_timeout_sensor` | 1 小时 | `timeout_threads_job` | 超时关闭超过 N 天的 OPEN 帖子 |
| `memory_lifecycle_sensor` | 1 天 | `lifecycle_memories_job` | ACTIVE→COLD（180天）、COLD→ARCHIVED（365天） |
| `quality_refresh_sensor` | 1 天 | `refresh_quality_job` | 刷新所有 ACTIVE 记忆的质量评分 |
| `es_sync_repair_sensor` | 10 分钟 | `repair_es_sync_job` | 修复 DB-ES 一致性（indexed_at IS NULL 的记忆重新索引） |
| `comment_count_reconcile_sensor` | 1 天 | `reconcile_comment_counts_job` | 修正帖子评论计数偏移 |

## 配置说明

所有配置通过环境变量设置，前缀 `FM_`，或写入 `.env` 文件。

### 数据库

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_DATABASE_URL` | （必填） | PostgreSQL 连接串 |
| `FM_DATABASE_ECHO` | `false` | 打印 SQL 日志 |

### Elasticsearch

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_ES_URL` | `http://localhost:9200` | ES 地址 |
| `FM_ES_ENABLED` | `true` | 总开关，关闭后回退到 SQL 搜索 |
| `FM_ES_INDEX_PREFIX` | `forum_memory` | 索引名前缀 |
| `FM_ES_USERNAME` / `FM_ES_PASSWORD` | 空 | ES 认证 |
| `FM_ES_VERIFY_CERTS` | `true` | 是否验证 ES 证书 |
| `FM_ES_KNN_NUM_CANDIDATES` | `100` | KNN 候选数量 |

### LLM Provider

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_LLM_PROVIDER` | `openai` | 提供商：`openai` 或 `custom` |
| `FM_LLM_API_KEY` | 空 | OpenAI API Key |
| `FM_LLM_MAIN_MODEL` | `gpt-4o` | 主模型名 |
| `FM_LLM_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding 模型名 |
| `FM_EMBEDDING_DIMENSION` | `1536` | 向量维度（custom 建议设为 1024） |
| `FM_LLM_TIMEOUT` | `60` | LLM 请求超时秒数 |

### Custom Provider（当 `FM_LLM_PROVIDER=custom`）

| 变量 | 说明 |
|------|------|
| `FM_CUSTOM_LLM_URL` | LLM 接口地址（OpenAI 兼容格式） |
| `FM_CUSTOM_EMBED_URL` | Embedding 接口地址 |
| `FM_CUSTOM_RERANK_URL` | Rerank 接口地址 |
| `FM_CUSTOM_API_KEY` | 自定义 API Key |
| `FM_CUSTOM_LLM_MODEL` / `FM_CUSTOM_EMBED_MODEL` / `FM_CUSTOM_RERANK_MODEL` | 模型名 |

### JWT 认证

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_JWT_ENABLED` | `false` | 启用 JWT 认证（关闭时使用 X-Employee-Id 头） |
| `FM_JWT_SECRET_KEY` | 空 | JWT 密钥（启用时必填） |
| `FM_JWT_ALGORITHM` | `HS256` | JWT 算法 |
| `FM_JWT_EXPIRE_HOURS` | `24` | Token 过期时间（小时） |

### RAG 知识库

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_RAG_BASE_URL` | 空 | 外部 RAG API 地址 |
| `FM_RAG_TIMEOUT` | `30` | RAG 请求超时秒数 |

### 文件上传

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_UPLOAD_DIR` | `uploads` | 上传文件存储路径 |
| `FM_UPLOAD_MAX_SIZE_MB` | `5` | 单文件最大尺寸（MB） |

### 生命周期与质量阈值

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_THREAD_TIMEOUT_DAYS` | `7` | 帖子超时关闭天数 |
| `FM_COLD_INACTIVE_DAYS` | `180` | 记忆冷冻天数 |
| `FM_ARCHIVE_INACTIVE_DAYS` | `365` | 记忆归档天数 |
| `FM_WRONG_FEEDBACK_THRESHOLD` | `3` | 质量惩罚阈值 |
| `FM_PROMOTE_USEFUL_RATIO` | `0.8` | 自动提升权威的有用率门槛 |
| `FM_PROMOTE_MIN_FEEDBACK` | `10` | 自动提升所需最低反馈数 |

## 核心概念

### 记忆生命周期

```
ACTIVE ──(180天不活跃)──→ COLD ──(365天不活跃)──→ ARCHIVED
   │
   └──(软删除)──→ DELETED
```

- **ACTIVE**：正常参与搜索和检索
- **COLD**：从 ES 索引移除，不再参与搜索，可手动恢复
- **ARCHIVED**：长期归档
- **DELETED**：软删除

### 帖子状态机

```
OPEN ──→ RESOLVED（人工/AI采纳）
  │
  ├──→ TIMEOUT_CLOSED（自动超时）
  │
  └──→ DELETED（软删除）
```

### 权威等级

- **LOCKED**：人工确认，AUDN 不可修改/删除
- **NORMAL**：系统可自动更新/去重

### 质量评分（五因子）

`0.35×有用率 + 0.20×来源权重 + 0.15×检索热度 + 0.15×新鲜度 + 0.15×(1-惩罚)`
