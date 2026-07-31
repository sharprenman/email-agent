# DeepAgents Email 企业级后端复刻开发方案

## 1. 文档目标

本文档用于指导在 `deepAgents_email/` 中复刻 `9900-H16C-Cake/` 的邮件 Agent 后端，并在后端完成后复制原 Next.js 前端、进行必要的协议适配和前后端联调。

本阶段只输出开发方案，不实施代码迁移、依赖安装、前端复制或业务开发。

## 2. 已确认范围

- 原项目：`9900-H16C-Cake/`
- 目标目录：`deepAgents_email/`
- 后端：使用 Python + DeepAgents 重新开发，不复制原 ConnectOnion 编排实现。
- 前端：后续复制 `9900-H16C-Cake/oo-chat/`，保留页面和交互，只修改必要的连接层代码。
- 联调：覆盖多轮会话、工具调用、人工审批、异常反馈和会话恢复。
- 邮件能力：Gmail、Outlook 与阿里邮箱开放平台使用相同领域接口，并明确各上游 API 的能力差异。
- 交付标准：可测试、可观测、可部署、可审计、可回滚，不以“本机能运行”作为完成标准。
- 部署模式：首期为单用户私有部署，但身份、凭证、线程和记忆边界按可扩展为多用户服务的方式设计。
- 产品入口：优先交付 Web/API，不把 CLI 作为首期阻塞项。
- 数据库：自建 PostgreSQL；开发环境通过容器提供，生产部署保留独立配置能力。
- 审批策略：所有产生外部副作用的操作默认要求人工审批。
- 代码语言约定：项目自有的 Python docstring、多行 Prompt、模块说明和面向用户文本尽量使用中文；代码标识符、API 字段、标准协议值和第三方库固定文本保持其规范语言。

## 3. 原项目运行逻辑

原后端的主要执行链如下：

```text
用户输入
  -> 意图识别（直接回复或进入执行）
  -> 串行 Planner（生成 skill/agent 步骤和 reads 依赖）
  -> Skill 参数解析与 schema 校验
  -> Python Skill 或通用 ReAct Agent 执行
  -> Finalizer 汇总结构化步骤结果
  -> USER_PROFILE / USER_HABITS / WRITING_STYLE 记忆回写
  -> 返回响应并延续 session
```

外部能力包括：

- Gmail/Outlook 邮件读取、搜索、正文、附件、发信和未回复邮件查询。
- Google/Microsoft Calendar 查询与事件写入。
- CRM 联系人初始化及 Gmail 发信后的联系人同步。
- 周报、紧急邮件、Bug 邮件、候选人简历、回复草稿、成品邮件发送、退订发现、退订执行、写作风格画像等工作流。
- CLI、HTTP Agent 服务、Next.js 聊天页面和 Docker Compose 部署。

## 4. 复刻策略与取舍

### 4.1 推荐策略：功能等价，不做内部代码一比一翻译

DeepAgents 已提供任务规划、Todo、子代理、Skills、上下文压缩、Checkpointer、持久化 Backend 和人工审批。新项目不再复制原来的 `IntentLayerOrchestrator`、YAML Planner、Skill Resolver 和 Finalizer 基础设施，否则会同时维护两套规划系统，增加模型调用次数、故障面和测试成本。

新执行链建议为：

```text
FastAPI 请求
  -> 身份、限流和请求校验
  -> DeepAgents 主 Agent
       -> Todo 规划
       -> 按需读取 Skill
       -> 委派只读邮件、邮件写入、日历等子代理
       -> 调用确定性业务工具
       -> 遇到副作用操作时 interrupt
  -> Checkpointer 保存线程状态
  -> StoreBackend 保存用户长期记忆
  -> 统一 API 响应或 SSE 事件
```

### 4.2 保留确定性代码的边界

下列逻辑不能只写在 Prompt 或 `SKILL.md` 中：

- OAuth token 管理和刷新。
- 邮件供应商 API 调用、超时、错误映射和重试策略。
- 退订头解析、RFC 8058 请求和 mailto 执行。
- 写操作审批、权限校验和幂等控制。
- 附件类型、大小及文本提取安全限制。
- 用户、线程和长期记忆的数据隔离。
- API 请求校验、错误码、日志脱敏和审计记录。

DeepAgents Skill 负责告诉 Agent “何时以及如何组合工具”，Python Service/Tool 负责安全地完成实际动作。

### 4.3 前端复制策略

可以复制原前端，但不能保证完全零修改。原 `oo-chat/app/api/chat/route.ts` 面向 ConnectOnion 的 `/info`、`/input` 和 Ed25519 签名协议。新后端采用版本化 HTTP/SSE 契约，因此只保留 UI、状态管理和审批组件，对以下位置做手术式调整：

- `app/api/chat/route.ts`：转发至新后端 `/api/v1/chat` 或流式端点。
- 会话状态：从原 `agentSession` 切换为后端 `thread_id`。
- 审批交互：把 DeepAgents interrupt 转换成现有审批卡片可识别的事件。
- 环境变量：统一为服务端后端地址和服务间凭证，浏览器不接触邮件 token 或后端密钥。

不建议为了前端零修改而重新实现 ConnectOnion 私有传输协议；这会引入与核心邮件功能无关的维护成本。

## 5. 目标架构

```text
Next.js UI
  -> Next.js server-side API proxy
    -> FastAPI /api/v1
      -> API Router / Schema / Exception Handler
      -> ChatApplicationService
        -> DeepAgents Supervisor
          -> Mailbox Reader Subagent（只读）
          -> Mail Writer Subagent（写操作需审批）
          -> Calendar Subagent（写操作需审批）
          -> Memory/Profile 能力
          -> DeepAgents Skills
        -> Provider Ports
          -> Gmail Adapter
          -> Outlook Adapter
          -> AliMail OpenAPI Adapter
          -> Google Calendar Adapter
          -> Microsoft Calendar Adapter
          -> AliMail Calendar Adapter
      -> LangGraph Checkpointer（线程状态）
      -> StoreBackend（用户长期记忆）
      -> Audit/Observability
```

### 5.1 建议目录

```text
deepAgents_email/
├── backend/
│   ├── pyproject.toml
│   ├── src/email_agent/
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── router.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   └── errors.py
│   │   ├── agents/
│   │   │   ├── __init__.py
│   │   │   ├── runtime.py
│   │   │   ├── loader.py
│   │   │   ├── results.py
│   │   │   ├── definitions.toml
│   │   │   ├── prompts/
│   │   │   └── tools/
│   │   ├── skills/
│   │   │   ├── catalog.py
│   │   │   ├── workflows.py
│   │   │   └── <skill-name>/SKILL.md
│   │   ├── persistence.py
│   │   ├── tools/
│   │   ├── services/
│   │   ├── domain/
│   │   ├── providers/
│   │   │   ├── gmail/
│   │   │   ├── outlook/
│   │   │   ├── alimail/
│   │   │   ├── google_calendar/
│   │   │   └── microsoft_calendar/
│   │   ├── security/
│   │   ├── observability/
│   │   └── core/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── contract/
│       └── evaluation/
├── frontend/                 # 后续从 oo-chat 复制
├── deploy/
├── scripts/
├── .env.example
├── docker-compose.yml
├── DEVELOPMENT_PLAN.md
└── README.md
```

目录以职责分层，不为单次使用的代码创建无意义抽象。具体文件只有在对应步骤开始开发时才创建。

### 5.2 模块化治理原则

- `src/email_agent` 是 Python 包根目录，不是所有实现的平铺目录；新增代码按
  `agents`、`api`、`services`、`domain`、`providers`、`persistence`、`security`
  等职责归属模块。
- Agent 运行时装配、声明加载和 Prompt 资源分别放在 `agents/runtime.py`、
  `agents/loader.py`、`agents/definitions.toml` 与 `agents/prompts/`，避免把编排、
  权限和大段提示词堆在单文件中。
- `agents/runtime.py` 只负责 Agent 图装配；Agent 输出契约放入 `agents/results.py`，
  邮箱读取、邮件写入和日历 Tool 按权限域放入 `agents/tools/`，不得把 Tool 实现重新
  堆回运行时。
- `definitions.toml` 只能从 Python 硬编码的最小权限上限中选择工具，不能通过配置给
  子代理扩权；副作用工具必须同时配置 DeepAgents interrupt。
- Prompt 与 TOML 作为 Python 包资源加载，禁止依赖进程当前工作目录；构建时必须检查
  wheel 中包含全部定义和提示词。
- 模块依赖方向为 API → 应用服务/Agent → 领域端口 → Provider/持久化适配器，业务核心
  不反向依赖 HTTP、数据库或供应商 SDK。
- 第 11 步的业务 Skill 保持独立目录；Skill 描述工作流，确定性校验和副作用仍由 Python
  服务承担，不与 Agent Prompt 混写。
- Web/API 运行时使用 StateBackend 虚拟文件注入内置 Skill，不允许为读取 Skill 启用可访问
  宿主磁盘的 FilesystemBackend；调用方不能覆盖 `/skills/email/` 下的内置资源。
- 线程 Checkpointer、用户 Store、长期记忆白名单和 PostgreSQL 生命周期集中在
  `persistence.py`；Agent 运行时只负责注入资源和装配 CompositeBackend，不包含数据库建表、
  连接管理或记忆内容校验。

## 6. 核心技术决策

| 领域 | 决策 | 原因 |
|---|---|---|
| Web 框架 | FastAPI + Pydantic | 明确 API 契约、异步支持、OpenAPI 和依赖注入 |
| Agent 框架 | DeepAgents `create_deep_agent` | 原生规划、Skills、子代理、文件系统和 HITL |
| 模型初始化 | `load_dotenv()` + `init_chat_model(f"openai:{raw_model}", use_responses_api=False)` | 适配当前 OpenAI 兼容服务的已验证调用方式，禁止擅自改写模型标识 |
| 会话状态 | LangGraph Checkpointer + `thread_id` | 支持多轮会话、interrupt 与恢复 |
| 长期记忆 | DeepAgents `CompositeBackend`，`/memories/` 路由至用户级 StoreBackend | 跨线程持久化并防止用户间泄漏 |
| 本地持久化 | Docker Compose 自建 PostgreSQL | 尽早验证与生产一致的会话、审批和记忆持久化行为 |
| 生产持久化 | 自建 PostgreSQL Checkpointer/Store | 支持可靠持久化、备份和多实例部署 |
| 邮件接入 | Provider Port + Gmail/Outlook/AliMail Adapter | 统一领域能力，同时允许供应商差异 |
| 副作用安全 | DeepAgents `interrupt_on` + 服务层二次校验 + 幂等键 | Prompt 不是安全边界 |
| 接口协议 | `/api/v1` JSON；聊天提供同步接口及 SSE 流式接口 | 便于前端代理、调试和演进 |
| 配置 | Pydantic Settings + 环境变量 | 类型校验，禁止硬编码密钥 |
| 质量工具 | pytest、pytest-asyncio、ruff、mypy、覆盖率、依赖安全扫描 | 建立自动化质量门禁 |
| 部署 | Docker Compose 开发环境；容器化生产配置 | 与原项目使用方式接近且便于联调 |

DeepAgents 依赖版本必须在 PoC 通过后精确锁定到 lockfile，不直接使用无限制的最新版范围。

## 7. 初始 API 契约

最终字段以接口设计评审结果为准，初始边界如下：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health/live` | 进程存活检查，不访问外部服务 |
| GET | `/health/ready` | 检查配置、持久化和必要依赖是否可用 |
| POST | `/api/v1/chat` | 同步聊天及多轮线程调用 |
| POST | `/api/v1/chat/stream` | SSE 输出模型、工具、审批和完成事件 |
| POST | `/api/v1/threads/{thread_id}/resume` | 提交 approve/edit/reject，恢复 interrupt |
| GET | `/api/v1/threads/{thread_id}` | 查询当前线程的可公开状态 |
| DELETE | `/api/v1/threads/{thread_id}` | 按权限清理线程数据 |

聊天请求至少包含：

- `message`：长度受限的用户消息。
- `thread_id`：可选；首次由服务端生成，后续复用。
- `attachments`：可选；必须使用受控文件 ID，不能接受任意服务器路径。
- `idempotency_key`：写操作恢复或网络重试时使用。

身份必须从可信认证信息中解析，不能相信请求正文传入的 `user_id`。响应使用统一错误结构，不返回堆栈、模型密钥、OAuth token、服务器路径或完整敏感邮件内容。

## 8. 人工审批与权限矩阵

| 操作 | 默认策略 |
|---|---|
| 搜索、读取邮件和附件元数据 | 无审批，受用户邮箱权限约束 |
| 读取附件正文 | 无交互审批，但执行文件类型、大小和解析器白名单 |
| 生成回复草稿 | 无审批，不产生外部副作用 |
| 发送邮件/回复邮件 | 必须审批，展示收件人、主题、正文和附件摘要 |
| 创建、修改、删除日历事件 | 必须审批，删除操作显著标识 |
| RFC 8058 或 mailto 退订 | 必须审批，并逐个展示目标与执行方式 |
| 网站退订 | 首期只返回人工链接，不执行浏览器自动化 |
| 修改邮箱状态或联系人 | 根据操作分类审批并写审计日志 |

审批配置必须落实到工具和服务边界；即使模型绕过 Skill，未批准的写操作也不能执行。

## 9. 分步开发计划

以下共 18 步。每一步完成并通过验收后再进入下一步；涉及架构或契约变化时先评审，不边写边猜。

### 第 1 步：建立需求基线与功能追踪矩阵

开发内容：

- 将原项目所有用户能力、CLI 能力、Skills、Provider 差异、审批点和部署方式整理成追踪矩阵。
- 为每项能力标记“完整复刻、行为调整、延期或不支持”。
- Gmail 与 Outlook 按同期交付建立功能矩阵，确认所有副作用操作默认审批。

验证：

- 每个原 Skill、主要工具和用户入口都有唯一追踪编号。
- 未确认项进入决策记录，不允许通过开发者个人猜测落地。

### 第 2 步：完成 DeepAgents 技术 PoC 与版本锁定

开发内容：

- 用最小 Agent 验证 `create_deep_agent`、自定义工具、子代理、Skills、Checkpointer、StoreBackend 和 `interrupt_on`。
- 验证当前选定模型的工具调用、结构化输出和上下文长度能力。
- 确认同步调用、异步调用、SSE 事件和 interrupt 恢复方式。
- PoC 通过后锁定 Python 与核心依赖版本。

验证：

- 一个测试线程能够调用假工具、触发审批、恢复执行并跨请求保留状态。
- 重启后能按生产持久化设计恢复线程或明确说明本地模式限制。

### 第 3 步：初始化后端工程骨架和质量门禁

开发内容：

- 建立 `src` 布局、依赖分组、配置模块、FastAPI 应用工厂和测试目录。
- 配置 ruff、mypy、pytest、覆盖率及依赖安全扫描。
- 建立 CI：安装锁定依赖、静态检查、单元测试、集成测试和镜像构建。
- 增加 `.env.example`，只写变量名和安全示例，不提交真实凭证。

验证：

- 空骨架可以启动并通过 `/health/live`。
- CI 在干净环境中可重复通过；故意引入 lint 或测试错误时会阻止合并。

### 第 4 步：定义领域模型和 Provider 接口

开发内容：

- 定义 Email、Thread、Attachment、Contact、CalendarEvent、UnsubscribeOption 等领域 DTO。
- 定义邮件和日历 Provider Protocol，仅暴露业务需要的方法。
- 定义统一异常：认证失效、限流、超时、权限不足、资源不存在和上游不可用。
- 明确 Gmail 与 Outlook 搜索能力差异，不强行把 Gmail 查询语法传给 Outlook。

验证：

- 使用 Fake Provider 完成读信、搜索、发信、日历查询和错误映射测试。
- Agent/Service 层不导入 Gmail 或 Microsoft SDK 的具体类型。

### 第 5 步：实现配置、身份和密钥安全

开发内容：

- 使用 Pydantic Settings 校验模型、数据库、邮件 Provider、时区和服务间认证配置。
- 建立 OAuth token 存储/刷新接口，日志和异常中统一脱敏。
- 首期实现单用户私有部署，但通过统一 `AuthContext`/身份依赖传递用户身份；业务代码禁止直接依赖“永远只有一个用户”的全局常量，为后续多用户认证留出稳定边界。
- 对请求体大小、附件大小、并发和超时设置明确上限。

验证：

- 缺失必需配置时应用快速失败并给出不含秘密的错误。
- 自动化测试确认 token、API key 和邮件正文不会出现在普通日志中。

### 第 6 步：实现 Gmail Adapter

开发内容：

- 实现身份查询、收件箱、搜索、正文、已发送邮件、附件、未回复邮件、发信/回复等基础能力。
- 设置连接/读取超时；只对安全幂等的读取请求执行有限重试。
- 将 Gmail 原始响应转换成统一领域 DTO。
- 发信接口接收幂等键，防止前端重试造成重复邮件。

验证：

- 使用 Mock/Recorded API 完成成功、空结果、分页、token 过期、限流、超时和权限不足测试。
- 沙箱 Gmail 账号完成最小端到端验证，不使用个人生产邮箱作为自动化测试依赖。

### 第 7 步：实现 Outlook Adapter 和能力降级

开发内容：

- 使用 Microsoft Graph 实现与统一接口对应的能力。
- 为 Gmail 独有功能返回明确 capability 状态，不伪造成功。
- 把 Outlook 文件夹、分类和查询方式封装在 Adapter 内。

验证：

- 相同 Provider 合约测试可分别运行于 Gmail Fake 和 Outlook Fake。
- 前端/Agent 能区分“不支持”“未授权”“暂时失败”。

### 第 8 步：实现日历 Adapter 与写操作保护

开发内容：

- 实现 Google Calendar 和 Microsoft Calendar 的查询、创建、更新、删除接口。
- 所有写操作必须同时通过 DeepAgents interrupt 和 Service 审批凭证校验。
- 对时间、时区、参与者、重复事件和删除目标进行确定性校验。

验证：

- 未批准、审批内容被篡改、审批过期和重复恢复均不能产生写操作。
- 批准后只执行预览中展示的那一次操作。

### 第 9 步：实现附件与退订确定性工具

开发内容：

- 迁移附件元数据获取和受控文本提取，增加 MIME、扩展名、大小、超时和临时文件隔离。
- 迁移 List-Unsubscribe/List-Unsubscribe-Post 解析。
- 实现 discovery、RFC 8058 POST、mailto 和 manual website 四类结果。
- 退订状态持久化，防止重复执行并保留审计证据。

验证：

- 覆盖恶意文件名、超大附件、不支持类型、解析失败和路径穿越测试。
- 退订测试覆盖 one-click、mailto、website、unknown、已退订、部分失败和重复提交。

### 第 10 步：构建 DeepAgents 主 Agent 和最小权限子代理

开发内容：

- 创建 Supervisor，负责理解请求、规划 Todo、选择 Skill、委派和整合结果。
- 创建 Mailbox Reader，只提供只读邮箱和附件工具。
- 创建 Mail Writer，只提供草稿相关能力和受审批保护的发送工具。
- 创建 Calendar Agent，只提供日历工具并区分读写权限。
- 子代理必须配置清晰描述、任务边界、输出要求和独立工具白名单。

验证：

- 权限测试证明只读子代理无法发信、退订或修改日历。
- 多步骤请求能按依赖顺序执行，失败步骤不会被总结成成功。

### 第 10.1 步：完成 Agent 模块化治理

开发内容：

- 将第 10 步单文件按职责拆为运行时装配、配置加载、Agent 声明和独立 Prompt 文件。
- 用 TOML 声明 Agent 描述、工具选择、可选工具和 interrupt，运行时按声明完成装配。
- 在 Python 中维护不可由配置突破的工具权限上限，校验未知 Agent、重复名称、路径穿越、
  未知工具、越权工具和副作用工具漏配审批。
- 使用包资源读取 Prompt 和 Agent 声明，对外继续由 `email_agent.agents` 提供稳定入口。

验证：

- 现有 Agent 行为与审批测试全部通过，拆分不改变业务结果。
- 配置越权、重复 Agent、不安全 Prompt 路径和漏配 interrupt 均启动失败。
- 构建 wheel 并确认 TOML 与全部 Prompt 被包含，不依赖源码目录才能运行。

### 第 10.2 步：完成 Agent Tool 模块化

开发内容：

- 将监督代理、邮箱读取、邮件写入和日历 Tool 从运行时拆至 `agents/tools/`，按权限域独立维护。
- 将 `AgentTaskResult`、状态枚举和失败优先聚合规则拆至 `agents/results.py`。
- `agents/runtime.py` 仅保留配置读取、Tool 注册、子代理装配和 DeepAgents 图创建。
- 保持 Tool 名称、输入参数、返回结构、审批校验及 `email_agent.agents` 公共导入兼容。

验证：

- 原有 Agent、Tool 白名单、审批、可信用户和失败聚合测试全部通过。
- Ruff、Python 编译、全量后端测试与 wheel 构建通过。
- `runtime.py` 不再包含邮箱、退订和日历 Tool 的业务适配实现。

### 第 11 步：迁移九类业务 Skill

开发内容：

- 将原 `registry.yaml + Python execute_skill` 重构为 DeepAgents Skill 目录和确定性工具组合。
- 首批迁移：weekly summary、urgent triage、bug triage、resume review、draft reply、send prepared email、unsubscribe discovery、unsubscribe execute、writing style profile。
- 每个 `SKILL.md` 只描述单一工作流、适用条件、安全边界、工具顺序和结果格式。
- 原 Skill 中复杂的筛选、状态机和副作用逻辑保留在 Python 服务中，不改写为自由文本推理。

验证：

- 每个 Skill 至少有正常、空结果、上游失败和越权调用测试。
- 使用原项目查询样例建立回归集，检查行为和证据一致性。

### 第 12 步：实现线程状态和长期记忆

开发内容：

- 使用 Checkpointer 保存消息、Todo、interrupt 和线程执行状态。
- 使用 CompositeBackend：线程工作区走 StateBackend，`/memories/` 走 StoreBackend。
- 将 profile、habits、writing style 迁移为用户级记忆文件，并设置 user namespace。
- 记忆写入采用白名单路径和内容规则，稳定事实与一次性对话分开。

验证：

- 同一用户不同线程可读取长期偏好；不同用户完全隔离。
- 并发写入不静默覆盖，失败后不损坏已有记忆。
- Prompt injection 不能写入策略或其他用户命名空间。

### 第 13 步：实现企业级 FastAPI 接口

开发内容：

- 按 Router、Schema、Application Service、Agent Runtime、Provider 分层实现接口。
- 实现统一成功/错误响应、全局异常处理、request/trace ID 和超时控制。
- 实现同步聊天、SSE 聊天、线程查询、interrupt 恢复及线程删除。
- 对输入长度、枚举、附件数量、线程 ID、审批操作和幂等键做 Pydantic 校验。
- OpenAPI 中记录认证、状态码和事件结构。

验证：

- 契约测试覆盖 2xx、400、401、403、404、409、422、429、503 和 504 场景。
- 异常响应不包含堆栈、内部路径、token 或原始上游响应。

### 第 14 步：完善审计、日志、指标与追踪

开发内容：

- 输出结构化日志：trace ID、thread ID、匿名用户标识、Agent/工具名称、耗时和结果状态。
- 邮件地址、主题、正文、附件内容、OAuth token 和模型密钥默认不写日志。
- 对模型调用、工具调用、上游 API、审批等待、错误率和延迟建立指标。
- 集成可选 LangSmith/OTel 追踪；未配置时不影响核心运行。
- 对所有写操作记录审批人、预览摘要哈希、幂等键和最终结果。

验证：

- 能从一个 trace ID 定位完整调用链，但无法从普通日志还原敏感正文。
- 关键错误和副作用操作具备可审计记录。

### 第 15 步：复制前端并完成协议适配

开发内容：

- 将原 `oo-chat/` 复制为目标项目 `frontend/`，排除 `.next/`、`node_modules/` 和本地秘密文件。
- 保持组件和视觉样式，最小修改 server-side API proxy、thread ID 和 SSE 解析。
- 将 DeepAgents interrupt 映射为现有审批 UI 的 approval/edit/reject 操作。
- 服务间认证只保存在 Next.js server 环境变量中。

验证：

- 前端构建通过，没有复制缓存、依赖目录或秘密。
- 浏览器网络请求中不出现邮件 OAuth token、数据库凭证或后端服务密钥。

### 第 16 步：执行前后端联调与核心旅程验收

开发内容：

- 联调普通对话、搜索邮件、查看正文、周报、紧急邮件、简历附件、草稿、发信审批、日历审批和退订审批。
- 验证多轮上下文、刷新页面后的线程恢复、网络重试和部分失败提示。
- 验证 Gmail/Outlook/AliMail capability 差异能被用户看懂。

验证：

- 每条核心旅程有可重复的验收脚本和预期结果。
- 任何工具失败都不会被 Agent 表述为已成功完成。

### 第 17 步：修复工程基线

开发内容：

- 隔离自动化测试与本地 `.env`、终端环境变量，禁止真实配置改变缺失配置测试的前提。
- 将代码直接导入的 `langchain`、`langchain-core` 和 `pydantic` 声明为直接依赖，并更新锁文件。
- 复验 `uv sync --frozen`、pytest、Ruff 和语法编译。
- 检查密钥文件、缓存、日志和临时文件是否进入版本控制。

验证：

- 后端全量测试零失败，且在预置邮箱及审批环境变量的终端中结果一致。
- `uv sync --frozen` 可以从锁文件还原环境，直接导入的包均为直接依赖。
- Git 不跟踪 `.env`、token、缓存、日志或临时测试数据。

### 第 18 步：完成阿里邮箱真实联调

开发内容：

- 验证访问令牌、身份、收件箱、搜索、正文、附件、联系人和错误映射。
- 验证草稿、发送、回复、标记已读以及阿里日历查询和审批写入。
- 验证限流、超时、权限不足、审批拒绝和幂等重放。
- 删除项目负责人允许删除的测试草稿，并形成真实联调记录。

验证：

- 阿里邮箱可以通过现有 API 向指定测试地址发送邮件，并返回真实邮件或草稿标识。
- 审批前不产生副作用，批准后只执行一次；日志不包含 Secret、token 或邮件正文。
- 邮件与日历权限不足时返回稳定、可审计的中文错误。

### 第 19 步：完成 PostgreSQL 持久化

开发内容：

- 完成 Checkpointer、Store、线程所有权、幂等记录和一次性审批消费记录的 PostgreSQL 落库。
- 增加数据库初始化、健康检查、连接释放和进程重启恢复。
- 验证并发写入、乐观锁、多实例共享状态和用户命名空间隔离。

验证：

- 重启后可以恢复线程、长期记忆和待审批任务。
- 重复幂等键、重复审批和并发恢复不会产生重复发信、退订或日历写入。
- 不同用户不能读取或修改对方线程、记忆和 Provider 状态。

### 第 20 步：补齐业务功能差距

开发内容：

- 在项目负责人确认其属于 V1.0 后，复刻 CRM 初始化、联系人画像、待回复联系人和发信后同步。
- 实现前端附件上传、受控文件 ID、文件生命周期和权限校验。
- 保持 API、Provider、Agent Tool 和 Skill 的现有模块边界，不为单次功能引入额外框架。

验证：

- 原项目要求保留的业务能力均有对应实现和自动化测试。
- 用户上传附件只能通过受控 ID 使用，不能注入任意本地路径或跨用户读取。
- CRM 数据进入用户隔离的长期存储；若 CRM 不属于 V1.0，必须在差异清单中明确签字排除。

### 第 21 步：完成真实模型和 Agent 回归

开发内容：

- 使用项目指定的 `init_chat_model` 方式运行中文业务回归集。
- 验证 Supervisor 的 Skill/SubAgent 选择、多步骤依赖、空结果、部分失败和工具异常。
- 测试 Prompt injection、越权工具调用和虚假成功，并按失败样本最小调整 Prompt、Skill 或工具说明。

验证：

- 常见任务选择正确 Skill，读操作不会调用写工具。
- 发信、退订、日历和长期记忆写入始终进入审批。
- 工具失败不会被总结为成功，回复不编造邮件、联系人、日期或已完成动作。

### 第 22 步：完成前后端完整联调

开发内容：

- 联调聊天、SSE、邮件搜索、正文、附件、联系人、草稿、发信、日历和退订。
- 联调批准、修改后批准、拒绝、重复提交、断流、超时和后端不可用。
- 完成浏览器点击、审批卡片、刷新后线程恢复和视觉验收。

验证：

- 页面状态与后端真实状态一致，审批卡片不暴露敏感字段。
- 刷新后可以恢复线程，SSE 断开不会把未完成操作显示为成功。
- 重复点击和网络重放不会重复执行副作用。

### 第 23 步：完成安全、性能和可靠性验收

开发内容：

- 测试服务认证、线程越权、用户隔离、路径穿越、SSRF、恶意附件和敏感信息泄漏。
- 对聊天、模型和 Provider 执行并发、超时、取消、限流和故障恢复测试。
- 检查重复发送、重复退订、重复日历事件和重复审批等幂等场景。
- 执行依赖漏洞、前端依赖和容器镜像扫描。

验证：

- 高风险副作用无法绕过审批、幂等和用户隔离。
- 日志不存在凭证及敏感正文泄漏，上游失败返回明确失败或部分成功。
- 性能基线、容量假设和已知瓶颈写入运行手册；未达标项不能静默上线。

### 第 24 步：完成私有部署交付

开发内容：

- 提供后端、前端和 PostgreSQL 的 Dockerfile 与 Compose，生产配置不内置秘密。
- 增加数据库初始化或迁移、健康检查、优雅关闭、备份恢复和回滚说明。
- 编写 README、配置表、邮箱授权指南、API 示例、故障排查和运维手册。
- 按功能追踪矩阵逐项签收，形成最终已知差异清单。

验证：

- 新环境执行 `docker compose up -d` 后，前端、后端和 PostgreSQL 均通过健康检查。
- 自动化测试、前端构建、镜像构建、迁移演练、重启恢复和核心冒烟测试全部通过。
- 发布包不包含 `.env`、OAuth token、缓存、测试邮箱数据或个人信息。
- 另一名开发者能够只按照交付文档独立完成部署和回滚。

## 10. 测试策略

### 10.1 测试分层

- 单元测试：领域校验、错误映射、Skill 辅助逻辑、审批策略和幂等逻辑。
- Provider 合约测试：同一组测试验证 Gmail/Outlook Adapter 的公共行为。
- 集成测试：FastAPI、Checkpointer、StoreBackend、PostgreSQL 和 Fake Provider。
- Agent 回归测试：使用固定模型响应或测试模型，避免 CI 依赖不稳定的真实 LLM。
- 评估测试：使用真实模型定期运行原项目查询集，评价工具选择、证据性和安全性。
- 前端契约测试：校验 JSON/SSE/interrupt 事件能被 UI 正确处理。
- 端到端测试：使用测试邮箱和日历账号覆盖关键用户旅程。

### 10.2 最低质量门禁

- ruff、mypy、pytest 全部通过。
- 新增核心业务逻辑必须有测试；总覆盖率目标不低于 80%，审批、幂等和权限模块目标不低于 90%。
- 不允许通过删除测试、降低断言或吞掉异常使 CI 通过。
- 外部 API 测试默认 mock；真实账号测试独立运行且凭证由安全环境注入。
- 合并前检查变更范围，不混入无关格式化或重构。

## 11. 企业级非功能要求

### 安全

- 最小权限 OAuth scope，token 加密存储并支持撤销。
- 用户、线程、记忆和 Provider 凭证强隔离。
- 所有写操作实行审批、幂等和审计。
- 附件解析在受控环境中执行，不允许任意路径和任意命令。

### 可靠性

- 所有外部调用必须设置超时。
- 只重试幂等操作；写操作依赖幂等键和状态机。
- 支持优雅关闭、请求取消和中断恢复。
- 上游失败返回明确的部分成功/失败结果，不伪造完成。

### 可观测性

- 请求、Agent、子代理和工具调用共享 trace ID。
- 日志结构化且默认脱敏。
- 具备延迟、错误率、token 消耗、上游限流和审批等待指标。

### 可维护性

- API 层不包含 Provider SDK 细节。
- Agent Prompt/Skill 不承担确定性安全校验。
- Gmail 与 Outlook 共用领域契约，但不隐藏真实能力差异。
- 依赖精确锁定，升级必须通过回归集。

## 12. 主要风险及应对

| 风险 | 影响 | 应对 |
|---|---|---|
| DeepAgents API 版本变化 | interrupt、backend 或 subagent 行为变化 | 第 2 步 PoC 后锁版本，升级单独走回归 |
| 原项目部分能力依赖 ConnectOnion 工具 | 无法直接迁移 | 用 Provider Adapter 重写，不引用 ConnectOnion 运行时 |
| LLM 规划结果不稳定 | 工具选择或步骤遗漏 | 确定性工具、结构化结果、回归集和关键流程服务化 |
| 前端协议与新后端不一致 | 聊天或审批无法联调 | 先冻结 JSON/SSE 契约，再最小修改 API proxy |
| 多用户记忆泄漏 | 严重隐私事故 | Store namespace 使用可信用户身份并做隔离测试 |
| 网络重试造成重复发信/退订 | 外部副作用 | 幂等键、审批记录和状态机 |
| Outlook 能力弱于 Gmail | 用户体验不一致 | capability 模型、明确降级、分 Provider 验收 |
| 附件包含恶意内容 | 资源消耗或安全风险 | 白名单、大小限制、隔离解析和超时 |
| 自动化测试依赖真实 LLM/邮箱 | CI 不稳定且可能产生费用 | Fake Provider、固定模型、真实 E2E 独立执行 |

## 13. 阶段性交付物

### 阶段 A：基础平台（第 1—5 步）

- 功能追踪矩阵、技术 PoC、锁定依赖、工程骨架、领域接口和安全配置。

### 阶段 B：业务能力（第 6—12 步）

- 邮件/日历 Adapter、附件和退订工具、DeepAgents 主/子代理、九类 Skills、线程和长期记忆。

### 阶段 C：产品集成（第 13—16 步）

- 企业 API、可观测性、复制并适配前端、核心旅程联调。

### 阶段 D：真实能力收口（第 17—20 步）

- 工程基线、阿里邮箱真实联调、PostgreSQL 持久化和业务差距补齐。

### 阶段 E：产品验收（第 21—22 步）

- 真实模型回归、Agent 行为评估和前后端完整联调。

### 阶段 F：上线交付（第 23—24 步）

- 安全、性能、可靠性、容器化、运维文档、发布和回滚验收。

## 14. 开发启动前必须再次确认的决策

### 14.1 已确认决策

1. Gmail 与 Outlook 同期开发。
2. 首期采用单用户私有部署，但按可扩展多用户服务设计身份、线程、凭证和记忆隔离边界。
3. 发信、退订、日历写入、邮箱状态修改等外部副作用默认全部人工审批。
4. 优先交付 Web/API；CLI 不作为首期阻塞项。
5. PostgreSQL 由项目自建。
6. 当前没有 Gmail、Outlook 和 Calendar 联调账号；前期使用 Fake Provider 和合约测试，进入真实 OAuth/E2E 阶段时再由项目负责人提供。
7. 根目录已有 `.env`；配置开发时应复用并校验已有变量，不擅自覆盖文件或编造配置值。
8. 项目自有的 `"""..."""` 内容尽量使用中文，但标准协议、API schema 和第三方固定值不强制翻译。
9. 模型配置读取根目录 `.env` 中的 `MODEL`；模型必须按以下已知可用方式初始化，不自动拆分或重写 `raw_model`：

   ```python
   dotenv.load_dotenv()

   raw_model = os.getenv("MODEL", "openai/gpt-5.1")
   model = init_chat_model(
       f"openai:{raw_model}",
       use_responses_api=False,
   )

   agent = create_deep_agent(model=model)
   ```

10. 浏览器通过 Next.js 服务端代理访问 FastAPI，浏览器不直接持有服务间认证信息。
11. PostgreSQL 开发环境由本项目 Docker Compose 启动，不要求宿主机单独安装。

### 14.2 后续阶段所需外部条件

真实邮箱账号不是第 1—5 步的前置条件。进入 Gmail/Outlook OAuth 集成和端到端联调阶段前，需要分别准备 Google Cloud OAuth 应用、Microsoft Entra 应用及对应的测试邮箱/日历账号。需要这些条件时应提前告知项目负责人，不得临时编造配置或使用个人生产邮箱替代测试账号。

## 15. 参考依据

- 原项目代码与 `docs/current-solution/` 文档；发生冲突时以运行代码为准。
- DeepAgents 官方概览：<https://docs.langchain.com/oss/python/deepagents/overview>
- DeepAgents 定制与子代理：<https://docs.langchain.com/oss/python/deepagents/customization>
- DeepAgents 子代理工具隔离：<https://docs.langchain.com/oss/python/deepagents/subagents>
- DeepAgents Skills：<https://docs.langchain.com/oss/python/deepagents/skills>
- DeepAgents Backends：<https://docs.langchain.com/oss/python/deepagents/backends>
- DeepAgents Memory：<https://docs.langchain.com/oss/python/deepagents/memory>
- DeepAgents Human-in-the-loop：<https://docs.langchain.com/oss/python/deepagents/human-in-the-loop>
- Gmail 附件读取：<https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments/get>
- Microsoft Graph 文件附件：<https://learn.microsoft.com/en-us/graph/api/resources/fileattachment?view=graph-rest-1.0>
- RFC 8058 One-Click Unsubscribe：<https://www.rfc-editor.org/rfc/rfc8058.html>

## 16. 当前实施进度

### 第 1 步：已完成

- 已完成原项目功能基线和架构决策，关键结论统一保留在本文档中，不再拆分额外过程文档。

### 第 2 步：已完成（PostgreSQL 实例验证后移）

- 已锁定 DeepAgents 0.6.12，并确认支持 Python 3.14。
- 已按项目指定方式完成真实模型连接。
- 已验证类型化工具调用、子代理委派、interrupt 批准/拒绝、同线程上下文和 StoreBackend 用户隔离。
- 当前机器没有 Docker，因此 PostgreSQL Checkpointer 的容器重启恢复验证转入正式持久化步骤，不将内存验证冒充生产持久化验证。

### 第 3 步：已完成

- PoC 源码已清理，正式后端收敛为最小 FastAPI 主流程。
- 已建立应用入口、模型构造、统一存活检查响应和 API 测试。
- 已锁定正式依赖，并通过语法、pytest、Ruff 和 ASGI 冒烟检查。

### 第 4 步：已完成

- 已用单个 `contracts.py` 集中定义邮件、日历领域模型、Provider 能力、异步协议和统一异常，避免过早拆分文件。
- Gmail、Outlook、Google Calendar 和 Microsoft Calendar 后续实现必须遵守同一契约，并通过 capability 显式表达差异。
- 已验证字段约束、日历时间范围、邮件发送必填项、异常重试属性以及完整/不完整 Fake Provider 的运行时协议识别。

### 第 5 步：已完成

- 已集中实现环境配置、敏感值封装、外部调用超时与请求/附件大小边界。
- 生产环境缺少服务间认证配置时会快速失败，配置对象不会在字符串输出中暴露敏感值。
- 首期单用户通过统一 `AuthContext` 进入应用状态，为后续多用户认证和 StoreBackend namespace 保留稳定边界。
- 模型仍严格使用项目已验证的 `dotenv.load_dotenv()` 与 `init_chat_model` 构造方式。

### 第 6 步：代码与自动化验证已完成（真实 OAuth 联调待外部条件）

- 已用单个 `gmail.py` 实现 Gmail 身份、收件箱、搜索、正文、已发送、未回复、附件元数据、People 联系人、发信/回复和标记已读能力。
- 读取请求使用有限重试并支持分页；写请求禁用客户端自动重试、要求幂等键，持久化幂等和审批仍由后续服务层负责。
- 已实现配置化 HTTP 超时、OAuth 凭证刷新、Google 错误到统一 Provider 异常的映射，以及响应到领域 DTO 的转换。
- 已用 Fake Google Service 验证成功、空结果、分页、凭证失效、权限不足、资源不存在、限流、超时和服务异常，共有 28 项后端测试通过。
- 真实 Gmail/People API 端到端验证需要项目专用 Google Cloud OAuth 应用和测试邮箱；在这些外部条件就绪前不把 Mock 测试表述为真实联调。

### 第 7 步：代码与自动化验证已完成（真实 Entra 联调待外部条件）

- 已用单个 `outlook.py` 实现 Microsoft OAuth token 刷新，以及身份、收件箱、搜索、正文、已发送、未回复、附件元数据、联系人、发信/回复和标记已读能力。
- Graph 列表严格沿用完整 `@odata.nextLink` 分页；读取请求有限重试，写请求不自动重试，401 刷新 token 后只重放一次未通过认证的请求。
- 新邮件通过“创建草稿后发送”返回稳定草稿 ID；回复通过 `createReply` 保留 Outlook 会话关系。两类写操作均要求幂等键，持久化幂等和审批继续由后续服务层负责。
- 第 7 步暂未开放 Outlook 退订头和附件正文能力；第 9 步已基于 Graph 的
  `internetMessageHeaders` 与文件附件 `contentBytes` 补齐这两项能力。
- 已用 Mock Graph 验证分页、空结果、token 轮换、读取重试、写操作不重试、权限/限流/超时/服务异常和统一 Provider 合约；当前共有 43 项后端测试通过。
- 真实 Microsoft Graph 端到端验证需要项目专用 Entra 应用、委托权限、refresh token 和测试邮箱；条件就绪前不把 Mock 测试表述为真实联调。

### 第 8 步：代码与自动化验证已完成（真实日历联调待外部条件）

- 已用单个 `calendar.py` 实现 Google Calendar 与 Microsoft Calendar 的时间窗口查询、分页、创建、更新、删除和受限重复规则映射。
- 已增加 IANA 时区、时间范围、参与者邮箱、重复频率、重复星期和结束条件的确定性校验；查询窗口最多 366 天。
- 所有日历写操作默认拒绝，必须提供由 DeepAgents interrupt 恢复层签发的一次性审批凭证；凭证绑定可信用户、操作、目标事件、完整请求内容和幂等键，过期、篡改、错用或重复消费都会在调用外部 API 前失败。
- 新实现不复制原项目“headless 无审批渠道时直接放行”以及 Outlook 日历未挂审批插件的行为；Google 与 Microsoft 写操作采用同一保护规则。
- Google 创建事件使用幂等事件 ID，Microsoft 创建事件使用 `transactionId`；供应商写请求均不自动重试，避免网络重放产生重复副作用。
- 当前一次性消费记录是单进程内存实现，满足首期单用户私有部署；扩展为多实例服务前必须在 PostgreSQL 持久化步骤中替换为带唯一约束的共享消费记录。
- 已用 Fake Google Calendar 与 Fake Graph 验证分页、字段映射、重复规则、错误映射、缺失审批、内容篡改、审批过期和重复恢复；当前共有 54 项后端测试通过。
- 真实端到端验证仍需要 Google Calendar scope、Microsoft `Calendars.ReadWrite` 委托权限及对应测试账号。

### 第 9 步：代码与自动化验证已完成（真实退订联调待外部条件）

- 已为 Gmail 和 Outlook Provider 增加附件正文下载，附件提取只支持
  TXT、Markdown、HTML、PDF 和 DOCX，并同时校验 MIME、扩展名、声明大小与实际大小。
- 附件解析全程使用内存，不采用不可信文件名创建磁盘文件；恶意路径会收敛为安全展示名。
  解析任务受超时、输出长度、PDF 页数和 DOCX 解压正文大小限制。
- 已解析 `List-Unsubscribe` 与 `List-Unsubscribe-Post`，支持 RFC 8058 one-click、
  mailto、人工网站链接和 unknown 四类确定性结果。网站退订永远不自动执行。
- one-click 只有在 HTTPS、DKIM 通过证据和 DKIM 签名头覆盖两个退订字段同时满足时才会开放；
  执行严格发送 `List-Unsubscribe=One-Click` 表单，并禁用重定向。
- one-click 与 mailto 均需一次性审批凭证，审批绑定可信用户、候选指纹、完整候选内容和幂等键。
  网络结果不确定时不会自动重试，避免重复副作用。
- 首期单用户状态采用原子替换的最小 JSON 存储，仅保存哈希化目标、幂等哈希、状态码和证据哈希，
  不保存原始退订令牌；多实例部署前必须替换为带唯一约束的 PostgreSQL 实现。
- 已覆盖恶意文件名、路径穿越、超限、格式伪装、解析失败、解析超时、one-click、mailto、
  website、unknown、缺失 DKIM、重复提交和批量部分失败；当前共有 63 项后端测试通过。
- 真实端到端验证仍需要项目专用测试邮箱及可安全退订的测试邮件列表，不能使用生产订阅做验证。

### 第 10 步：代码与自动化验证已完成（真实模型行为评估后移）

- 已在 `agents/` 模块中装配 DeepAgents Supervisor、`mailbox-reader`、`mail-writer` 和
  `calendar-agent`，未提前创建第 11 步的业务 Skill 或第 13 步的 HTTP 聊天接口。
- Supervisor 只持有无副作用的结果聚合工具；邮箱、邮件写入和日历业务能力必须通过 `task`
  委派。DeepAgents 自动提供的 `general-purpose` 子代理只能继承该聚合工具，无法接触业务 Provider。
- `mailbox-reader` 只允许身份、收件箱、搜索、正文、已发送、待回复、附件、联系人和退订发现；
  工具白名单中不存在发信、执行退订或任何日历写入。
- `mail-writer` 只允许结构化草稿、受审批发信和可选的受审批退订；草稿结果明确包含
  `sent=false`，不能被表述为已发送。
- `calendar-agent` 只允许日历查询及受审批的创建、修改、删除。日历和退订执行继续使用既有
  Provider/Service 二次审批，发信新增同等级的服务层一次性审批校验。
- 所有副作用工具首次调用时由 DeepAgents `interrupt_on` 中断；模型不能生成有效审批凭证，
  后续 API 恢复层必须在用户批准后注入与请求内容、可信用户和幂等键绑定的凭证。
- 子代理统一返回 `AgentTaskResult`。确定性聚合规则保证任一步失败时总体不能为 success，
  混合成功与失败只能为 partial，并保留失败原因与证据。
- 已验证 Agent 图实际构建、三个工具白名单、草稿未发送、发信审批不可绕过、可信用户透传、
  退订隔离和失败聚合；完成模块化配置测试后当前共有 76 项后端测试通过。
- 真实模型的工具选择准确率、长任务规划质量和 Prompt 回归将在第 11 步业务 Skill 数据集与
  第 16 步端到端联调中验证，当前不把结构测试表述为真实模型验收。

### 第 10.1 步：Agent 模块化治理已完成

- 运行时装配已迁入 `agents/runtime.py`，Agent 声明和 Prompt 分别位于
  `agents/definitions.toml` 与 `agents/prompts/`，加载和安全校验集中在
  `agents/loader.py`。
- TOML 只负责从代码白名单中选择工具，不能扩大任何 Agent 权限；未知工具、重复 Agent、
  Prompt 路径穿越以及副作用工具漏配 interrupt 都会阻止服务启动。
- `email_agent.agents` 保留稳定导入入口，调用方不需要了解内部文件结构。
- 已增加包资源加载与配置安全测试；wheel 已包含 TOML 和四个 Prompt 资源。

### 第 10.2 步：Agent Tool 模块化已完成

- 监督代理、邮箱只读、邮件写入和日历 Tool 已分别迁入 `agents/tools/supervisor.py`、
  `agents/tools/mailbox.py`、`agents/tools/mail_writer.py` 和 `agents/tools/calendar.py`，
  权限域之间没有相互导入。
- Agent 结果契约和失败优先聚合规则已迁入 `agents/results.py`。
- `agents/runtime.py` 已缩减为 Agent 图装配模块，不再实现邮箱、退订或日历 Tool；
  原有公共导入、Tool 契约和双层审批行为保持不变。

### 第 11 步：九类业务 Skill 已完成代码与自动化验证

- 已迁移 weekly email summary、urgent email triage、bug issue triage、
  resume candidate review、draft reply、send prepared email、unsubscribe discovery、
  unsubscribe execute 和 writing style profile 九类工作流。
- 每个 Skill 使用独立目录和中文 `SKILL.md`，包含适用条件、输入规则、执行流程、安全边界、
  空结果、上游失败和结果要求；原项目的动态 `registry.yaml + execute_skill` 不再复制。
- `skills/catalog.py` 启动时校验固定九项、目录与名称一致、必需章节、直接工具白名单和委派工具
  声明，并打包为 `/skills/email/` 下的 StateBackend 虚拟文件。
- `skills/workflows.py` 确定性生成时间窗口、搜索查询、结果上限和委派工具组合；
  Supervisor 必须先调用无副作用的 `prepare_skill_workflow`，Prompt 不能自行扩大范围。
- 内置 Skill 只允许直接使用 `task`、`prepare_skill_workflow` 和
  `merge_subagent_results`；具体邮箱、日历和副作用工具仍受三个子代理白名单、interrupt、
  服务层审批和幂等校验约束。
- 已验证 DeepAgents 原生 SkillsMiddleware 能识别全部九项，StateBackend 注入后模型系统提示
  包含 Skill 目录；调用输入不能覆盖内置 Skill 文件。
- writing style 本步骤只生成可供持久化的画像，不虚假声称已经跨会话保存；用户级 StoreBackend
  写入与隔离在第 12 步实现。
- 当前共有 104 项后端测试通过；真实模型的 Skill 选择准确率和长任务回归仍按第 16 步
  端到端数据集验收，当前不把结构与模拟模型测试表述为真实模型行为验收。

### 第 12 步：线程状态和长期记忆已完成代码与自动化验证

- Agent 已接入 LangGraph Checkpointer；消息、Todo、interrupt 和线程工作区随 `thread_id`
  保存，默认工作区继续使用 StateBackend。
- 已使用 CompositeBackend 将 `/memories/` 路由到 StoreBackend，并固定为
  `profile.md`、`habits.md`、`writing-style.md` 三类用户级记忆。
- Store namespace 使用可信 `user_id`，运行时上下文与构建 Agent 时的可信身份不一致会被拒绝；
  首期单用户部署与后续多用户隔离采用同一命名空间规则。
- 通用文件工具只能读取长期记忆，不能直接写入；`save_user_memory` 是唯一写入口，必须经过
  DeepAgents interrupt 审批，并且要求先读取版本后执行乐观版本写入。
- 记忆内容仅允许固定中文标题、二级标题和事实条目，同时限制总大小、单行长度并拦截常见
  Prompt injection 特征；一次性对话、邮件正文、联系人、凭证和策略指令不得持久化。
- 已提供 PostgreSQL Checkpointer/Store 生命周期工厂并锁定
  `langgraph-checkpoint-postgres` 与 psycopg 依赖；生产环境缺少 `DATABASE_URL` 时配置校验失败。
- 已验证同用户跨线程读取、不同用户隔离、并发版本冲突、Store 失败不损坏旧内容、非白名单
  路径和指令注入拒绝、记忆审批中断入检查点；当前共有 118 项后端测试通过。
- 因当前没有 PostgreSQL 连接信息，本步骤未声称完成真实 PostgreSQL 建表和重启恢复联调；
  获得数据库后需补充真实连接、进程重启恢复和多实例并发验证。

### 第 13 步：企业级 FastAPI 接口已完成代码与自动化验证

- 已按 Router、Schema、Application Service、Agent Runtime 和 Provider 边界实现
  `POST /api/v1/chat`、`POST /api/v1/chat/stream`、线程查询、interrupt 恢复和线程删除。
- HTTP 成功响应统一为 `code/message/data/request_id/trace_id`；400、401、403、404、409、
  422、429、503 和 504 使用稳定错误码，异常响应不返回堆栈、服务器路径、token 或上游原文。
- 使用 `X-Service-Token` 执行服务间认证；生产环境必须配置，开发/测试环境可显式不配置。
  用户身份只从服务端 `AuthContext` 获取，请求正文携带 `user_id` 会因额外字段被拒绝。
- 首次聊天由服务端生成 thread ID，并在 Store 中记录可信用户所有权；查询、继续、恢复和删除
  均先验证所有权。同线程执行使用进程内串行锁，幂等键重复返回 409。
- interrupt 恢复通过 `interrupt_id + decisions` 精确匹配待审批动作；API 恢复层会覆盖模型生成的
  幂等键并签发绑定最终参数的一次性审批凭证，客户端不能注入审批 token 或更换工具名称。
- SSE 使用命名事件 `thread/message/tool/approval_required/completed/error`；工具事件只返回
  工具名称和调用 ID，不返回工具参数，审批卡片事件才返回经敏感字段过滤的待确认参数。
- 增加 request/trace ID 中间件、请求体声明长度限制、Agent 超时、统一异常处理和
  `/health/ready`。应用未装配 Agent/Persistence 时聊天和就绪检查明确返回 503，
  `/health/live` 始终不访问外部依赖。
- Chat、审批、附件引用、线程 ID、审批动作、编辑参数、请求幂等键和操作幂等键均由 Pydantic
  严格校验；OpenAPI 已记录服务认证、错误状态和 `text/event-stream`。
- 当前没有受控附件上传/解析服务，非空附件引用会返回明确 503，不接受或转发任意本地路径；
  前端附件协议和上传能力在第 15—16 步联调时补齐。
- 已验证真实 DeepAgents interrupt 经 HTTP 应用服务恢复后写入长期记忆，并完成 146 项后端
  自动化测试；真实模型、真实邮箱和 PostgreSQL 仍按第 16 步端到端条件验收。

### 第 14 步：审计、日志、指标与追踪代码已完成

- 新增单文件 `observability.py` 作为独立可观测性模块，没有把日志、指标和追踪逻辑继续堆入
  `runtime.py`，也没有拆出只含少量代码的多层目录。
- HTTP、Agent、模型、工具、Provider 上游调用和审批等待共用 request ID、trace ID、thread ID
  与匿名用户引用；HTTP 指标使用路由模板作为名称，避免线程 ID 形成高基数标签。
- 普通日志采用固定字段白名单，只允许组件名称、结果、耗时、异常类型和关联 ID；回调不会读取
  Prompt、邮件参数、工具输出或上游原文，原始用户标识使用进程密钥 HMAC 后再记录。
- 指标使用有界聚合，包含调用次数、成功/失败、总耗时、最大耗时及模型输入/输出 token；
  Provider 相关工具同步形成上游调用指标，限流和超时按异常类型聚合。
- LangChain 回调 metadata 支持部署环境按标准方式启用 LangSmith；OTel 通过可选 TraceSink
  适配器接入，默认使用空实现，不配置时不会导入 SDK 或发送外部遥测。
- 所有经 interrupt 审批的邮件、退订、日历和长期记忆写操作都会记录审批人匿名引用、
  预览摘要哈希、操作幂等键哈希、审批结果和最终执行结果；拒绝操作记录为未执行。
- 新增脱敏、token 指标、工具/上游指标、可选追踪和写操作审计测试；在可用的 Python 3.14
  依赖组合中，除未安装 `pypdf` 的附件专项外共 142 项测试通过，相关模块定向回归全部通过。
- 当前包下载链路无法下载锁文件中的二进制包，因此尚未声称完成 `uv sync --frozen` 后的
  151 项正式全量复验；已完成语法编译、差异检查和非 PDF 广泛回归，下载恢复后只需补跑
  锁定环境的 pytest 与 Ruff，不需要人工邮箱、模型、数据库或密钥。

### 第 15 步：前端复制与协议适配已完成

- 仅复刻原 `oo-chat` 的聊天视觉和核心交互，没有复制 `.next`、`node_modules`、`.env`、
  ConnectOnion 地址簿、浏览器私钥、外部中继、浏览器直连模型、语音和演示设置。
- 前端按页面、组件、协议契约、SSE 解码和服务端代理模块化管理；服务认证只由 Next.js
  服务端读取，浏览器只访问同源 `/api/agent/*`，客户端静态包未发现服务端认证字段。
- 已适配聊天流、thread ID、工具状态及 DeepAgents interrupt 的批准、修改后批准和拒绝操作；
  浏览器仅在 `sessionStorage` 保存 thread ID，不持久化邮件正文或聊天内容。
- 服务端代理校验聊天和线程输入，透传安全的 request/trace ID，限制上游地址协议，并把非
  JSON 上游错误统一转换为中文受控响应，避免将内部响应原文返回浏览器。
- SSE 解码器覆盖跨数据块、CRLF、多行 data、无效 JSON 和超大未完成事件；4 项单元测试、
  TypeScript 严格检查、ESLint、Next.js 生产构建和 `npm audit` 均通过，依赖审计为 0。
- 本地 HTTP 冒烟确认首页返回 200、无效请求返回 400、后端未启动时合法请求返回中文 502；
  当前 FastAPI 运行时尚未完成生产装配，因此真实 Agent 成功流、审批恢复和刷新后线程恢复
  不在本步骤虚假签收，按第 16 步在后端可启动后完成。
- 当前环境缺少应用内浏览器技能文件，本步骤未声称完成浏览器视觉验收；已完成生产构建、
  HTTP 冒烟和客户端静态包扫描，视觉及真实邮箱端到端交互留到第 16 步补验。

### 第 16 步：前后端联调进行中

- 已增加独立 `bootstrap.py`，按 `MAIL_PROVIDER=gmail|outlook|alimail` 显式装配邮箱、日历、审批、
  退订、附件、Agent 和持久化资源；不设置隐式 Provider 默认值。
- 开发环境未配置数据库时使用进程内持久化，配置 `DATABASE_URL` 后使用 PostgreSQL；
  生产环境继续强制要求 PostgreSQL、服务认证、审批签名密钥和邮箱 Provider。
- FastAPI lifespan 会在启动时注入 `AgentApplicationService`，并在关闭时释放 Outlook、
  日历、退订 HTTP 客户端和 PostgreSQL 资源；未选择 Provider 的开发进程保持存活但明确
  `/health/ready=503`。
- 前端新增同源 `/api/agent/health`，状态从固定“就绪”改为“检查中 / 就绪 / 服务未配置”，
  避免后端 503 时向用户显示绿色成功状态。
- 后端全量 162 项测试、变更文件 Ruff、语法编译、前端 4 项测试、TypeScript、ESLint、
  Next.js 生产构建和依赖审计均通过；依赖审计为 0。
- 本地真实进程已验证 `/health/live=200`、未装配时 `/health/ready=503`、聊天接口 503、
  前端首页 200、同源聊天代理和健康代理安全透传中文错误及 request/trace ID。
- 当前尚未选择首个真实 Gmail/Outlook 账号，也没有对应 OAuth 配置和 PostgreSQL；
  因此真实邮件搜索、正文、周报、紧急邮件、附件、发信审批、日历审批、退订审批、刷新恢复
  和进程重启恢复尚未签收。
- 当前 Codex 会话没有可用浏览器实例，未完成点击、审批卡片和刷新恢复的视觉验收；后续获得
  浏览器实例及真实 Provider 配置后继续本步骤，不把 HTTP 冒烟表述为完整端到端验收。

### 阿里邮箱 API Provider 增量

- 按模块拆分为 `providers/alimail/client.py`、`mail.py` 和 `calendar.py`：认证与 HTTP、
  邮件领域映射、日历审批写入互不混放，邮件与日历共用一个访问令牌和连接池。
- 支持标准版/AI 尊享版官方地址与国产化版本官方地址，不允许环境变量注入任意 API Host；
  通过 `ALIMAIL_API_EDITION=standard|localized` 选择。
- 运行配置为 `MAIL_PROVIDER=alimail`、`ALIMAIL_CLIENT_ID`、`ALIMAIL_CLIENT_SECRET`、
  `ALIMAIL_ACCOUNT_EMAIL`；阿里邮箱免费版不支持开放平台，企业管理员还需为应用授权邮件、
  通讯录和日历权限。
- 邮件能力覆盖身份、收件箱/未读、KQL 搜索、详情、已发送、未回复、附件、共享通讯录、
  草稿发送/回复和标记已读；日历覆盖查询及默认审批的创建、修改、删除。
- API 路由、Agent Tool 和前端协议保持不变，通过现有 Provider 契约直接接入；真实企业账号
  凭证和管理员授权到位后，再完成真实邮件、日历与审批端到端验收。

### 第 17 步：已完成

- `langchain`、`langchain-core` 和 `pydantic` 已声明为直接依赖并写入 `uv.lock`；
  `uv sync --frozen` 可以从锁文件还原环境，未引入第三方版本漂移。
- pytest 在收集测试前清理应用配置环境变量，并离开包含本地 `.env` 的后端目录；普通环境与
  预置生产、邮箱、模型和审批变量的环境均为后端 182 项测试通过。
- 后端全量 Ruff 和 `src`、`tests` 语法编译通过。
- `.env` 与前端本地环境文件未被 Git 跟踪，未发现 token、缓存、日志或临时测试数据进入
  版本控制。

### 第 18 步：已完成

- 2026-07-29 使用标准版阿里邮箱开放平台完成真实联调：应用 token、账号访问、收件箱、
  已发送、搜索、正文、附件、共享联系人、日历查询和真实 404 错误映射均通过；联调输出仅含
  数量、状态和脱敏 ID，不包含 Secret、token、邮件正文或联系人内容。
- 完成结构化草稿、未审批默认拒绝、审批后自发信、回复、自投递、标记已读、审批凭证重放拒绝，
  以及无参与人日程的未审批拒绝、创建、更新、重放拒绝和删除；本步骤共留下 4 封自发测试邮件
  作为联调记录，草稿遗留数为 0，测试日程已删除。
- 真实环境发现并修复两项协议差异：平台会重写 `internetMessageId`，现按保留的幂等 Header
  确认正式邮件 ID；附件下载会话返回 `302` 和站内相对 `Location`，现仅允许 HTTPS 地址或
  当前固定 Host 下的单斜杠绝对路径。
- 为避免主动触发生产限流或修改应用权限，权限不足、限流、网关超时和网络超时通过受控 HTTP
  响应验证，分别稳定映射为中文 Provider 错误；真实当前凭证的邮件、联系人和日历授权均可用。
- 阿里邮箱专项 10 项测试、后端全量 185 项测试、全项目 Ruff 和 `src`、`tests` 语法编译通过；
  临时附件只在内存中比对，未写入项目或日志。

### 第 19 步：已完成

- 2026-07-30 使用独立 `email_agent` 数据库完成 PostgreSQL 17.10 真实验证；LangGraph
  Checkpointer、Store、线程归属、请求幂等、审批消费和退订状态均由数据库持久化。
- 线程执行使用 PostgreSQL advisory lock 跨实例串行化；请求幂等、审批凭证及其绑定的操作、
  退订占位使用唯一约束或条件写入，长期记忆直接对 LangGraph Store 执行版本条件更新。
- `/health/ready` 会实际查询数据库；应用生命周期关闭后释放 Checkpointer、Store 和应用状态
  连接池。两个独立 Python 进程已验证线程、长期记忆和待审批检查点可在进程退出后恢复。
- 真实数据库测试覆盖两个持久化实例并发写入、相同幂等键、不同审批凭证绑定同一操作、
  退订占位、记忆乐观锁、关闭重开恢复和不同用户命名空间隔离；随机测试记录已全部精确清理。
- `uv sync --frozen`、含真实 PostgreSQL 的后端全量 186 项测试、全项目 Ruff 和 `src`、
  `tests` 语法编译均通过；未新增依赖，因此 `uv.lock` 无变化。

### 第 20—24 步：待开始

- 按本文档第 20—24 步依次完成业务差距、真实模型、前后端、安全可靠性和私有部署验收。
- 第 20 步开始前需要项目负责人确认 CRM 是否属于 V1.0 必须交付范围。
