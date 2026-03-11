# Forum Memory Agent 用户手册

## 目录

1. [系统简介](#1-系统简介)
2. [快速开始](#2-快速开始)
3. [核心概念](#3-核心概念)
4. [用户指南](#4-用户指南)
5. [管理员指南](#5-管理员指南)
6. [系统配置](#6-系统配置)
7. [API 参考](#7-api-参考)
8. [Dagster 编排](#8-dagster-编排)
9. [运维指南](#9-运维指南)
10. [常见问题](#10-常见问题)

---

## 1. 系统简介

### 1.1 什么是 Forum Memory Agent？

Forum Memory Agent 是一个**智能知识论坛系统**，它将传统论坛与 AI 记忆能力结合。用户发帖提问后，系统自动：

- **AI 即时回答**：基于已有知识库和外部 RAG 数据源，生成参考回答
- **知识自动提取**：帖子解决后，自动从讨论中提取可复用的知识点
- **知识去重与演化**：通过 AUDN 机制（增/改/删/跳过），自动管理知识库的增删更新
- **混合搜索**：BM25 关键词 + KNN 语义向量混合搜索，精准召回相关知识

### 1.2 设计哲学

| 原则 | 说明 |
|------|------|
| **减少用户负担** | 发帖即获 AI 回答，知识提取全自动，用户只需关注内容 |
| **软删除优先** | 所有删除为状态标记，数据可恢复 |
| **事件驱动** | 异步编排，不阻塞用户操作 |
| **优雅降级** | ES 不可用时降级为 SQL 搜索，RAG 失败不阻塞 AI 回答 |

### 1.3 技术架构

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────┐
│  React 前端   │────→│  FastAPI 后端     │────→│ PostgreSQL  │
│  (Vite)      │     │  (同步模式)       │     │  (数据存储)  │
└──────────────┘     └────────┬─────────┘     └─────────────┘
                              │
                    ┌─────────┼─────────┐
                    ↓         ↓         ↓
             ┌──────────┐ ┌──────┐ ┌──────────┐
             │ ES 8.9   │ │ LLM  │ │ Dagster  │
             │ 混合搜索  │ │ 提供商│ │ 异步编排  │
             └──────────┘ └──────┘ └──────────┘
```

---

## 2. 快速开始

### 2.1 环境要求

- Python 3.11+
- Node.js 18+
- PostgreSQL 14+
- Elasticsearch 8.9+
- Dagster（用于异步编排）

### 2.2 后端启动

```bash
# 1. 设置环境变量（或创建 .env 文件）
export FM_DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/forum_memory"
export FM_ES_URL="http://localhost:9200"
export FM_ES_ENABLED=true
export FM_LLM_PROVIDER="openai"
export FM_LLM_API_KEY="sk-..."

# 2. 安装依赖
cd forum_memory_backend
pip install -r requirements.txt

# 3. 初始化数据库
python -m forum_memory.scripts.init_db

# 4. 启动 API 服务
uvicorn forum_memory.main:app --host 0.0.0.0 --port 8000

# 5. 启动 Dagster（新终端）
dagster dev -m forum_memory.dagster.definitions
```

### 2.3 前端启动

```bash
cd forum_memory_frontend
npm install
npm run dev
```

前端默认运行在 `http://localhost:5173`，API 代理到 `http://localhost:8000`。

---

## 3. 核心概念

### 3.1 板块（Namespace）

板块是知识的隔离单元，每个板块拥有：
- **独立的 ES 索引**（`forum_memory_{板块名}`），知识搜索互不干扰
- **独立的术语词典**，用于搜索查询预处理（团队俚语 → 标准术语）
- **可配置的知识源**：内部记忆搜索 + 外部 RAG 知识库
- **访问模式**：公开 / 受限 / 私有

### 3.2 帖子（Thread）生命周期

```
         ┌─────── 用户标记最佳回答 ──→ RESOLVED
         │
OPEN ────┼─────── 超时（7天无回复）──→ TIMEOUT_CLOSED
         │
         └─────── 管理员删除 ────────→ DELETED
```

- **OPEN**：新建帖子，可接收回复
- **RESOLVED**：已解决，触发知识提取
- **TIMEOUT_CLOSED**：超时自动关闭，仍可提取知识（但标记需人工确认）
- **DELETED**：软删除，保留数据

### 3.3 记忆（Memory）生命周期

```
ACTIVE ──(180天不活跃)──→ COLD ──(365天不活跃)──→ ARCHIVED
  ↕                        ↕                        ↕
DELETED（软删除）        DELETED                  DELETED
```

每条记忆有两个维度：
- **状态（Status）**：ACTIVE → COLD → ARCHIVED → DELETED
- **权限（Authority）**：NORMAL（AI 可修改） / LOCKED（人工确认，AI 不可覆盖）

### 3.4 知识类型

| 类型 | 说明 | 示例 |
|------|------|------|
| `how_to` | 操作步骤 | "如何配置 K8s HPA 自动扩缩" |
| `troubleshoot` | 问题排查 | "OOMKilled 错误的排查步骤" |
| `best_practice` | 最佳实践 | "生产环境 Redis 应设置 maxmemory-policy" |
| `gotcha` | 易踩坑点 | "Python datetime.now() 默认无时区信息" |
| `faq` | 常见问答 | "为什么部署后 Pod 一直 CrashLoopBackOff" |

### 3.5 AUDN 去重机制

每次提取新知识时，系统会与已有记忆做语义比对，LLM 决策：

| 动作 | 触发条件 | 行为 |
|------|---------|------|
| **ADD** | 新知识，无重复 | 新增记忆 |
| **UPDATE** | 新知识是已有记忆的升级版 | 合并到已有记忆 |
| **DELETE** | 已有记忆已过时 | 标记已有记忆为删除 |
| **NONE** | 已完全覆盖 | 跳过，不做操作 |

> LOCKED 状态的记忆不可被 UPDATE 或 DELETE，如果冲突则新知识以 ADD 方式添加并标记冲突。

### 3.6 质量评分

每条记忆有一个 0~1 的质量评分，由 5 个因子加权计算：

```
质量分 = 0.35 × 有用比例 + 0.20 × 来源权重 + 0.15 × 检索热度 + 0.15 × 新鲜度 + 0.15 × (1 - 惩罚)
```

- **有用比例**：有用反馈 / (有用 + 无用 + 错误)，无反馈时默认 0.5
- **来源权重**：管理员=1.0，普通用户/提问者=0.7，AI=0.5
- **检索热度**：被检索次数 / 100（封顶 1.0）
- **新鲜度**：从 1.0 线性衰减至 0.1（365 天）
- **惩罚**：错误和过时反馈的累积惩罚

---

## 4. 用户指南

### 4.1 浏览板块

访问首页 `/boards`，查看所有可用板块。每个板块显示名称、描述和状态。点击板块卡片进入帖子列表。

### 4.2 发帖提问

1. 进入板块后点击 **"新建帖子"**
2. 填写：
   - **标题**：简洁描述问题
   - **内容**：详细描述，支持 Markdown 格式，可粘贴/拖拽图片
   - **标签**（可选）：便于分类检索
   - **环境**（可选）：如 "prod"、"staging"，用于搜索时环境匹配
3. 提交后，AI 会在几秒内自动生成回答

### 4.3 查看 AI 回答

- 帖子详情页自动轮询 AI 回答（渐进式退避：3s → 30s）
- AI 回答会标注来源：
  - `[M-xxxxxxxx]` 引用了哪条记忆
  - 知识库引用会标注来自外部 RAG
- 如果 AI 回答不满意，可点击 **"重新生成"** 按钮

### 4.4 互动与回复

- **回复帖子**：在评论区添加回复
- **点赞**：对有帮助的回答点赞（可取消）
- **标记最佳回答**：帖子作者可标记某条回复为最佳回答，帖子自动变为 RESOLVED 状态

### 4.5 反馈知识质量

对 AI 引用的每条记忆，可以反馈：
- **有用**：知识确实帮助解决了问题
- **无用**：知识不相关
- **错误**：知识内容有误
- **过时**：知识已不适用

反馈会影响记忆的质量评分，帮助系统逐步优化知识库。

### 4.6 搜索知识

在搜索页 `/search` 输入关键词，系统会：
1. 应用板块词典进行术语标准化
2. LLM 改写查询以提升召回率
3. BM25 + KNN 混合搜索
4. Rerank 精排
5. 返回结果，标注质量评分、环境匹配情况

---

## 5. 管理员指南

### 5.1 角色与权限

| 角色 | 权限范围 |
|------|---------|
| **超级管理员** | 全局管理：所有板块、用户、记忆 |
| **板块管理员** | 管理所属板块：帖子、记忆、配置 |
| **普通用户** | 发帖、回复、点赞、反馈 |

### 5.2 管理仪表盘

访问 `/admin` 进入管理界面（需管理员权限）。

**全局仪表盘**（超级管理员）：
- 板块总数、AI 解决率、待处理项、记忆总量
- 板块概览表
- 快捷链接到各管理模块

**板块仪表盘**（板块管理员）：
- 本板块的统计数据
- 板块信息和快捷导航

### 5.3 板块配置

在 `/admin/settings` 配置板块：

**基本信息**：
- 名称、描述、访问模式（公开/受限/私有）

**术语词典**：
- 定义团队俚语到标准术语的映射
- 示例：`"OOM" → "Out of Memory"`, `"k8s" → "Kubernetes"`
- 用于搜索预处理，提升查询准确性

**知识源配置**：
- 内部记忆搜索：开启/关闭
- 外部 RAG 搜索：开启/关闭，配置知识库 SN 列表

**板块管理员**（仅超级管理员）：
- 添加/移除板块管理员

### 5.4 记忆管理

在 `/admin/memories` 管理知识库：

**筛选维度**：
- 板块、知识类型、生命周期状态、权限级别
- 标签过滤、关键词搜索
- 待人工确认状态

**操作**：
- 编辑记忆内容
- 调整权限：NORMAL ↔ LOCKED
- 软删除记忆
- 查看来源帖子

### 5.5 待处理中心

在 `/admin/pending` 处理待确认项：

- **全部待处理**：所有需关注的记忆
- **待人工确认**：超时关闭帖子提取的记忆，需人工验证
- **低质量**：质量评分 < 0.3 的记忆

**操作**：
- **确认 & 锁定**：验证后提升为 LOCKED 权限
- **丢弃**：内容无价值则删除

### 5.6 批量导入

在 `/admin/import` 导入历史数据：
- 支持 `.json` 和 `.zip` 文件
- 可配置并发工作线程数（1-8）
- 可选跳过知识提取、仅试运行
- 显示导入结果统计

---

## 6. 系统配置

### 6.1 环境变量

所有配置通过 `FM_` 前缀的环境变量设置：

#### 数据库
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_DATABASE_URL` | 必填 | PostgreSQL 连接串（psycopg2 同步驱动） |

#### Elasticsearch
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_ES_URL` | `http://localhost:9200` | ES 地址 |
| `FM_ES_ENABLED` | `true` | 是否启用 ES |
| `FM_ES_USERNAME` | - | ES 认证用户名 |
| `FM_ES_PASSWORD` | - | ES 认证密码 |
| `FM_ES_INDEX_PREFIX` | `forum_memory` | 索引名前缀 |

#### LLM
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_LLM_PROVIDER` | `openai` | LLM 提供商（`openai` 或 `custom`） |
| `FM_LLM_API_KEY` | - | API 密钥 |
| `FM_LLM_MAIN_MODEL` | `gpt-4o` | 主推理模型 |
| `FM_LLM_SMALL_MODEL` | `gpt-4o-mini` | 轻量模型（压缩、改写） |
| `FM_LLM_EMBEDDING_MODEL` | `text-embedding-3-small` | 嵌入模型 |
| `FM_EMBEDDING_DIMENSION` | `1536` | 嵌入维度 |

#### 自定义 LLM（当 provider=custom 时）
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_CUSTOM_LLM_URL` | - | 自定义 LLM API 地址 |
| `FM_CUSTOM_EMBED_URL` | - | 自定义嵌入 API 地址 |
| `FM_CUSTOM_RERANK_URL` | - | 自定义 Rerank API 地址 |

#### RAG
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_RAG_BASE_URL` | - | 外部 RAG API 地址 |
| `FM_RAG_TIMEOUT` | `30` | RAG 请求超时（秒） |

#### 业务参数
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FM_THREAD_TIMEOUT_DAYS` | `7` | 帖子超时天数 |
| `FM_COLD_INACTIVE_DAYS` | `180` | 记忆转冷存天数 |
| `FM_ARCHIVE_INACTIVE_DAYS` | `365` | 记忆归档天数 |
| `FM_WRONG_FEEDBACK_THRESHOLD` | `3` | 错误反馈惩罚阈值 |
| `FM_PROMOTE_USEFUL_RATIO` | `0.8` | 自动提升权限的有用比例阈值 |
| `FM_PROMOTE_MIN_FEEDBACK` | `10` | 自动提升权限的最少反馈数 |

---

## 7. API 参考

### 7.1 帖子 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v1/threads` | 帖子列表（支持分页、状态过滤、标题搜索） |
| `POST` | `/api/v1/threads` | 创建帖子（自动触发 AI 回答） |
| `GET` | `/api/v1/threads/{id}` | 帖子详情 |
| `POST` | `/api/v1/threads/{id}/resolve` | 标记已解决 |
| `POST` | `/api/v1/threads/{id}/ai-answer` | 手动触发 AI 回答 |
| `GET` | `/api/v1/threads/{id}/comments` | 评论列表 |
| `POST` | `/api/v1/threads/{id}/comments` | 添加评论 |
| `POST` | `/api/v1/threads/{id}/comments/{cid}/upvote` | 切换点赞 |
| `DELETE` | `/api/v1/threads/{id}/comments/{cid}` | 删除评论 |
| `DELETE` | `/api/v1/threads/{id}` | 删除帖子 |

### 7.2 记忆 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v1/memories` | 记忆列表（支持高级筛选） |
| `POST` | `/api/v1/memories` | 手动创建记忆 |
| `GET` | `/api/v1/memories/{id}` | 记忆详情 |
| `PUT` | `/api/v1/memories/{id}` | 更新记忆内容 |
| `DELETE` | `/api/v1/memories/{id}` | 软删除记忆 |
| `PUT` | `/api/v1/memories/{id}/authority` | 变更权限（LOCKED/NORMAL） |
| `POST` | `/api/v1/memories/search` | 混合搜索记忆 |
| `POST` | `/api/v1/memories/extract/{thread_id}` | 手动触发知识提取 |
| `POST` | `/api/v1/memories/batch` | 批量获取记忆 |
| `GET` | `/api/v1/memories/tags` | 获取所有标签 |

### 7.3 板块 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v1/namespaces` | 板块列表 |
| `POST` | `/api/v1/namespaces` | 创建板块 |
| `GET` | `/api/v1/namespaces/{id}` | 板块详情 |
| `PUT` | `/api/v1/namespaces/{id}` | 更新板块 |
| `DELETE` | `/api/v1/namespaces/{id}` | 删除板块 |
| `GET` | `/api/v1/namespaces/{id}/stats` | 板块统计 |
| `GET` | `/api/v1/namespaces/stats/aggregate` | 全局统计 |
| `PUT` | `/api/v1/namespaces/{id}/dictionary` | 更新术语词典 |

### 7.4 反馈 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/feedback/{memory_id}` | 提交反馈 |
| `DELETE` | `/api/v1/feedback/{memory_id}` | 撤回反馈 |
| `GET` | `/api/v1/feedback/{memory_id}` | 反馈列表 |
| `GET` | `/api/v1/feedback/{memory_id}/summary` | 反馈汇总 |

---

## 8. Dagster 编排

### 8.1 传感器（Sensors）

| 传感器 | 触发条件 | 功能 |
|--------|---------|------|
| `thread_resolved_sensor` | 每 30 秒轮询 | 检测帖子解决事件，触发知识提取 |
| `thread_timeout_sensor` | 每小时 | 批量超时关闭过期帖子 |
| `memory_lifecycle_sensor` | 每天 | 执行记忆冷存/归档转换 |
| `quality_refresh_sensor` | 每天 | 批量刷新质量评分 |

### 8.2 作业（Jobs）

**知识提取作业** (`extract_memories_job`)：
```
load_thread_discussion → compress_discussion → extract_facts → process_facts_audn → finalize_extraction
```

**其他作业**：
- `timeout_threads_job`：批量超时关闭帖子
- `lifecycle_memories_job`：记忆状态转换
- `refresh_quality_job`：质量评分刷新

### 8.3 Dagster UI

启动 `dagster dev` 后访问 `http://localhost:3000`，可查看：
- 作业运行历史和日志
- 传感器状态和触发记录
- 手动触发作业执行

---

## 9. 运维指南

### 9.1 ES 索引管理

```bash
# 重建板块索引（当索引损坏或映射变更时）
python -m forum_memory.scripts.reindex --namespace-id <UUID>

# 全量回填记忆到 ES
python -m forum_memory.scripts.backfill
```

### 9.2 监控要点

- **ES 索引健康**：检查 ES 集群状态和索引大小
- **Dagster 作业**：监控提取作业的成功率和延迟
- **LLM API**：监控调用延迟和错误率
- **记忆质量分布**：定期审查低质量记忆（< 0.3）

### 9.3 数据恢复

所有删除操作为软删除，可通过以下方式恢复：
- 帖子：将 `status` 从 `DELETED` 改回 `OPEN`
- 记忆：将 `status` 从 `DELETED` 改回 `ACTIVE`
- 板块：将 `is_active` 改为 `True`

---

## 10. 常见问题

### Q: AI 回答不准确怎么办？

1. 对引用的记忆提交"错误"反馈，降低其质量评分
2. 管理员可编辑或删除不准确的记忆
3. 手动创建正确的记忆并 LOCK
4. 点击"重新生成"获取新回答

### Q: 知识提取没有触发？

1. 检查帖子状态是否已变为 RESOLVED 或 TIMEOUT_CLOSED
2. 检查 Dagster 传感器是否正常运行
3. 查看 `DomainEvent` 表中事件是否已标记 `processed=True`
4. 可手动调用 `POST /api/v1/memories/extract/{thread_id}`

### Q: 搜索结果不理想？

1. 配置板块术语词典，标准化团队专用术语
2. 检查 ES 索引是否正常（`FM_ES_ENABLED=true`）
3. 确认记忆状态为 ACTIVE（COLD/ARCHIVED 不参与搜索）
4. 增加 `top_k` 参数扩大召回范围

### Q: 记忆被误删/误改怎么办？

- LOCKED 记忆不会被 AUDN 自动修改或删除
- 对于重要知识，建议及时设置为 LOCKED 状态
- 误删的记忆可在数据库中将 `status` 恢复为 `ACTIVE`
