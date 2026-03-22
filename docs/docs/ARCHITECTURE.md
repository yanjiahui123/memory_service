# 可插拔式记忆 Agent 服务 — 知识论坛专项架构方案

**版本：** 5.0  
**日期：** 2025-02-25  
**定位：** 面向知识论坛场景的记忆 Agent 完整设计

---

## 修订说明（v4.0 → v5.0）

| 序号 | 变更项 | 变更说明 |
|:---:|--------|----------|
| 1 | 帖子状态精简 | 移除 DUPLICATE 和 CLOSED 状态，仅保留 OPEN / RESOLVED / TIMEOUT_CLOSED 三个状态，降低状态机复杂度 |
| 2 | 移除发帖前智能推荐 | 去除发帖时 300ms 防抖实时检索记忆库的功能，降低前端复杂度，聚焦核心提取流程 |
| 3 | 记忆权威等级简化为二级 | 移除 HUMAN 等级，仅保留 LOCKED（人工参与）和 NORMAL（AI 自动）两级，简化权威判定逻辑 |
| 4 | 移除 Prompt AB 测试体系 | 暂时去除 Prompt AB 测试能力，减少系统复杂度，后续按需引入 |

---

## 目录

1. 项目概述
2. 设计原则与约束
3. 论坛系统设计
4. 系统总体架构
5. 记忆 Agent 核心引擎
6. 存储层设计（含完整建表 SQL）
7. LLM 服务层设计
8. Prompt 体系
9. 任务编排层设计（Dagster）
10. 管理后台
11. 自动化运维管道
12. 数据流图：从论坛变更到记忆生成
13. 非功能性设计
14. 分阶段实施计划
15. 风险与应对

---

## 1. 项目概述

### 1.1 背景

软件开发部存在大量周边问答场景，团队成员被不同周边团队反复问重复问题，造成大量人力浪费。需要一套系统将团队成员解决周边问题的过程资产进行总结复用。

### 1.2 目标

构建一个面向**知识论坛**的记忆 Agent 服务，将论坛帖子问答过程中的知识自动沉淀为持久记忆，并持续迭代优化，让后续类似问题可由 AI 直接回答，减少人工重复答疑。

### 1.3 核心价值

- **降低知识录入门槛：** 用户正常在论坛沟通即可无感录入知识，无需额外操作
- **减少重复问答：** 已解决的帖子自动形成记忆，后续类似问题由 AI 秒答
- **知识持续演进：** 记忆通过 AUDN 循环自动更新，通过反馈闭环持续优化质量
- **板块隔离复用：** 不同板块共享同一套记忆能力，知识按板块隔离

### 1.4 术语定义

| 术语 | 定义 |
|------|------|
| 记忆 (Memory) | 从帖子对话中提取的结构化知识条目，可被语义检索 |
| 命名空间 (Namespace) | 记忆隔离的最小单元，对应论坛的"板块"概念 |
| AUDN 循环 | Add/Update/Delete/None，记忆与已有知识对比后的四种操作决策 |
| 权威等级 (Authority) | 记忆的可信度分级：LOCKED > NORMAL |
| 生命周期状态 (Status) | 记忆的使用阶段：ACTIVE / COLD / ARCHIVED / DELETED |

---

## 2. 设计原则与约束

### 2.1 设计原则

**2.1.1 状态驱动提取**

不盲目提取所有帖子内容，只在帖子达到明确的"已解决"状态时才触发记忆提取。帖子状态信号直接决定提取时机和记忆权威等级。

**2.1.2 轻量化人机协同**

AI 自动提取 + 反馈驱动修正。系统自动完成绝大部分工作（提取、去重、合并），人工仅通过日常使用中的反馈（有用/没用/错误/过时）参与质量控制，无需主动审核每条记忆。管理员只需处理系统推送的冲突告警和低质量预警，实现"异常驱动"而非"逐条审核"。

**2.1.3 减少人的主动操作**

论坛用户不应被强制要求做复杂操作。发帖人点赞某个回答即可触发提取流程；超时后系统自动处理，但保留人工事后确认的机会。

**2.1.4 复用成熟方案**

核心记忆管道（事实提取 Prompt、AUDN 决策 Prompt）直接复用 Mem0 开源框架经过验证的 Prompt 体系，在此基础上做场景化扩展。

**2.1.5 反馈驱动质控**

不设前置审核门槛，所有提取的记忆直接生效参与检索和 AI 回答。通过用户反馈信号（有用率、错误报告、过时标记）持续监控质量，问题记忆由系统自动降权 + 推送管理员处理。用"事后反馈修正"替代"事前人工审核"，大幅减少人力投入。

### 2.2 技术约束

- 存储层基于 PostgreSQL + Elasticsearch，不引入图数据库
- LLM 服务通过可配置的 Provider 接口接入，不绑定特定模型
- 全部服务容器化部署，支持水平扩展
- Embedding 模型可替换，存储层支持重索引机制
- 任务编排基于 Dagster，定时任务和提取管道统一管理

---

## 3. 论坛系统设计

> 本章专注论坛产品本身的设计，与记忆系统相互独立，通过帖子状态变更事件解耦。

### 3.1 用户角色体系

一个论坛板块内存在以下角色，不同角色在帖子中的权限和影响力不同：

| 角色 | 定义 | 主要权限 |
|------|------|----------|
| **发起人 (Poster)** | 帖子的创建者 | 发帖、点赞最佳回答、关闭帖子 |
| **评论人 (Commenter)** | 参与帖子讨论的所有用户 | 回帖、点赞、举报 |
| **AI** | 系统自动生成回复 | 基于记忆库自动回帖、标注引用来源 |
| **板块管理员 (Admin)** | 板块创建者或其授权管理团队成员 | 权威解答、关闭帖子、管理记忆后台 |

**角色与回答权重的关系：**

```
板块管理员  →  权重 1.0（最高可信度，触发最高权威等级）
评论人      →  权重 0.7（所有评论人统一权重）
AI          →  权重 0.5（AI 自动回答）
```

**设计说明：**

所有参与评论的用户权重一致为 0.7。权重的真正意义在于提取时指导 LLM 优先采信哪个角色的观点。在帖子讨论中，最终由**发起人点赞确认**哪个回答有效——这个"确认动作"本身已经代表了验证，比回答者的身份权重更有意义。

### 3.2 帖子状态机

#### 3.2.1 状态定义

| 状态 | 含义 | 触发记忆动作 |
|------|------|-------------|
| **OPEN** | 帖子进行中（包含待处理、讨论中、AI 已回复等中间态） | 不提取 |
| **RESOLVED** | 发起人点赞某个回答（含 AI 回答）并关闭帖子 | 根据被点赞的回答类型触发提取 |
| **TIMEOUT_CLOSED** | 帖子超时无响应，系统自动关闭 | 触发提取（待人工事后确认） |

**设计说明：**

帖子状态机精简为三个状态。OPEN 合并了所有"帖子还没有最终结论"的过程态，避免了"很难判定一个问题是否达成共识"的歧义——共识的信号只有一个：**发起人主动点赞并关闭帖子，或系统超时关闭**。

超时关闭（TIMEOUT_CLOSED）是独立状态，原因是它与 RESOLVED 的来源性质不同：RESOLVED 有发起人的主动确认，TIMEOUT_CLOSED 没有。两种状态产出的记忆权威等级不同，且 TIMEOUT_CLOSED 需要人工事后进行一次确认。

#### 3.2.2 帖子状态流转图

```
用户发帖
    │
    ▼
  OPEN（进行中）
    │
    ├─── 发起人点赞某回答并关闭帖子
    │         │
    │         ├── 被点赞的是 AI 回答  ──────────▶  RESOLVED [ai_resolved]
    │         └── 被点赞的是人工回答  ──────────▶  RESOLVED [human_resolved]
    │                                              （管理员回答 = human_resolved + admin_flag）
    │
    └─── 超过 N 天无人回答，系统自动关闭  ────▶  TIMEOUT_CLOSED（触发提取，待人工确认）
```

#### 3.2.3 RESOLVED 状态的记忆等级映射

| 场景 | resolved_type | 记忆权威等级 |
|------|:---:|:---:|
| 发起人点赞 AI 的回答 | ai_resolved | NORMAL（直接生效，通过反馈闭环监控质量） |
| 发起人点赞评论人的回答 | human_resolved | LOCKED（有人工参与，直接锁定） |
| 发起人点赞管理员的回答 | human_resolved + admin_flag | LOCKED（有人工参与，直接锁定） |

**说明：** 权威等级简化为两级。AI 提取的记忆统一为 NORMAL，表示"纯 AI 生成，未经人工验证"；所有有人工参与的回答（无论是评论人还是管理员）产出的记忆统一为 LOCKED，表示"有人工参与并验证过"，禁止自动修改。这种二元划分清晰明了：要么是 AI 自动的，要么是人参与过的。

#### 3.2.4 TIMEOUT_CLOSED 提取策略

超时关闭帖子提取后需要人工事后确认，具体规则：

- 帖子超时关闭时，如果帖子内有 AI 或人工回答，触发提取，记忆标记 `pending_human_confirm = true`
- 管理后台"待处理中心"展示这些记忆，管理员可一键确认（晋升至 LOCKED）或丢弃
- 若 30 天内无人处理，记忆保持 NORMAL 等级正常参与检索

### 3.3 论坛核心功能

- **板块管理：** 创建/配置板块，板块级 AI 配置，板块仪表盘
- **帖子系统：** 发帖（问题 + 标签 + 附件）、回帖（AI 自动 + 人工 + 代码块）、点赞最佳回答、关闭帖子
- **智能辅助：** AI 自动回复、相关问题推荐
- **搜索与发现：** 帖子 + 记忆融合搜索，热门问题展示
- **通知与协作：** 新帖通知、@提及、超时提醒

### 3.4 帖子分类标签体系

| 标签类型 | 选填方式 | 说明 |
|----------|----------|------|
| **技术分类** | 可选，多选 | 注入记忆 metadata，辅助精准检索 |
| **紧急程度** | 可选，单选 | P0~P3，影响通知策略 |
| **知识类型** | 可选，单选 | HOW_TO / TROUBLESHOOT / BEST_PRACTICE / GOTCHA / FAQ |
| **适用环境** | 可选，自由文本 | 注入记忆 metadata.environment，不强制版本格式 |

> **说明：** 原方案中有"适用版本/环境"字段，但并非所有问题都有版本概念（如流程类问题、认知类问题）。此处调整为**"适用环境"**（更宽泛），格式由用户自由填写（如 "v2.3.x"、"JDK17"、"K8s 集群"、"生产环境"），LLM 在提取时判断是否有版本语义，有版本语义的才参与版本匹配逻辑。

### 3.5 AI + 人工协作流程

```
用户发帖
    │
    ▼
AI 预检索记忆库
    │
    ├── 命中高相关记忆 → AI 直答（标注引用来源）→ 发起人查看
    ├── 命中中等记忆   → AI 参考回答（建议等人工确认）→ 发起人查看
    └── 无相关记忆     → 等待人工，通知板块管理员
    │
    ▼
发起人操作
    │
    ├── 点赞 AI 回答并关闭   → RESOLVED [ai_resolved]   → 提取 NORMAL（直接生效）
    ├── 点赞人工回答并关闭   → RESOLVED [human_resolved] → 提取 LOCKED（直接生效）
    └── 不操作（超时）       → TIMEOUT_CLOSED            → 提取 NORMAL + pending_confirm
```

---

## 4. 系统总体架构

### 4.1 架构总览

```
╔══════════════════════════════════════════════════════════════╗
║                  论坛前端 (Forum Frontend)                   ║
║           发帖 / 回帖 / 点赞 / 关闭帖子 / 搜索              ║
╠══════════════════════════════════════════════════════════════╣
║                  论坛后端 (Forum Backend)                    ║
║    帖子 CRUD / 状态机流转 / 通知 / 用户权限                  ║
║    ↓ 帖子状态变更事件（异步消息队列）                         ║
╠══════════════════════════════════════════════════════════════╣
║                记忆 Agent 核心层 (Memory Core)               ║
║  ┌────────────┐  ┌────────────┐  ┌────────────────────────┐ ║
║  │  写入管道  │  │  检索引擎  │  │       管理服务         │ ║
║  │ 会话压缩   │  │ 查询预处理 │  │ 记忆 CRUD / 权威管理   │ ║
║  │ 事实提取   │  │ 混合检索   │  │ 报表 / 反馈监控        │ ║
║  │ AUDN 循环  │  │ Reranker精排│  │                        │ ║
║  │ 写入存储   │  │ 过滤排序   │  │                        │ ║
║  └────────────┘  └────────────┘  └────────────────────────┘ ║
╠══════════════════════════════════════════════════════════════╣
║                   任务编排层 (Dagster)                       ║
║  Sensor: 感知帖子关闭事件 / 定时: 质量/维护/合成             ║
╠══════════════════════════════════════════════════════════════╣
║                       存储层                                 ║
║        PostgreSQL（记忆主存储）+ ES（向量检索）               ║
╚══════════════════════════════════════════════════════════════╝
```

### 4.2 数据流全景

```
论坛帖子产生状态变更（RESOLVED / TIMEOUT_CLOSED）
       │
       ▼
  [事件总线] 异步消息：帖子 ID + 状态 + resolved_type
       │
       ▼
  [记忆 Core·写入] 存帖子原文（PG）→ 加入 Dagster 提取队列
       │         → Dagster Job：压缩 → 事实提取 → AUDN → 存记忆
       │
       ▼
  [记忆 Core·检索] AI 自动回答
       │         查询 → 黑话映射 → ES 混合召回 Top50 → Reranker 精排 Top5
       │         → 版本匹配 → 返回 + 记录引用链 + 采集反馈
       │
       ▼
  [管理层] Dagster 定时任务：质量刷新 / 合并检测 / 僵尸清理 / 周报
```

---

## 5. 记忆 Agent 核心引擎

### 5.1 三层记忆管道

核心引擎采用三层递进处理架构，将原始帖子逐步炼化为可检索的结构化知识：

```
Layer 1: 帖子原文        Layer 2: 压缩摘要        Layer 3: 结构化记忆

完整保存每条消息         对话的压缩版本            独立可检索的知识条目
用于溯源和审计           保留关键事实和上下文       语义检索/质量评分/权威等级
只写不改                随对话增长递归更新          AUDN 循环持续演进
存储: PG                存储: PG                  存储: PG + ES
```

增加 Layer 2 的价值：长帖不需要全量送入 LLM，压缩摘要足够做事实提取；压缩过程本身是一次信息筛选，过滤噪声；压缩可用小模型完成，降低成本。

### 5.2 写入管道

#### 5.2.1 触发策略

帖子状态变更为 RESOLVED 或 TIMEOUT_CLOSED 时，事件发送到记忆 Agent。帖子内容存入 PG 后加入 Dagster 提取队列，进入 5 步提取管道。

#### 5.2.2 处理流程

```
帖子关闭事件到达
  │
  ▼
存储帖子原文（Layer 1, PG）
  │
  ▼
加入 Dagster 提取队列
  │
  ▼（Dagster Job: memory_extraction_pipeline）
  │
  ├── Op 1: load_thread
  │   从 PG 加载帖子全部消息（只取有效内容：最佳回答 + 必要上下文）
  │   去重检查：thread_id 是否已在 PG 处理记录表中（幂等保护）
  │
  ├── Op 2: compress_if_needed
  │   ≤ 10 条: 跳过压缩，直接用原文
  │   10~50 条: 递归压缩生成摘要（Layer 2）
  │   > 50 条: 递归压缩 + 最佳回答原文完整保留
  │
  ├── Op 3: extract_facts
  │   事实提取（Mem0 FACT_RETRIEVAL_PROMPT + 场景扩展）
  │
  ├── Op 4: audn_cycle
  │   对每条候选事实：向量检索相似已有记忆
  │   → AUDN 决策（Mem0 UPDATE_MEMORY_PROMPT + 权威等级保护）
  │   ├── 新事实 vs LOCKED 记忆冲突 → 另存 + 冲突告警
  │   └── 新事实 vs NORMAL 记忆    → 标准 AUDN
  │
  └── Op 5: write_memories
      写入 PG + 同步 ES + 记录操作日志 + 建立溯源链接
      记录 thread_id 到处理记录表（幂等标记）
```

#### 5.2.3 提取内容的智能裁剪

- 只取"定论"不取"过程"——最终被发起人点赞的最佳回答内容为主
- 丢弃中间试错、附和（+1）、已被后续纠正的错误回答
- 代码块/配置片段即使压缩也完整保留
- TIMEOUT_CLOSED 时，取帖子内最高赞的回答（如无则跳过提取）

### 5.3 记忆状态体系

记忆有**两个独立的状态维度**，互不耦合：

```
维度一: authority（权威等级）— "这条记忆有多可信"
  LOCKED / NORMAL

维度二: status（生命周期状态）— "这条记忆处于什么阶段"
  ACTIVE / COLD / ARCHIVED / DELETED
```

#### 5.3.1 权威等级定义

| 等级 | 含义 | 产生方式 | 保护策略 |
|------|------|----------|----------|
| **LOCKED** | 有人工参与并验证，最高权威 | human_resolved 帖子提取、人工手动创建/编辑、管理员手动锁定 | 禁止自动修改/合并/删除，冲突时以此为准 |
| **NORMAL** | AI 提取或未经人工验证 | ai_resolved 帖子提取、timeout_closed 提取 | 全部自动操作允许 |

**设计说明：** 权威等级简化为两级，核心逻辑是：**人工参与了就是 LOCKED，AI 自动的就是 NORMAL**。这种二元划分去除了原方案中 HUMAN 和 LOCKED 之间的模糊地带，降低了系统判定和管理的复杂度。LOCKED 记忆禁止一切自动操作（UPDATE/DELETE/MERGE），只有人工才能修改，确保人工验证过的知识不会被 AI 覆盖。

#### 5.3.2 权威等级流转表

| 当前等级 | 目标等级 | 触发条件 | 触发方式 |
|----------|----------|----------|----------|
| NORMAL | LOCKED | 管理员/负责人在后台手动确认 | 人工 |
| NORMAL | LOCKED | useful_ratio > 0.8 且反馈 ≥ 10 | 自动推荐 + 管理员一键确认 |
| NORMAL | LOCKED | human_resolved 帖子提取时直接产生 | 自动 |
| LOCKED | NORMAL | 管理员手动降级（撤销误确认） | 人工 |
| LOCKED | 自动降级 | — | ❌ 禁止 |

**关键规则：** LOCKED 级别的记忆永远不会被自动降级——它们是人工确认过的，只有人工才能修改。

#### 5.3.3 生命周期状态设计

**设计原则：** 每个状态必须有明确的存在理由和对应行为。

| 状态 | 定义 | 存在理由 | 进入条件 | 退出条件 |
|------|------|----------|----------|----------|
| **ACTIVE** | 正常参与检索和 AI 回答生成 | 这是记忆的正常工作状态 | 记忆创建时 | 长期未被访问 / 人工归档 |
| **COLD** | 降低检索权重，不用于 AI 自动回答 | 长期未被访问的记忆可能已过时或不相关，降权而非直接删除，保留被"复活"的可能 | NORMAL 级别 180 天未被检索 | 被用户检索命中（自动复活）/ 人工恢复 |
| **ARCHIVED** | 从检索中完全移除，仅供溯源查询 | 详见下方 ARCHIVED 价值说明 | 人工归档，或 COLD 超 365 天仍未被检索 | 人工恢复 |
| **DELETED** | 软删除，仅管理员可见 | 允许误删后恢复，同时满足"我要删除这条记忆"的用户需求 | 人工删除 | 管理员从回收站恢复 |

**关键规则：** LOCKED 级别的记忆永远不会自动变 COLD/ARCHIVED——它们是人工确认过的，只有人工才能归档。

#### 5.3.4 ARCHIVED 状态的价值与作用

ARCHIVED 状态看似"只是从检索中移除"，但它对论坛自动回复系统有以下具体价值：

**① 提升检索精度（对自动回复的直接帮助）**

ARCHIVED 记忆被从 ES 索引中移除，不参与向量检索和 BM25 召回。这意味着 AI 自动回复时，不会被大量过时或低质量的知识"污染"检索结果。检索池越干净，AI 回答的准确率越高。如果没有 ARCHIVED，COLD 状态的记忆虽然降权但仍参与检索，当历史知识积累到一定量级时（比如板块运行两年后），大量 COLD 记忆会严重稀释检索结果的质量。

**② 存储与性能优化**

ARCHIVED 记忆不需要在 ES 中维护向量索引，减少 ES 存储和计算开销。当记忆总量达到万级以上时，将确认无用的记忆彻底移出检索层，对检索延迟有实质帮助。

**③ 审计回溯与恢复能力**

与 DELETED（软删除）不同，ARCHIVED 明确表达"这条知识曾经有效但现在不需要了"的语义。当新问题出现时，管理员可以在管理后台搜索 ARCHIVED 记忆，发现曾经解决过类似问题的旧知识，选择恢复为 ACTIVE。这在技术回退、旧版本维护等场景下有实际意义。

**④ COLD → ARCHIVED 的自然过渡**

COLD 是"降温观察"，ARCHIVED 是"确认淘汰"。COLD 超 365 天仍无人检索命中，说明这条知识确实没有价值了，自动进入 ARCHIVED 是合理的生命周期终点。没有 ARCHIVED，这些记忆只能选择"永远 COLD"或"直接 DELETED"——前者浪费检索资源，后者过于激进。

### 5.4 检索引擎

#### 5.4.1 三阶段检索管道：召回 → 精排 → 过滤

**第一阶段——查询预处理：**

```
原始查询: "天启超时怎么办"
     │
     ├── 黑话字典映射: "天启" → "支付网关 payment-gateway"
     ├── 查询改写（LLM 小模型）: 扩展缩写、补充相关术语
     └── 环境信号提取: 如果查询中含版本/环境信息，记录 env_hint
     │
     ▼
扩展后查询: "天启 支付网关 payment-gateway 超时怎么办"
```

**第二阶段——ES 混合召回（Top 50）：**

- 向量语义检索（dense_vector, cosine）
- 全文关键词检索（BM25）：错误码、命令名、配置项精确匹配
- 权威等级加权：LOCKED × 2.0 > NORMAL × 1.0
- 质量分加权：quality_score 字段参与排序

**第三阶段——Reranker 精排（Top 5）：**

ES 召回的 Top 50 候选集送入 Reranker 模型精排，显著提升技术支持场景准确率。推荐模型：BGE-Reranker-v2-m3（开源可自部署）。

**第四阶段——后处理：**

- 环境上下文匹配：记忆的 environment 与查询的 env_hint 比较，不匹配则降权 50% 并标注"⚠️ 此知识来自不同环境/版本"
- 记录反向引用链（cited_by）

#### 5.4.2 环境与版本上下文匹配

**设计调整：** 原方案中 `software_version` 字段采用固定格式（如 "v2.x"），但并非所有问题都有版本概念。优化方案是将版本作为"环境上下文"的子集：

```
检索时的环境匹配逻辑:

  用户查询含 env_hint（可以是版本、也可以是部署环境）
       │
  检索结果后处理:
       │
       ├── 记忆A (environment 含 "v2.x", LOCKED) → 匹配 ✓，正常权重
       ├── 记忆B (environment 含 "v1.x", LOCKED) → 不匹配 ✗，权重降 50%
       └── 记忆C (environment = null, NORMAL)     → 无环境限制，正常权重
```

记忆的 environment 字段由 LLM 在提取时自动判断是否填写，不强制所有记忆都有此字段——没有环境信息的知识（如通用流程、认知型问题）照常工作。

#### 5.4.3 同义词/黑话字典

在查询预处理层增加轻量的"黑话字典"映射，存储在板块配置表中（PG JSONB），应用启动时加载到内存缓存。管理员可在管理后台增删改。

### 5.5 反馈闭环

#### 5.5.1 反馈采集

- **显式反馈：** 每条记忆/AI 回答旁的 👍👎 按钮，支持"有用/没用/已过时/错误"四种类型
- **隐式信号：** 用户看到记忆后是否继续追问，是否点击"查看原文"，是否重复搜索相似问题

#### 5.5.2 质量评分公式

```
quality_score =
    useful_ratio × 0.35          （有用率）
  + source_weight × 0.20         （来源角色权重）
  + retrieve_heat × 0.15         （检索热度，归一化）
  + time_freshness × 0.15        （时间新鲜度，衰减函数）
  - negative_penalty × 0.15      （负面信号惩罚）
```

#### 5.5.3 反馈驱动的自动动作

| 条件 | 自动动作 |
|------|----------|
| WRONG 反馈 ≥ 3 次 | 从检索中降权 + 推送待审核 |
| OUTDATED 反馈 ≥ 3 次 | 标记待更新 + 触发重新提取 |
| useful_ratio > 0.8 且反馈 ≥ 10 | 晋升候选（NORMAL → 推荐管理员确认为 LOCKED） |
| 同一查询多人反馈没用 | 知识缺口告警 |

#### 5.5.4 反向链接与溯源归因

当一条记忆被检索并帮助解决了新问题时，将新问题的帖子 ID 追加到记忆的 `cited_by` 列表中。

**价值：** 可量化每条记忆的影响范围；当发现某条记忆是错的时，可通过引用链通知所有受影响的用户（"更正通知"）；为 quality_score 中的"检索热度"提供更精确数据。

### 5.6 Metadata 设计

#### 5.6.1 数据来源（四层，按优先级）

```
来源①：帖子事件自动注入（最主要来源）
  → 从帖子字段映射（标签→tags，作者角色→source_role，等）

来源②：用户在发帖时手动选择
  → 技术分类、知识类型、适用环境等

来源③：LLM 在事实提取时自动推断
  → 对未提供的字段（如 knowledge_type、environment），LLM 在提取时推断

来源④：管理员在后台手动补充/修正
  → 最终兜底手段

优先级: 管理员修正 > 用户选择/帖子注入 > LLM 推断 > 默认值
```

#### 5.6.2 字段设计：固定核心字段 + JSONB 扩展

**固定核心字段（PG 独立列，可索引，可做过滤条件）：**

| 字段名 | 类型 | 必填 | 存在理由 | 说明 |
|--------|------|:---:|----------|------|
| source_type | VARCHAR(50) | 是 | **来源系统标识**——当记忆系统未来接入其他来源（如 IM、工单系统）时，通过此字段区分知识来源，支持按来源类型过滤和统计。当前固定值 `forum`。 | 来源类型 |
| source_id | VARCHAR(200) | 否 | **溯源必备字段**——记录知识来自哪个帖子，支持"查看原文"跳转、引用链追踪、以及当记忆出错时定位原始上下文。手动创建的记忆可为空。 | 源帖子 ID |
| source_role | VARCHAR(50) | 是 | **决定初始权威等级的关键依据**——回答者角色（admin/commenter）直接影响提取出的记忆是 LOCKED 还是 NORMAL 等级，也影响 quality_score 中的 source_weight 权重分。 | 回答者角色: admin/commenter |
| knowledge_type | VARCHAR(50) | 否 | **检索精度优化**——不同知识类型（排障/操作指南/最佳实践）的检索需求不同。用户搜"怎么配置 X"时，优先返回 how_to 类型的记忆。同时用于知识合成时按类型归类。 | 知识类型: how_to/troubleshoot/best_practice/gotcha/faq |
| resolved_type | VARCHAR(50) | 是 | **记忆质量评估的核心输入**——区分知识来自人工确认解决（高可信）还是 AI 回答被确认（需观察）还是超时关闭（最低可信）。直接决定初始权威等级和是否标记 pending_confirm。 | 来源: ai_resolved/human_resolved/timeout_closed |
| tags | TEXT[] | 否 | **多维度检索入口**——技术分类标签支持 GIN 索引高效过滤，用户可按标签浏览知识，Dagster 定时任务按标签分组做知识合成和合并检测。 | 技术分类标签数组 |
| environment | VARCHAR(200) | 否 | **防止跨环境误匹配**——同一个问题在不同环境（JDK8 vs JDK17、K8s vs 物理机）的解决方案可能完全不同。此字段支持检索后处理中的环境匹配降权逻辑。自由格式，LLM 判断是否填写。 | 适用环境（自由格式） |
| access_level | SMALLINT | 否 | **信息安全隔离**——部分知识涉及内部系统架构、密钥配置等敏感信息，需要按等级控制可见范围。与用户的 clearance_level 比对决定是否展示。默认 1（公开）。 | 访问等级 1-10，默认 1 |
| pending_human_confirm | BOOLEAN | 否 | **超时记忆的质量兜底**——TIMEOUT_CLOSED 帖子缺少发起人的主动确认，提取出的记忆需要人工事后复核。此字段驱动管理后台"待处理中心"的展示，确保低可信记忆不被遗漏。 | 是否待人工事后确认 |

**自由扩展字段（PG JSONB `extra` 列，灵活扩展，无需改表）：**

| 字段名 | 类型 | 存在理由 | 示例值 |
|--------|------|----------|--------|
| os | string | **操作系统相关问题定位**——部分排障知识仅适用于特定 OS，辅助环境匹配和检索过滤。 | `"Linux"` |
| runtime | string | **运行时环境标识**——Java 版本、Python 版本等差异会导致完全不同的解决方案，补充 environment 字段的细粒度信息。 | `"JDK17"` |
| deployment | string | **部署方式标识**——同一服务在容器化 vs 物理机部署下的配置和排障方式不同。 | `"K8s"` |
| affected_service | string | **受影响服务名**——精确标识知识关联的具体服务，支持按服务名过滤检索，在微服务架构下尤其重要。 | `"payment-gateway"` |
| error_code | string | **错误码精确匹配**——错误码是排障场景中最高效的检索关键词，存入扩展字段后可被 ES BM25 精确匹配命中。 | `"ERR_CONN_REFUSED"` |
| root_cause_category | string | **根因分类统计**——支持管理后台按根因类型统计问题分布（配置错误 vs 代码缺陷 vs 环境问题），指导团队改进重点。 | `"配置错误"` |
| synthesized_from | string[] | **知识合成溯源**——当多条碎片记忆被合成为一条指南型记忆时，记录原始碎片的 ID，支持溯源和回滚。 | `["M-101", "M-205"]` |
| admin_flag | boolean | **管理员回答标识**——标记知识来自管理员的回答，可用于后续统计和分析。 | `true` |

**设计理由总结：**

固定字段的选取标准：高频使用（检索过滤/权威判定/质量评估中每次都需要）、需要建索引（支持 WHERE 条件高效过滤）、结构稳定（不会频繁增删）。

扩展字段的选取标准：使用频率较低（仅特定场景需要）、不同板块的需求差异大（有的板块不需要 error_code，有的不需要 deployment）、可能持续扩展（新场景新需求可直接加字段，无需 ALTER TABLE）。

### 5.7 API 设计

#### 5.7.1 核心接口

```
写入接口:
  POST /api/v1/memory/add              记忆写入（从帖子中自动提取）

检索接口:
  POST /api/v1/memory/search           语义检索记忆

管理接口:
  GET    /api/v1/memory                获取记忆列表（支持过滤/分页）
  GET    /api/v1/memory/{id}           获取单条记忆详情
  PUT    /api/v1/memory/{id}           更新记忆内容（人工修正）
  DELETE /api/v1/memory/{id}           删除记忆
  PUT    /api/v1/memory/{id}/authority  变更权威等级
  GET    /api/v1/memory/{id}/history   操作历史（审计）
  GET    /api/v1/memory/{id}/citations  获取引用链（溯源）

反馈接口:
  POST /api/v1/memory/{id}/feedback    提交反馈

板块接口:
  POST /api/v1/namespace               创建命名空间
  GET  /api/v1/namespace/{ns}/stats    板块统计
  PUT  /api/v1/namespace/{ns}/dictionary  管理黑话字典

运维接口:
  POST /api/v1/admin/reindex           触发 ES 重索引
```

---

## 6. 存储层设计

### 6.1 PostgreSQL — 记忆主存储

**记忆主表（memories）：**

```sql
CREATE TABLE memories (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace             VARCHAR(200)  NOT NULL,
    content               TEXT          NOT NULL,

    -- 二维状态
    authority             VARCHAR(20)   NOT NULL DEFAULT 'NORMAL',
    -- 枚举: LOCKED / NORMAL
    status                VARCHAR(20)   NOT NULL DEFAULT 'ACTIVE',
    -- 枚举: ACTIVE / COLD / ARCHIVED / DELETED

    -- 质量指标
    quality_score         FLOAT         NOT NULL DEFAULT 0.5,
    useful_count          INT           NOT NULL DEFAULT 0,
    not_useful_count      INT           NOT NULL DEFAULT 0,
    retrieve_count        INT           NOT NULL DEFAULT 0,

    -- 固定 metadata（独立列，可索引）
    source_type           VARCHAR(50)   NOT NULL DEFAULT 'forum',
    source_id             VARCHAR(200),
    source_role           VARCHAR(50)   NOT NULL,
    knowledge_type        VARCHAR(50),
    resolved_type         VARCHAR(50)   NOT NULL,
    tags                  TEXT[],
    environment           VARCHAR(200),
    access_level          SMALLINT      NOT NULL DEFAULT 1,
    pending_human_confirm BOOLEAN       NOT NULL DEFAULT FALSE,

    -- 扩展 metadata（JSONB）
    extra                 JSONB         NOT NULL DEFAULT '{}',

    -- 时间戳
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    -- 向量模型版本（用于重索引）
    embedding_model       VARCHAR(100)
);

CREATE INDEX idx_memories_namespace ON memories(namespace);
CREATE INDEX idx_memories_authority ON memories(authority);
CREATE INDEX idx_memories_status ON memories(status);
CREATE INDEX idx_memories_tags ON memories USING GIN(tags);
CREATE INDEX idx_memories_knowledge_type ON memories(knowledge_type);
CREATE INDEX idx_memories_extra ON memories USING GIN(extra);
CREATE INDEX idx_memories_pending_confirm ON memories(pending_human_confirm)
    WHERE pending_human_confirm = TRUE;
```

**提取处理记录表（extraction_records）——替代 Redis Bloom Filter 的幂等保护：**

```sql
CREATE TABLE extraction_records (
    thread_id             VARCHAR(200) PRIMARY KEY,
    status                VARCHAR(20)  NOT NULL DEFAULT 'COMPLETED',
    -- 枚举: COMPLETED / FAILED / IN_PROGRESS
    processed_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    memory_ids            UUID[]       -- 该帖子产出的记忆 ID 列表
);
```

**其他核心表：**

- **操作日志表（memory_operations）：** 每次 ADD/UPDATE/DELETE/MERGE/PROMOTE/DEMOTE 的完整审计记录
- **反馈记录表（memory_feedback）：** 每次检索反馈的详细信息
- **引用链表（memory_citations）：** 记忆被哪些帖子引用过
- **帖子原文表（threads）：** 完整保存原始帖子消息，含提取进度检查点
- **压缩摘要表（thread_summaries）：** 帖子的递归压缩版本
- **板块配置表（namespace_configs）：** 命名空间定义、管理员、角色权重、提取策略、黑话字典（JSONB）
- **知识缺口表（knowledge_gaps）：** 搜索无结果的查询记录

### 6.2 Elasticsearch — 检索加速层

从 PG 同步数据，提供高性能检索：

- **dense_vector 字段：** 记忆内容的向量表征，cosine 语义检索
- **全文索引：** BM25 全文检索（技术关键词精确匹配）
- **聚合分析：** 热门记忆统计、板块检索量统计
- **function_score 查询：** 融合向量得分、全文得分、权威等级加权、质量分加权

---

## 7. LLM 服务层设计

### 7.1 模型分工

| 任务 | 模型要求 | 说明 |
|------|----------|------|
| 会话压缩 | 小模型 | 信息筛选和摘要，对创造性要求低 |
| 事实提取 | 主力模型 | 需要高理解和抽象能力 |
| AUDN 决策 | 主力模型 | 需要精确的语义对比和冲突判断 |
| Embedding | 专用模型 | 向量化，支持可替换 + 重索引 |
| Reranker | 专用精排模型 | 推荐 BGE-Reranker-v2-m3 |
| 查询改写 | 小模型 | 简单的关键词扩展 |
| 知识合成 | 主力模型 | 跨帖子碎片合并为系统性指南 |

### 7.2 Provider 抽象

通过 Factory 模式抽象 LLM 调用，支持切换不同 Provider，不绑定特定供应商。配置在板块级可覆盖。

---

## 8. Prompt 体系

### 8.1 Prompt 调用关系

```
写入管道:
  帖子原文 → [长帖?] → 会话压缩 Prompt → 事实提取 Prompt → AUDN 更新 Prompt → 写入记忆

检索管道:
  用户查询 → 查询改写 Prompt → ES 混合检索 + Reranker → AI 回答生成 Prompt → 返回
```

### 8.2 核心 Prompt 来源与说明

| Prompt | 来源 | 说明 |
|--------|------|------|
| 事实提取（原始） | Mem0 FACT_RETRIEVAL_PROMPT | Apache 2.0 |
| 事实提取（扩展） | 自研，基于 Mem0 扩展 | 技术场景 + 角色权重注入 |
| AUDN 更新（原始） | Mem0 DEFAULT_UPDATE_MEMORY_PROMPT | Apache 2.0 |
| AUDN 更新（扩展） | 自研 | 追加权威等级保护规则 |
| 会话压缩（首次） | 自研 | `<analysis>` 思考链 + verbatim 保留 |
| 会话压缩（递归） | 自研 | 基于上次摘要 + 增量消息 |
| 查询改写 | 自研 | 扩展缩写、补充术语 |
| AI 回答生成 | 自研 | 基于检索记忆生成回答，标注引用 |
| 合并建议生成 | 自研 | 判断 OVERLAP/CONFLICT |

---

## 9. 任务编排层设计（Dagster）

### 9.1 职责划分

```
Dagster 负责: 全部任务编排 + 调度 + 监控
  → 提取管道编排（5 步 Op，每步独立重试）
  → 所有定时任务调度（daily/weekly/monthly）
  → 执行历史/日志/可观测性（Dagster UI）
  → 通过 Sensor 感知帖子关闭事件（轮询 PG 事件表或消息队列）

PG 负责: 原 Redis 承担的核心职责迁移
  → 提取去重: extraction_records 表替代 Bloom Filter
  → 提取队列: 帖子事件表 + Dagster Sensor 轮询替代 Redis List
  → 黑话字典: namespace_configs 表 JSONB 字段 + 应用内存缓存
  → 检索计数: retrieve_count 直接更新 PG（当前 QPS 不需要缓冲层）
```

**移除 Redis 的理由：**

当前阶段系统 QPS 预估为检索 100 QPS、写入 50 QPS，PG 完全能够支撑。Redis 的四个用途（提取队列、去重、计数缓冲、字典缓存）均可通过 PG + 应用内存缓存替代，减少一个基础设施组件的运维成本。未来如果 QPS 增长到需要高频写入缓冲时，可按需引入。

### 9.2 写入管道编排

```python
@dg.sensor(job_name="memory_extraction_pipeline", minimum_interval_seconds=30)
def thread_closed_sensor(context):
    """感知论坛帖子关闭事件，触发提取"""
    # 从 PG 事件表轮询未处理的帖子关闭事件
    pending_threads = db.query(
        "SELECT thread_id FROM thread_events WHERE status = 'PENDING' LIMIT 50"
    )
    for thread in pending_threads:
        yield dg.RunRequest(
            run_key=f"extract-{thread.thread_id}",
            run_config={"ops": {"load_thread": {"config": {"thread_id": thread.thread_id}}}}
        )

@dg.job(retry_policy=dg.RetryPolicy(max_retries=3, delay=30))
def memory_extraction_pipeline():
    thread = load_thread()
    compressed = compress_if_needed(thread)
    facts = extract_facts(compressed)
    audn_results = audn_cycle(facts)
    write_memories(audn_results)
```

### 9.3 定时任务调度

```python
@dg.schedule(cron_schedule="0 2 * * *", job_name="daily_quality_refresh")
def daily_quality_schedule():
    """每日: 质量评分刷新 + 合并检测"""
    return dg.RunRequest()

@dg.schedule(cron_schedule="0 3 * * 0", job_name="weekly_maintenance")
def weekly_maintenance_schedule():
    """每周: 僵尸清理 + 版本过时扫描 + 知识缺口 + 周报"""
    return dg.RunRequest()

@dg.schedule(cron_schedule="0 4 * * 6", job_name="weekly_knowledge_synthesis")
def weekly_synthesis_schedule():
    """每周: 知识合成（碎片→指南）"""
    return dg.RunRequest()
```

---

## 10. 管理后台

### 10.1 访问权限设计

**管理后台的访问范围：板块管理员（及其管理团队成员）专属，普通用户不可见。**

**设计理由：**

记忆后台的核心功能是处理"需要人工决策的事项"（冲突告警、合并建议、晋升推荐等）。这些决策需要对板块知识有深度理解，普通用户并不具备这个能力，反而会造成决策混乱。

普通用户的参与通过**前台反馈机制**实现（点赞/踩、"有用/错误"标注），这些信号会汇聚到管理后台供管理员参考，形成"用户信号输入→管理员决策输出"的分工。

**具体权限划分：**

| 角色 | 可访问内容 |
|------|----------|
| 板块创建者 | 本板块全部管理功能，可授权其他成员为管理员 |
| 板块管理员（授权成员） | 本板块记忆列表、待处理中心、配置管理 |
| 普通用户 | 不可访问管理后台（仅通过前台反馈参与） |

### 10.2 板块概览仪表盘

展示各板块记忆总数、本周新增、活跃率、各权威等级分布、待处理事项数、记忆健康度评分、AI 解决率趋势。

### 10.3 记忆列表与详情

支持按板块/权威等级/状态/质量分/时间范围筛选。详情页展示完整内容（可编辑）、权威等级（可调整）、质量指标仪表盘、反馈明细、来源溯源（跳转原始帖子）、引用链（被哪些帖子引用过）、关联记忆、操作历史。

### 10.4 待处理中心 — "看一眼→点一下"

汇聚所有需要人工决策的事项：

| 事项类型 | 说明 | 操作 |
|----------|------|------|
| 冲突告警 | 新事实与 LOCKED 记忆矛盾 | 保留哪个 |
| 合并建议 | 两条相似记忆 | 采纳合并/手动编辑/忽略 |
| 低质量警告 | useful_ratio < 0.2 | 归档/修正 |
| 晋升推荐 | NORMAL 记忆 useful_ratio > 0.8 且反馈 ≥ 10 | 一键确认为 LOCKED |
| 僵尸记忆 | 长期未被检索 | 归档/保留 |
| Timeout 确认 | TIMEOUT_CLOSED 帖子产生的待确认记忆 | 确认入库（晋升 LOCKED）/丢弃 |
| 更正通知触发 | 高引用量记忆被标记错误 | 一键通知受影响用户 |

### 10.5 板块配置

提取策略、角色权重、自动晋升阈值、通知规则、黑话字典管理。

---

## 11. 自动化运维管道

### 11.1 任务总览（全部由 Dagster 编排）

| 任务 | 频率 | 说明 |
|------|------|------|
| 质量评分刷新 | 每日 | 重算所有 ACTIVE 记忆的 quality_score |
| 合并检测 | 每日 | 语义相似度扫描，NORMAL 自动合并，LOCKED 生成建议 |
| 僵尸记忆清理 | 每周 | 标记 COLD/ARCHIVED |
| 知识缺口分析 | 每周 | 聚合无结果搜索，识别高频未解决查询 |
| 知识合成 | 每周 | 同 tag 下碎片 ≥ 10 条 → LLM 合成为指南 → 人工审核入库 |
| 周报/月报 | 每周/月 | 新增记忆数、检索次数、AI 解决率、待处理事项汇总 |

### 11.2 合并检测流程

1. 对每个板块，用 ES 向量近邻查询找到 cosine_similarity > 0.85 的记忆对
2. LLM 二次判定：DUPLICATE / OVERLAP / RELATED / CONFLICT
3. NORMAL + NORMAL 的 DUPLICATE → 自动合并（保留质量分高的）
4. 涉及 LOCKED → 只生成建议，推送待处理中心

### 11.3 知识合成（Memory Synthesis）

当某个 tag 下的 NORMAL/LOCKED 级别记忆 ≥ 10 条时，触发知识合成：收集所有相关记忆 → LLM 合成为一篇结构化指南 → 推送管理后台审核 → 采纳后新建 LOCKED 级别的"指南型记忆"，原始碎片可选保留或 ARCHIVED。

---

## 12. 数据流图：从论坛变更到记忆生成

> 本章说明论坛中的变更如何一步步转变为记忆，以及记忆系统如何判断是**新增**、**更新**还是**删除**。

### 12.1 总览数据流图

```
┌─────────────────────────────────────────────────────────────────┐
│                         论坛前端                                 │
│  发起人点赞某个回答并关闭帖子（或系统超时自动关闭）               │
└──────────────────────────┬──────────────────────────────────────┘
                           │ 帖子状态变更事件
                           │ {thread_id, status: RESOLVED|TIMEOUT_CLOSED,
                           │  resolved_type: ai_resolved|human_resolved|timeout,
                           │  best_answer_id, namespace}
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                      事件总线（异步）                             │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    记忆 Agent 写入管道                            │
│                                                                  │
│  Step 1: 去重检查                                                │
│    PG extraction_records 表: thread_id 是否已处理过？             │
│    ├── 已处理 → 跳过（幂等保护）                                  │
│    └── 未处理 → 继续                                             │
│                                                                  │
│  Step 2: 加载帖子内容                                            │
│    从 PG threads 表加载帖子原文                                   │
│    只取有效内容：最佳回答 + 必要的问题上下文                        │
│    丢弃：+1 评论、试错过程、已纠正的错误回答                        │
│                                                                  │
│  Step 3: 内容压缩（长帖）                                         │
│    ≤ 10 条消息 → 直接使用原文                                     │
│    10~50 条   → 小模型递归压缩，生成摘要                           │
│    > 50 条    → 递归压缩 + 最佳回答原文完整保留                    │
│    代码块、配置项 → 始终完整保留，不压缩                            │
│                                                                  │
│  Step 4: 事实提取（LLM 主力模型）                                 │
│    输入：压缩摘要 + resolved_type + 帖子 tags + 环境信息           │
│    输出：候选事实列表                                             │
│    例：["payment-gateway 超时默认 30s，可通过 X 配置调整",          │
│         "K8s 环境下需额外配置 Y"]                                  │
│                                                                  │
│  Step 5: AUDN 循环（对每条候选事实）                              │
│    → 见下方 12.2 详细流程图                                      │
│                                                                  │
│  Step 6: 写入结果                                                │
│    ADD/UPDATE → 写入 PG + 同步 ES + 记录操作日志                  │
│    DELETE     → 软删除 PG + 从 ES 移除 + 记录操作日志              │
│    NONE       → 无操作（记录已评估日志）                           │
│    记录 thread_id 到 extraction_records（幂等标记）                │
└─────────────────────────────────────────────────────────────────┘
```

### 12.2 AUDN 决策流程图（核心判断逻辑）

> AUDN = Add / Update / Delete / None，是记忆系统判断如何处理新候选事实的核心决策。

```
┌───────────────────────────────────────────────────────────────────┐
│                    新候选事实（单条）                               │
│                例："payment-gateway 超时默认 30s"                  │
└───────────────────────────┬───────────────────────────────────────┘
                            │
                            ▼
              向量检索：在当前 namespace 下
              找相似度 > 0.75 的已有记忆（Top 5）
                            │
            ┌───────────────┴────────────────────┐
            │ 有相似记忆                          │ 无相似记忆
            ▼                                     ▼
  LLM 语义对比判断                            ┌──────────┐
  新事实 vs 每条相似记忆                      │   ADD    │
            │                                │ 新知识，  │
    ┌───────┴──────────────────────┐         │ 直接新建  │
    │           │                 │         └──────────┘
    ▼           ▼                 ▼
  相同含义    有新增信息/       明显矛盾
  （重复）    旧信息已过时      （冲突）
    │           │                 │
    ▼           ▼                 ▼
  NONE        UPDATE             冲突处理（见下方）
（不操作）  （更新旧记忆）
              │
              ▼
        权威等级保护检查
              │
        ├── 旧记忆是 LOCKED → 禁止自动 UPDATE
        │                      另存新事实 + 冲突告警推送管理后台
        │
        └── 旧记忆是 NORMAL → 允许 UPDATE，记录变更历史


冲突处理流程（矛盾情形）:
    │
    ├── 旧记忆是 LOCKED → 新事实另存为 NORMAL（待管理员评判）
    │                      + 冲突告警推送
    │
    └── 旧记忆是 NORMAL → LLM 判断谁更可信
                          可信度高的覆盖低的（等同 UPDATE）
                          或两者标注"存在争议"均保留

注：DELETE 的触发场景（较少见）:
  - 帖子被明确标记为 OUTDATED（将对应记忆标记待审核）
  - WRONG 反馈 ≥ 3 次（从检索中降权，推送管理后台，由人工确认是否 DELETE）
  - 管理员手动删除
```

### 12.3 完整生命周期状态变化图

```
帖子关闭事件
      │
      ▼
  记忆创建
  authority = NORMAL 或 LOCKED
  status = ACTIVE
      │
      ▼
  直接生效（参与检索和 AI 回答生成）
      │
      ├── WRONG 反馈 ≥ 3 次 ──────────────────▶ 降权排除 + 管理后台确认是否删除
      │                                         └──▶ 管理员确认删除 → DELETED
      │
      ├── useful_ratio > 0.8 且反馈 ≥ 10 ─────▶ 推荐管理员确认晋升为 LOCKED
      │
      ▼（正常生命周期）
  ACTIVE（正常工作）
      │
      ├── NORMAL 记忆 180 天未检索 ──────────▶ COLD（降温）
      │
      │   LOCKED 记忆 → 永远不自动 COLD
      │
      ├── COLD 记忆被用户检索命中 ───────────▶ ACTIVE（复活）
      ├── COLD 超 365 天未复活 ─────────────▶ ARCHIVED（从 ES 移除，仅 PG 保留）
      │
      ├── 管理员手动归档 ─────────────────────▶ ARCHIVED
      ├── 管理员手动删除 ─────────────────────▶ DELETED（软删除）
      │
      ├── ARCHIVED 管理员恢复 ───────────────▶ ACTIVE（重新同步 ES）
      └── DELETED 管理员恢复 ────────────────▶ ACTIVE
```

### 12.4 多帖子同主题的记忆演进图

> 说明同一类问题被多次提问时，记忆如何随时间演进。

```
第 1 次帖子关闭（ai_resolved）
    │
    ▼
创建记忆 M-001
  content: "payment-gateway 超时默认 30s"
  authority: NORMAL

第 2 次帖子关闭（human_resolved，同主题）
    │
    ▼
AUDN 检测到 M-001 与新事实相似
  新事实: "payment-gateway 超时配置在 application.yml 中设置 timeout=30s，
           K8s 环境下需同时设置 service mesh 的 timeout"
    │
    判断: 新事实包含更多信息，旧内容已被补充
    │
    ├── 旧记忆 M-001 是 NORMAL → 允许 UPDATE
    ▼
UPDATE M-001
  content: （更新为包含 K8s 配置的更完整版本）
  authority: NORMAL → LOCKED（human_resolved 来源触发晋升）
  操作日志: 记录变更前后内容，来源帖子 ID

第 3 次帖子（OUTDATED 标记，旧版本配置）
    │
    ▼
AUDN 检测到 M-001 与新事实明显矛盾
  新事实: "v3.0 起 timeout 配置移至 config-center，application.yml 方式已废弃"
    │
    判断: M-001 此时已经是 LOCKED
    │
    ▼
禁止自动 UPDATE → 新事实另存为 NORMAL + 冲突告警推送管理后台
  管理员审核后决定：更新 M-001 内容 / 保留两条 / 归档旧的
```

---

## 13. 非功能性设计

### 13.1 性能目标

| 指标 | 目标 |
|------|------|
| 写入 API 响应时间 | < 200ms（原文存储同步返回，提取异步） |
| 检索 API 响应时间（P95） | < 500ms（含 Reranker 精排） |
| 记忆提取端到端延迟 | < 30s（含 LLM 调用） |
| 系统吞吐量 | 100 QPS 检索，50 QPS 写入 |

### 13.2 可用性

- API 服务无状态，支持水平扩展
- 存储层（PG/ES）按各自最佳实践做高可用
- 写入管道异步化，LLM 不可用时不影响原文存储，提取任务由 Dagster 自动重试

### 13.3 安全与权限

- **namespace 级访问控制：** 板块设置 public/internal/restricted
- **记忆级访问控制：** access_level 与 clearance_level 比对
- **敏感信息脱敏：** 提取前自动检测并脱敏密码、Token、密钥等
- **审计日志：** 所有记忆操作完整记录
- **更正通知：** 高引用量记忆被标记错误时通过引用链通知受影响用户

### 13.4 可扩展性

- 多模态预留：metadata 中预留 has_image、image_description 字段
- 批量导入/导出：Markdown/CSV/JSON 格式
- Embedding 模型可替换：ES 支持重索引
- 知识合成：碎片记忆自动合成为系统性指南

---

## 14. 分阶段实施计划

### Phase 0: 技术验证（2 周）

- 搭建 LLM 调用链路，验证 Prompt 效果（事实提取准确率、AUDN 决策准确率）
- 验证 ES dense_vector 检索性能
- 验证递归压缩的压缩比和信息保留率
- 确定 LLM 选型（主力模型 + 压缩用小模型）
- 验证 Reranker 模型效果
- 搭建 Dagster 基础环境

### Phase 1: 核心引擎（5~6 周）

- 三层记忆管道实现（原文→压缩→记忆）
- 写入管道（事件驱动 + Dagster 编排 5 步 Op）
- 检索引擎（ES 混合检索 + Reranker 精排 + 权威等级加权）
- 二级权威等级 + 四状态生命周期 + AUDN 保护机制
- 环境/版本 metadata 支持（自由格式 + LLM 推断）
- 反馈采集 + 反向引用链
- 核心 API + 接口文档

### Phase 2: 知识论坛系统（4~5 周）

- 板块管理 + 帖子 CRUD
- 帖子状态机（3 个状态 + 流转规则）
- 发起人点赞最佳回答 + 关闭帖子的 UX 设计
- AI 自动回复（基于记忆库）
- 搜索（帖子 + 记忆融合）+ 黑话字典
- 反馈按钮集成

### Phase 3: 管理后台 + 自动化（4~5 周）

- 记忆管理后台（仪表盘 + 记忆列表 + 待处理中心 7 种事项）
- Dagster 自动化管道（质量评估/合并检测/僵尸清理/报表）
- 论坛运营仪表盘 + AI 解决率漏斗看板
- 权限控制（namespace ACL + 记忆级 access_level）

### Phase 4: 高级功能（持续）

- 知识合成（碎片→指南）
- ES 重索引机制（Embedding 模型升级）
- 跨 namespace 检索 + 知识地图
- 开放 API + SDK（为未来其他系统接入预留）
- 按需引入 Redis（QPS 增长后的高频写入缓冲）

---

## 15. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| LLM 提取质量不稳定 | 记忆内容不准确 | 状态驱动（只提取 RESOLVED 帖子）+ 反馈闭环自动降权 |
| AI 回答错误未被及时发现 | 错误记忆影响后续回答 | 反馈驱动质控（WRONG ≥ 3 自动降权）+ 管理员异常推送 + 引用链更正通知 |
| 记忆膨胀 | 存储成本、检索变慢 | 衰减淘汰（COLD→ARCHIVED）+ 自动合并 + 知识合成压缩碎片 |
| 发起人不点赞关闭帖子 | 提取触发不及时 | 超时自动关闭（TIMEOUT_CLOSED）+ 后台 pending_confirm 机制 |
| 无版本概念的知识被误匹配 | 推荐错误上下文 | environment 字段自由格式 + LLM 判断是否有版本语义，无版本知识不参与版本过滤 |
| 语义漂移/模型升级 | 检索效果下降 | ES 重索引 + 黑话字典 + Reranker 兜底 |
| 隐私敏感信息入库 | 合规风险 | 提取前脱敏 + namespace 隔离 + 记忆级 access_level |
| LLM 服务不可用 | 提取/检索中断 | 写入异步化 + Dagster 自动重试 + 检索降级为纯全文搜索 |
