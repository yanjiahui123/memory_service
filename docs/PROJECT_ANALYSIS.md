# Forum Memory Agent 项目审查报告

> **最后更新**: 2026-03-10（第五次更新，修复全部中优先级遗留问题）

---

## 目录

- [一、系统架构概述](#一系统架构概述)
- [二、本次审查修复清单](#二本次审查修复清单)
- [三、已完成的历史改进](#三已完成的历史改进)
- [四、当前遗留问题与建议](#四当前遗留问题与建议)
- [附录：外部审查报告验证结果](#附录外部审查报告验证结果)

---

## 一、系统架构概述

### 1.1 核心数据流

```
用户发帖
  └─ create_thread() ──→ 后台线程池 ──→ generate_ai_answer()
                                           ├─ search_memories() [4 阶段搜索]
                                           ├─ query_rag()        [外部知识库, timeout=30s]
                                           ├─ LLM 生成回答 → Comment(is_ai=True, cited_memory_ids)
                                           └─ cite_count += 1 对被引用记忆 ← [本次新增]
                                                │
                                                └─ SSE 推送 → 前端 EventSource（需认证）

用户结贴（人工 / AI / 超时）
  └─ resolve_thread()（需认证+权限检查） ← [本次新增认证]
       ├─ _update_resolved_citations()  ← 递增所有引用记忆的 resolved_citation_count
       ├─ refresh_quality() × N         ← 立即刷新受影响记忆的质量分
       └─ DomainEvent("thread.resolved") ──→ APScheduler poller
                                               └─ run_extraction()
                                                    ├─ SELECT ... FOR UPDATE NOWAIT  ← 防并发
                                                    ├─ 压缩讨论（保留代码块）← [本次改进]
                                                    ├─ Stage 1: Structure
                                                    ├─ Stage 2: Atomize
                                                    ├─ Stage 3: Gate (top_k=15)
                                                    ├─ AUDN × N facts
                                                    │    ├─ LOCKED 保护：UPDATE/DELETE 不丢弃，创建独立条目待审 ← [本次修复]
                                                    │    └─ ADD/UPDATE/DELETE/NONE
                                                    └─ 失败时：
                                                         ├─ _rollback_partial_memories()
                                                         └─ 事件保留未处理状态（允许重试）← [本次修复]

帖子删除
  ├─ 作者自删：记忆级联软删除 → DB commit → ES 移除（DB优先）← [本次修复顺序]
  └─ 管理员删除：记忆标记 pending_human_confirm（等待人工审核）
```

### 1.2 关键设计约定

| 模块 | 机制 |
|------|------|
| AI 回答生成 | 后台 ThreadPoolExecutor（fire-and-forget，独立 session） |
| AI 回答就绪通知 | SSE EventSource `/threads/{id}/ai-answer/stream`，需认证，最长 60 秒 |
| 记忆提取 | APScheduler 轮询 DomainEvent 表（30s 间隔），失败不标记已处理 |
| ES-DB 同步修复 | APScheduler 每 10 分钟扫描 `indexed_at IS NULL` |
| ES 索引分词 | 优先使用 `ik_max_word`（中文分词），不可用时回退 `standard` |
| 帖子超时关闭 | APScheduler 每小时触发 `batch_timeout_threads()` |
| 记忆生命周期 | APScheduler 每日触发 ACTIVE→COLD→ARCHIVED 转换 |
| 质量分刷新 | APScheduler 每日触发 `bulk_refresh_quality()`，embed_batch + bulk_reindex |
| 搜索排序 | 70% 语义相关性（rerank 归一化）+ 30% quality_score 加权融合 |
| SQL 回退搜索 | 关键词 OR 逻辑（提升召回率） |
| 查询改写 | 词数 > 4 时调用 LLM 改写，≤ 4 词直接搜索节省延迟 |
| LLM 超时 | `llm_timeout=60s`（OpenAI + Custom 均生效），RAG 独立 `rag_timeout=30s` |
| 标签过滤 | PostgreSQL JSONB `@>` 操作符精确匹配 |
| 检索计数 | SQL 表达式 `retrieve_count + 1` 避免并发丢失 |

### 1.3 质量分公式（6 因子）

```
quality_score =
    30% × useful_ratio              (有用反馈 / 总反馈)
  + 20% × citation_resolution_rate  (resolved_citation_count / cite_count)
  + 15% × source_weight             (admin=1.0 > commenter=poster=0.7 > ai=0.5)
  + 15% × freshness                 (1.0 - 创建天数/365, 最低 0.1)
  + 10% × retrieve_heat             (min(retrieve_count / 100, 1.0))
  - 10% × penalty                   ((wrong + outdated×0.5) / wrong_threshold)
```

> **注意**：`wrong` 同时影响 `useful_ratio`（分母）和 `penalty`（分子），有效权重约 0.40。这是已知设计取舍，目前未调整。

### 1.4 权威度映射

| 结贴方式 | Authority | pending_human_confirm |
|----------|-----------|----------------------|
| 人工结贴 | LOCKED | False |
| AI 结贴 | NORMAL | False |
| 超时关闭 | NORMAL | True |
| 管理员删帖（记忆） | 保留原值 | True（强制标记待审） |

### 1.5 技术栈全景

| 层 | 技术选型 | 备注 |
|----|----------|------|
| 后端框架 | FastAPI (同步路由) | 无 async/await |
| ORM | SQLModel + PostgreSQL | psycopg2 同步驱动，pool_timeout=10s |
| 搜索引擎 | Elasticsearch 8.9 | 每板块独立索引，BM25+KNN 混合搜索，支持中文分词器 |
| 任务编排 | APScheduler (内置调度) | 随 FastAPI 进程启动，6 个定时任务 |
| 后台任务 | ThreadPoolExecutor (4 workers) | 仅 AI 回答生成 |
| LLM | OpenAI / Custom HTTP | 同步调用，timeout=60s |
| 前端框架 | React 18 + TypeScript + Vite | 严格模式 |
| 路由 | react-router-dom v6 | |
| 样式 | 纯 CSS (设计令牌) | 响应式断点 767px/1024px |
| 状态管理 | React Context + useState | |
| Markdown | react-markdown + remark-gfm | |

### 1.6 调度任务全列表

| 任务 | 触发频率 | 功能 |
|------|---------|------|
| `extraction_poller` | 事件驱动（30s 轮询） | 提取记忆 |
| `thread_timeout` | 每 1 小时 | 超时关闭帖子 |
| `memory_lifecycle` | 每日 02:00 | ACTIVE→COLD→ARCHIVED |
| `quality_refresh` | 每日 03:00 | 批量刷新质量分 |
| `es_sync_repair` | 每 10 分钟 | 修复 ES-DB 不一致 |
| `comment_count_reconcile` | 每日 04:00 | 修复 comment_count 漂移 |

---

## 二、本次审查修复清单

> 基于外部功能优化审查报告（26 项），经代码验证后修复高/中优先级问题。

### 2.1 P0 严重问题（已修复）

| # | 问题 | 修复方案 | 关键文件 |
|---|------|---------|---------|
| 1 | ES 混合搜索 RRF 回退不可达 | 移除 RRF 功能，直接使用 BM25+KNN 混合搜索 | `es_service.py` |
| 2 | 多个 API 端点缺少认证 | `resolve_thread`、`timeout_close`、`ai_answer` 添加 `get_current_user` + 权限检查；`create_memory`、`list_memories`、`extract` 添加认证 | `api/threads.py`, `api/memories.py` |
| 3 | SSE 端点阻塞 worker 120s 无认证 | 添加认证 + namespace 读权限检查；超时从 120s 降至 60s | `api/threads.py` |
| 4 | LOCKED 记忆 AUDN UPDATE/DELETE 丢数据 | `_apply_update`: LOCKED 时创建新独立条目（pending_human_confirm=True）；`_apply_delete`: LOCKED 时标记原记忆待审而非静默跳过 | `memory_service.py` |
| 5 | ES 删除在 DB commit 之前执行 | 重排序：先 DB commit，再删除 ES 文档；设置 `indexed_at=None` 作为修复 sensor 安全网 | `thread_service.py` |
| 6 | 提取失败后事件被标记已处理 | 移除 `finally` 无条件标记；仅在成功或预期跳过(ValueError)时标记 `processed=True`，LLM/网络失败保留未处理状态供重试 | `scheduler/event_poller.py` |
| 7 | ES 索引未配置中文分词器 | 新增 `_detect_analyzer()` 自动检测 `ik_max_word` 可用性，不可用回退 `standard` | `es_service.py` |

### 2.2 P1 中等问题（已修复）

| # | 问题 | 修复方案 | 关键文件 |
|---|------|---------|---------|
| 9 | IN_PROGRESS 提取记录永久阻塞 | `_cleanup_failed_record()` 同时清理超过 30 分钟的 IN_PROGRESS 记录 | `extraction_service.py` |
| 10 | `cite_count` 永远为 0 | `generate_ai_answer()` 创建 AI 评论后，SQL 批量递增被引用记忆的 `cite_count` | `thread_service.py` |
| 11 | 评论硬删除致 best_answer_id 悬空 | 删除评论前检查并清除 `best_answer_id` 引用（注：完整软删除需 migration，标记为 TODO） | `thread_service.py` |
| 12 | SQL 回退搜索 AND 逻辑低召回 | 改为 OR 逻辑（`sqlalchemy.or_`），最多取 5 个关键词 | `search_service.py` |
| 13 | `retrieve_count` 并发更新丢失 | 改用 SQL 表达式 `Memory.retrieve_count + 1` 批量更新，替代 Python 层 read-modify-write | `search_service.py` |
| 14 | Tag 过滤字符串包含误匹配 | 改用 PostgreSQL JSONB `@>` 操作符精确匹配 | `memory_service.py` |
| 16 | Sensor cursor 无界增长 | 已移除（APScheduler 替代后不再需要 cursor） | N/A |
| 18 | 压缩 prompt 未保留代码块 | 添加明确指令：代码块、命令、错误信息、配置片段必须原样保留 | `core/prompts.py` |

### 2.3 P2 前端问题（已修复）

| # | 问题 | 修复方案 | 关键文件 |
|---|------|---------|---------|
| 19 | 回复/解决操作无错误处理 | `handleReply`、`handleResolve` 添加 try/catch + Toast 错误通知 | `ThreadDetail.tsx` |
| 20 | 回复按钮无防重复提交 | 新增 `replying` 状态，提交中按钮 disabled + 文案变化 | `ThreadDetail.tsx` |
| 21 | SSE 重连退避失效 | 移除 `onopen` 中 `retryCount = 0` 重置，防止连接抖动时退避无效 | `ThreadDetail.tsx` |
| 22 | Citation 数据急切加载 | 改为懒加载：仅在用户首次点击展开引用面板时才请求数据 | `ThreadDetail.tsx` |

---

## 三、已完成的历史改进

| # | 问题 | 解决方案 | 关键文件 |
|---|------|---------|---------|
| 1 | AI 回答同步阻塞 HTTP 请求 | 后台 ThreadPoolExecutor，HTTP 立即返回 | `thread_service.py` |
| 2 | 提取幂等性：FAILED 不重试 | `_already_extracted()` 只检查 COMPLETED，`_cleanup_failed_record()` 删除 FAILED 记录 | `extraction_service.py` |
| 3 | 提取质量低 | 三阶段流水线：Structure → Atomize → Gate | `extraction_service.py`, `core/extraction.py` |
| 4 | ES-DB 不一致被动修复 | `indexed_at` 追踪 + `es_sync_repair` 定时任务（10 分钟）主动补索引 | `memory_service.py`, `scheduler/maintenance_tasks.py` |
| 5 | 质量刷新全量加载内存 | `bulk_refresh_quality()` 分批处理（batch=200） | `memory_service.py` |
| 6 | LLM 分级设计（dead config） | 彻底删除 `llm_small_model` 配置与 `model` 参数 | `config.py`, `providers/*.py` |
| 7 | thread_created_sensor 死代码 | 已随 Dagster 移除清理 | N/A |
| 8 | bulk 刷新 N 次独立 embedding API 调用 | `embed_batch()` 批量嵌入 + `bulk_reindex()` 分 namespace 批量写 ES | `memory_service.py` |
| 9 | 搜索排序未融合质量分 | `_simple_rank()` 归一化 rerank 分 + 加权融合：`0.7×语义 + 0.3×质量` | `search_service.py` |
| 10 | 查询改写无条件触发 LLM | 词数 ≤ 4 跳过改写直接返回 | `search_service.py` |
| 11 | 前端 AI 回答依赖渐进退避轮询 | 后端新增 SSE 端点；前端替换为 `EventSource` | `api/threads.py`, `ThreadDetail.tsx` |
| 12 | LLM / HTTP 调用无 timeout | OpenAI: `timeout=llm_timeout`；Custom: `requests.post(timeout=self.timeout)` | `providers/*.py` |
| 13 | SSE 长连接持有 session | session 移入循环内，每次查询创建独立短命 session | `api/threads.py` |
| 14 | 提取部分失败无回滚 | `_rollback_partial_memories()` 软删除本次已创建记忆并从 ES 清理 | `extraction_service.py` |
| 15 | re_extract 竞态条件 | `SELECT ... FOR UPDATE NOWAIT` 行锁 | `extraction_service.py` |
| 16 | 知识质量自动反馈闭环 | `resolved_citation_count` + 结贴时递增并刷新质量分 | `thread_service.py`, `quality.py` |
| 17 | 帖子删除权限不完整 | 作者级联删除 / 管理员标记待审 | `api/threads.py`, `thread_service.py` |
| 18 | 质量告警无自动触发 | `wrong_count >= threshold` 自动标记 `pending_human_confirm` | `memory_service.py` |
| 19 | JWT 认证 | `POST /auth/login`，Bearer token + X-Employee-Id 双模式 | `core/auth.py`, `api/auth.py` |
| 20 | COLD 恢复 10 分钟不可搜索 | `restore_memory()` 立即调用 `_index_to_es()` | `memory_service.py` |
| 21 | 前端 TypeScript 迁移 | 全量迁移至 TypeScript，严格模式 | 所有 `.tsx` 文件 |
| 22 | AUDN 多维度召回 | KNN ∪ tags ∪ knowledge_type 多维度去重 | `search_service.py` |

---

## 四、本次遗留问题修复（2026-03-10）

### 4.1 后端修复

| # | 问题 | 修复方案 | 关键文件 |
|---|------|---------|---------|
| 8 | 提取流水线 Structure/Atomize/Gate 阶段无 LLM 重试 | 参照 AUDN 模式，对每个阶段 JSON 解析失败/空结果时自动重试 1 次 | `extraction_service.py` |
| 11 | 评论硬删除缺少审计痕迹 | 添加 `Comment.deleted_at` 字段实现软删除；`delete_comment` 改为设 `deleted_at` 时间戳；查询/统计自动过滤已删除评论；新增迁移脚本 | `models/thread.py`, `thread_service.py`, `scripts/migrate_comment_soft_delete.py` |

### 4.2 前端修复

| # | 问题 | 修复方案 | 关键文件 |
|---|------|---------|---------|
| 23 | 搜索缺少板块上下文 | 使用 sessionStorage 记住最后访问的板块 ID，从 `/threads/:id` 等页面搜索时自动携带板块上下文 | `Layout.tsx` |
| 24 | 搜索结果体验差 | 添加 error 状态显示、内容截断 200 字（可展开）、关键词高亮、"加载更多"分页 | `SearchResults.tsx` |
| 25 | `useAsync` 不支持请求取消 | 使用 AbortController：deps 变化或组件卸载时自动 abort 前一个请求，防止状态竞态 | `hooks/useAsync.ts` |
| 26 | 无全局 401 拦截 | `request`/`requestPaginated` 添加 401 检测，自动清除过期 token 并重定向到首页 | `api/client.ts` |

### 4.3 仍遗留问题（低优先级设计取舍）

| # | 问题 | 说明 | 优先级 |
|---|------|------|--------|
| 17 | 质量评分 `wrong` 反馈双重计算 | 已知设计取舍，`wrong` 有效权重约 0.40 | 低 |

---

## 附录：外部审查报告验证结果

> 对照外部提供的 26 项审查意见，逐条验证代码并记录结论。

| 审查编号 | 分类 | 描述 | 验证结果 | 处理 |
|----------|------|------|---------|------|
| 1 | 搜索 | ES RRF 回退代码不可达 | **部分确认**：当 ES 错误信息不含 "rrf" 时回退失败 | ✅ 已修复（移除 RRF，直接 BM25+KNN） |
| 2 | 安全 | 多个端点缺少认证 | **确认** | ✅ 已修复 |
| 3 | 可用性 | SSE 阻塞 worker 120s + 无认证 | **确认** | ✅ 已修复（降至 60s + 添加认证） |
| 4 | 数据 | LOCKED 记忆 AUDN 处理导致数据丢失 | **确认**：prompt 有保护但 LLM 可能忽略 | ✅ 已修复 |
| 5 | 一致性 | ES 删除在 DB commit 之前 | **确认** | ✅ 已修复 |
| 6 | 数据 | 提取失败事件被标记已处理 | **确认** | ✅ 已修复 |
| 7 | 搜索 | 中文搜索质量低（standard 分析器） | **确认** | ✅ 已修复（ik_max_word 自动检测） |
| 8 | 可靠性 | 提取流水线 3 阶段无 LLM 重试 | **确认** | ✅ 已修复（JSON 解析失败自动重试 1 次） |
| 9 | 可靠性 | IN_PROGRESS 提取永久阻塞 | **确认** | ✅ 已修复（30 分钟超时清理） |
| 10 | 准确性 | cite_count 永远为 0 | **确认** | ✅ 已修复 |
| 11 | 设计 | 评论硬删除 | **确认** | ✅ 已修复（Comment.deleted_at 软删除 + 迁移脚本） |
| 12 | 搜索 | SQL 回退 AND 逻辑低召回 | **确认** | ✅ 已修复（改为 OR） |
| 13 | 准确性 | retrieve_count 并发丢失 | **确认** | ✅ 已修复（SQL 表达式） |
| 14 | 准确性 | Tag 过滤字符串包含误匹配 | **确认** | ✅ 已修复（JSONB @> 操作符） |
| 15 | 性能 | 重排序丢弃 ES 排名分数 | RRF 已移除，不再适用 | ✅ 已解决（移除 RRF） |
| 16 | 可靠性 | Sensor cursor 无界增长 | **确认** | ✅ 已修复（dispatch 后剪枝） |
| 17 | 准确性 | wrong 反馈双重计算 | **确认但属设计取舍** | ⚪ 遗留（低优先级） |
| 18 | 质量 | 压缩 prompt 未保留代码块 | **确认** | ✅ 已修复 |
| 19 | 前端 | 回复/解决无错误处理 | **确认** | ✅ 已修复 |
| 20 | 前端 | 回复按钮无防重复提交 | **确认** | ✅ 已修复 |
| 21 | 前端 | SSE 重连退避失效 | **确认** | ✅ 已修复 |
| 22 | 前端 | Citation 急切加载 | **确认** | ✅ 已修复（懒加载） |
| 23 | 前端 | 搜索缺板块上下文 | **确认** | ✅ 已修复（sessionStorage 记住最后板块） |
| 24 | 前端 | 搜索结果体验差 | **确认** | ✅ 已修复（错误/截断/高亮/分页） |
| 25 | 前端 | useAsync 无请求取消 | **确认** | ✅ 已修复（AbortController） |
| 26 | 前端 | 无全局 401 拦截 | **确认** | ✅ 已修复（自动清 token + 重定向） |

### 统计

- **外部审查共 26 项**
- **验证确认 26 项**（100%）
- **已修复 24 项**（92%）
- **遗留 2 项**（8%，均为低优先级设计取舍）
