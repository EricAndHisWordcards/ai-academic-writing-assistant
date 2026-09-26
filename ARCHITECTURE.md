# 项目定位与技术架构

> 本文回答三个问题：这个项目**是什么**、**不是什么**、**用什么技术实现**。
> 面向需要在几分钟内判断其技术形态的读者（访客、评审、协作者）。
>
> **文档导航**：[`README.md`](./README.md) 扉页（是什么 / 给谁用 / 怎么跑起来）· **本文**（怎么实现）·
> [`DECISIONS.md`](./DECISIONS.md) 为什么变成这样（演化账本）· [`DEPLOY.md`](./DEPLOY.md) 怎么部署 ·
> [`源码打包指南.md`](./源码打包指南.md) 怎么出包 ·
> [`AI学术写作辅助系统_PRD.md`](./AI学术写作辅助系统_PRD.md) 产品定义与验收标准。

## 一句话定位

**一个人机协同的、以提示词编排的确定性 LLM 工作流系统。**

它把「写一篇论文」拆成一条有状态的流水线：

```
选题 →（设计录入）→（主题聚类）→ 大纲 → 文献/材料注入 → 引用调度 → 分段生成 → 导出
```

每一步的产出都落库、可回看、可重来；两个关键节点必须由人确认才能继续（大纲、引用绑定）；而**决定「能不能往下走」的是代码里的闸门，不是模型**。

---

## 一、它不是什么

这三条是判断技术形态时最容易误判的地方，逐条附上可自行验证的反证。

### 1. 不是多智能体（Multi-Agent）系统

全部模型调用**都是单轮** `messages=[{"role": "user", "content": prompt}]`：没有对话历史累积、没有让模型决定「下一步做什么」、没有工具调用。全仓搜索 `tools=` / `tool_choice` / `function_call` **零命中**，`llm.py` 里根本没有工具参数这个入口。

依赖表里**没有任何 Agent 框架**——无 LangChain、无 LangGraph、无 AutoGen、无 CrewAI。后端依赖总共 10 个库（8 个运行时 + 2 个测试）。

### 2. 模型不决定流程走向

- 项目有 **10 个状态**（`backend/app/db.py` 的 `ProjectStatus`）：草稿 → 选题完成 → 大纲待确认 → 大纲已确认 → 资源注入中 → 引用待确认 → 引用已确认 → 生成中 → 生成完成 → 已导出。状态落库持久化，刷新页面不丢。**终态「已导出」的唯一写入点是 `POST /projects/{id}/export`**（回执路由，幂等）——在那之前它被定义过、被读过（`_CITATION_EDITABLE` 等三处），却没有一处写过它，而前端早就在"已完成状态"的集合里认它。
- 状态推进由**路由里的硬闸门**控制：哪类论文必须先有文献、哪类必须有引用绑定、哪个接口只对哪类论文开放（非文献综述调聚类接口直接 400）、同一状态的任务重复触发返回 409。
- 模型无权跳过引用调度、无权给自己加一章、无权改用另一类论文的骨架。它做的是**在给定槽位里填空**。

### 3. 「Agent」是职责命名，不是运行时实体

项目把处理链上每个环节各自封装成一个模块，命名为 xx Agent（共 10 个）。但其中**有一个根本不调用模型**：`schedule_agent.py` 是纯 Python 算法（按章节铺开绑定、补齐缺口、兜底分布），一次 LLM 调用都没有——它叫 Agent，只是因为它在流水线上占一个职责位。

其余 9 个模块共 **10 个调用点**（`outline_agent` 调两次：先定结构与标题，再独立分配字数），每个都是同一形状：

> **固定提示词 + 输入白名单 + 输出解析与校验**

其中 `relevance_agent.py`（判断哪几篇文献属于哪一章）那次调用有一个形态上的例外：它包在 `asyncio.wait_for(..., timeout=...)` 里，因此 `grep "await llm\."` 抓不到它——**任何失败（超时、不可解析、一条有效条目都没有）都不抛给调用方，而是返回 `None` 让整步退回确定性兜底**。它是唯一一个「调用失败不算失败」的调用点。

唯一存在的循环是 `generate_agent` 的字数校准重试（偏差 > 5% 时重新生成本节）——那是**质量重试**，不是自主决策。

---

## 二、与 Skills 的关系

**不是 Skills**：没有 `SKILL.md`，模型不能自选调用，它们是后端按固定时机调用的 Python 函数。

但有一处**结构上的亲缘**值得指出：每个 Agent 都是「封装好的过程 + 明确的适用条件 + 输入输出契约」，这正是 skill 的形态，只是触发者是代码而不是模型。可以说它是**一组服务端不可协商的技能库**——模型不能跳过引用调度、不能多加一章，也不能把质性研究的骨架拿去写数学论文。

---

## 三、它实际上是什么：带状态机的工作流引擎

真正与之同构的不是聊天应用，而是工作流引擎：

| 特征 | 在本项目中的体现 |
|---|---|
| 状态机 | 10 个状态，落库持久化，刷新不丢 |
| 人工闸门 | 两次必选确认（大纲、引用绑定），**后端强校验**，不是前端提示。两道闸门各是一条**共用的前置校验函数**（`_require_outline_confirmable` / `_require_citation_editable`），内容是同一套四件事：404、两套忙闲登记都查、上游产物在不在、**状态白名单**——判据是「这个状态有没有走到过这一步」，而不是「某个产物字段存不存在」（下游产物在，恰恰不能说明上游的确认点已经过）。写状态的下游入口只要有一条不查状态，闸门就废掉一个 |
| 中间产物 | 四份可回看的显式产物：设计字段、主题聚类、引用绑定、材料融入计划 |
| 异步任务 | 秒到分钟级操作统一登记为后台任务，进度落库、前端轮询；服务重启后僵死任务标记为 `interrupted`，且**在界面上如实说明原因、可重新发起**（包括刷新页面后才发现的那个中断，它是从项目快照读出来的，不经过轮询）——失败、部分失败、中断三种终态在界面上如实区分，不统一显示成「完成」 |
| 可回退 | 章节标题变化会作废引用绑定与材料计划（两者都以章节标题为 join key）；改选题/类型/字数/写作思路会**覆盖式作废**大纲、正文与四份下游产物。但回退有两条纪律：**只有四者真的变了才作废**（原样重提连状态都不写，否则一次路过点击就把已确认过大纲的项目打回上一步），**提交前逐项列明将失去什么**（作废不可撤销，告知是它唯一的护栏）。宁可让用户重做一次调度，也不允许带着悬空引用或按旧选题写成的稿子往下走。范围也可单独收窄：**只作废已生成的正文**是一个任何时候都可用的动作（`sections/reset`），它保留大纲、绑定、设计与材料计划——此前"想重写一版正文"只能靠改选题绕路，代价是把四份下游产物一起作废 |
| 回看 ≠ 回退 | 步骤条可在已走过的步骤间前后自由跳转，可达范围取「服务端前沿、本地只增不减的记忆、当前下标」三者最大值。导航本身不作废任何东西——否则用户点回前一步会以为后面的活被撤销了，转头把已经生成好的内容重新生成一遍 |
| 产物自洽 | **三件事必须对得上：正文、文末列表、文献库。**正文里的角标与文末参考文献列表**只在生成那一刻对齐**：此后再改文献或重做调度，界面挂一条**不可关闭**的告警条（`citations_stale`，每次读取现算的非列字段），并在**生成前拒绝**（400，校验放在路由层因而零写入）——不猜测、也不静默重写用户已经生成好的正文。第二条是**文献库 vs 绑定**（`unbound_documents`）：绑定只在调度那一刻定格，此后加进来的文献从未进入任何章节，而 `citations_stale` **结构上看不见它**（它按绑定里的 doc_id × 当前文献库重算再与落盘快照比对，未进绑定的新文献对快照零影响；"跳过引用 → 补文献 → 续写"这条路上两边都是空列表，`[] == []` 让它永远为假）。处置是把「执行引用调度」提为主按钮并说明有几篇未绑定，而不是再加一道拒绝。第三条是**"到底哪一样变了"**（`refs_change`）：同一条"不一致"其实分两种后果差一个量级的改动，只说"不一致"会让用户照错的那条去修。判据来自那个比较指纹本身（`_refs_key` 比 `(编号, doc_id, 排版文本)`）：两侧编号与指向一一对应而只有排版文本有差 → `render`，出路是**重排文末列表**（零模型调用、不碰正文，`references/rerender` 写下去的值与护栏比对**共用同一个快照函数**，所以完成即一致）；否则 → `numbering`，出路是**作废已生成的正文**（`sections/reset`，只清正文/基线/进度，大纲与四份下游产物一字不动）——重排对 `numbering` 一律 400，因为那时重排会亲手做出"正文角标指着 A、列表第 3 条写着 B"这种静默错配。三个事实**回答三个不同的问题，不能合成一句**（合成一句用户就会照着错的那条去修），三者都走同一组四个挂载点（项目详情 + 三个 citations 写接口），诊断与那句人话也**只在服务端算一份**（它有两个消费点：告警条与 400 文案）。**同类问题一律用代码校验而不是提示词约束**：润色前后逐节核对角标集合（多重集比对）、聚类 `doc_ids` 按库内实际文献做白名单、文献元数据的占位词在解析与显示两处清洗 |
| 派生字段只有一个推导点 | 论文标题（= 项目显示名）由服务端算一份（`display_title`：用户改的名字 → 研究核心方向 → 兜底词），前端只渲染——它有五个消费点（列表卡片 / 页头 / 删除确认 / 导出的一级标题 / 下载文件名），各写一遍兜底就是五份迟早漂移的实现。它随项目的**所有**返回值下发（含新建与确认选题的响应），因为前端拿到响应就直接替换本地副本 |
| 作者的话优先于模型的归纳 | 选题步可写下作者自己的**写作思路与论证思路**（`writing_ideas`：项目级、跨类型、选填、上限 2000 字）。它进**两处**：大纲第一遍（第一步声明的核心研究问题必须**收敛**这段思路，不得另立一条主线，作者点到的论证角度要落到节标题上）与**每一节正文**（v1.18 顺带把此前**只到大纲**的 `core_question` 也接进正文——写每一节的模型此前看不到全篇主线，只能从节标题反推，而节标题本身是被模型改写过的一句话）。两处都写明**作者原文优先于模型自己的推断、也优先于那句核心研究问题**。与设计字段、聚类块同一纪律：**空串 ⇒ 整段渲染为空串**，没填过的项目（存量全部如此）两处提示词逐字节不变。它按 `strip()` 后的值参与「四者全等才不动」的比对（只 strip、不折叠内部空白——多行文本里末尾一个换行是常态而非边界），真变了走同一套覆盖式作废；超上限由**路由内 400** 拒绝，前端刻意不镜像（静默截断会让作者的方案被吃掉一半而永不知情） |

这类形态的代价与收益都很明确：**灵活性低、确定性高**。模型每一步只能填槽，换来的是全流程可追溯、可中断、可复现。

---

## 四、技术栈

一句话概括：**没有重型框架**——无 ORM、无消息队列、无 Agent 框架、无状态管理库，`sqlite3` / `asyncio` / 标准库直上。

### 后端

| 组件 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| Web 框架 | FastAPI 0.115.6 + uvicorn 0.34.0 |
| 校验 / 配置 | pydantic 2.10.4 + pydantic-settings 2.7.0 |
| 模型调用 | `openai` 1.59.6（`AsyncOpenAI`）——**任意 OpenAI 兼容端点**（DeepSeek / 通义千问 / Moonshot / 智谱 等） |
| 存储 | SQLite（**原生 `sqlite3`，无 ORM**），单文件 `backend/data/app.db`，3 张表：`projects` / `documents` / `materials` |
| 并发 | 原生 `asyncio` 后台任务 + 进度落库（无 Celery / Redis）；**必须单进程运行**（任务登记表在进程内，理由见 [`DEPLOY.md`](./DEPLOY.md) §4.3） |
| PDF 解析 | pypdf 5.1.0，带**逐页索引**以便引用定位 |
| 材料解析 | `.docx` / `.xlsx` 用**标准库 `zipfile` + `ElementTree`** 直读 OOXML；`.txt/.md/.csv/.json/.log` 分级编码回退 |
| 上传与配置 | python-multipart 0.0.20（文件上传）+ python-dotenv 1.0.1（`.env`） |
| 测试 | pytest 8.3.4 + httpx，**1041 个用例，默认离线**（真实模型调用显式打桩） |

### 前端

| 组件 | 选型 |
|---|---|
| 框架 | React 18.3.1（**无状态管理库**） |
| 构建 | Vite 5.4.21 + @vitejs/plugin-react 4.7.0 |
| 主组件 | `src/App.jsx`（3,925 行），承载分步工作流 |
| API 客户端 | `src/api.js`，手写。非 2xx 一律把后端的 `{detail: "中文原因"}` 原样抛出（`errorDetail` 一处实现，三个入口共用），取不到才退回通用文案 |
| 导出 | `src/exporters.js`（纯 JS，不含 React）：Markdown / 纯文本 / **Word（.docx，手写 OOXML + 不压缩 zip）**。放在单独模块而不是塞进 `App.jsx`，一是三种格式共用同一份「导出文档」形状（标题、章节、参考文献只拼一次），二是它不含 React，可以直接用 Node 跑起来验证。**文件不经过后端**；后端只有一个回执路由（`POST /projects/{id}/export`），它只写状态 |

### 桌面端（已产出可分发 zip）

Electron 31.7.7 + electron-builder 24.13.3（`asar: false`，`win.target: ["zip"]`）；后端用
PyInstaller **onefile** 打包（`backend/run_desktop.spec`，`console=False`）→ 约 18.5 MB 的
`academic_backend.exe`，作为 `extraResources` 落到 `resources/backend/`。当前交付物
`frontend/release7/AI学术写作辅助系统-1.0.0-win.zip`（127,391,559 字节，2026-09-24 烘，
sha256 `89e74fcf461587077e1bc1594ba3e98f50c854b1665b5a3f1af4e39319599701`），
解压即用（免装 Python / Node）。

**输出目录随版本递增，已交付的包一个字节都不要动**——同名重建会覆盖那份说了「不动了」的 zip
（打包指南坑 3 就是这条）。哈希只说明"包里那份 == 我刚构建的那份"、**不说明"构建出来的跑的是
新代码"**（漏跑一次 PyInstaller 打出的包恰好就是"能开、不报错、改动一行没进去"），所以还要把
那个 exe **真跑起来**验过一次：一次性 `USERPROFILE` + 一个本地假模型端点（不联网、不用真实密钥、
不花钱）。**如实记一条边界**：我验的是**后端 exe**，**Electron 外壳没有双击验证过**；隐私逐项
扫描零命中（`.env` / `*.db` / 配置文件 / 密钥值 / 用户名 / 本机路径 / 真实项目标题全无）。

历代交付包与沿革见 [`DECISIONS.md`](./DECISIONS.md) 的「交付包沿革」。

打包态与源码态有四处**必须不同**，每一处错了都是静默失效（都已修，见下）：

| 事项 | 源码态 | 打包态 | 错了会怎样 |
|---|---|---|---|
| 数据库位置 | `backend/data/app.db` | `~/.academic_writer/app.db` | 落进 PyInstaller 的临时解压目录 `_MEIPASS`，每次启动都是新空库 ⇒ **关掉应用论文就没了** |
| 日志去处 | 控制台 | `~/.academic_writer/backend.log` | 窗口模式没有控制台，`sys.stdout/stderr` 是 `None`，第一次写日志就 `AttributeError` 退出 ⇒ **双击没反应且无痕**。被 Electron 拉起时 stdio 是管道（不是 `None`），那条兜底不触发，所以主进程再抄一份到同一个文件 |
| Vite `base` | `/` | `./` | 资源写成绝对路径 `/assets/...`，`file://` 下解析成 `file:///assets/...` ⇒ **窗口白屏** |
| 关后端 | — | **只杀 Python 子进程那一个 pid**（v1.28 起；pid 文件缺失 / 核对不过 / 父进程没按时退出则回落 `taskkill /T /F`） | `child.kill()` 只杀 PyInstaller 的父（解压）进程，跑 Python 的子进程活下来占着 8000 端口 ⇒ **下次启动新后端 bind 失败却被残留后端应答成正常**，且进程越攒越多。**这里原本写的做法是直接 `taskkill /PID <pid> /T /F`，代价是强杀让父进程来不及删 `_MEI`、每次开关在 `%TEMP%` 留约 28 MB** —— v1.28 换成单杀子进程之后这个代价连同它一起消失了（见打包指南坑 8 与 §八 第 28 条） |

配置与密钥的落点见下节；本地配置与打包态数据库同一个目录（`~/.academic_writer/`），
便于用户整体备份或迁移。

### API Key

密钥走 `.env`（`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`），已 gitignore，不入库；未配置密钥时以演示模式运行。

---

## 五、目录结构

```
.
├── backend/                    # FastAPI 后端
│   ├── app/
│   │   ├── main.py             # 应用入口 + /api/meta（下发型配置）
│   │   ├── config.py           # 配置加载（环境变量 > .env > 本地配置 > 默认值）
│   │   ├── local_config.py     # ~/.academic_writer/config.json 读写
│   │   ├── db.py               # SQLite 数据层 + 状态机 + 一次性迁移
│   │   ├── llm.py              # LLM 抽象层（OpenAI 兼容，单轮调用）
│   │   ├── tasks.py            # 后台任务登记（同 key 互斥）
│   │   ├── paper_types.py      # 七类论文类型的工序表与大纲骨架（单一事实来源）
│   │   ├── pdf_parser.py       # PDF 逐页文本提取（坏页留空 + 点名，不抛）
│   │   ├── material_parser.py  # 作者材料文本提取（无第三方依赖）
│   │   ├── citation_format.py  # 参考文献格式（GB/T 7714-2015 / APA / MLA）+「哪种语言能选哪些」的唯一判据
│   │   ├── citation_gb_types.py # GB/T 7714 里期刊以外那几类（[M]/[D]/[C]/[N]）的著录模板，触发条件只读本类型专属的新列
│   │   ├── writing_lang.py     # 写作语言（zh / en）：未知值收敛、字数单位、输出上限、给提示词的输出语言宣告
│   │   ├── names.py            # 著者姓名规范化（GB/T 姓全大写 / APA 倒装缩写 / MLA 全名 + 等、et al.）
│   │   ├── metadata.py         # 文献元数据「缺失值」判定（单一事实来源）
│   │   ├── agents/             # 10 个职责模块（其中 schedule_agent 不含 LLM 调用）
│   │   └── routers/            # API 路由 + 闸门
│   │       ├── projects.py     # 项目主路由（全部业务入口）
│   │       └── config.py       # 配置路由（Key 查看 / 更新）
│   ├── tests/                  # pytest，1041 个用例
│   └── requirements.txt
├── frontend/                   # Vite + React 前端
│   ├── src/{App.jsx, api.js, exporters.js, ...}
│   ├── electron/               # Electron 外壳（打包用）
│   └── package.json
├── AI学术写作辅助系统_PRD.md     # 产品需求文档
├── ARCHITECTURE.md             # 本文（怎么实现）
├── DECISIONS.md                # 演化账本（为什么变成这样）
├── DEPLOY.md                   # 部署方案
├── 源码打包指南.md               # 桌面端打包
└── README.md                   # 扉页（是什么 / 给谁用 / 怎么跑起来）
```

---

## 六、刻意不做什么（设计取舍）

每一条都是「能省事但故意没做」，并附原因。行内出现的 `v1.2x` 标记是**这条取舍当初为什么成立**
的痕迹；完整的演化过程（此前怎么做、为什么改）见 [`DECISIONS.md`](./DECISIONS.md)：

| 不做 | 原因 |
|---|---|
| 不引 ORM（SQLAlchemy 等） | 3 张表 + 少量列变更（加列四处同改），原生 `sqlite3` 足够；引入 ORM 会让「加一列要改哪几处」这个纪律从代码里消失 |
| 不引 `python-docx` / `openpyxl` | `.docx`/`.xlsx` 本质是 zip，正文在固定路径的 XML 里，标准库足够。桌面版用 PyInstaller 打包，新增依赖意味着改 spec 的 `hiddenimports` 并重验整条打包链路 |
| 不用消息队列（Celery / Redis） | 单机单进程应用，`asyncio` 后台任务 + 进度落库已足够，且少一个要部署的部件。**单进程是硬约束而不是现状描述**：任务登记表在进程内，部署时勿加 `--workers`，理由与正确扩容路径见 [`DEPLOY.md`](./DEPLOY.md) §4.3 |
| 不做向量库 / 相似度检索 | **不做 embedding、不做 RAG 取素材**——没有索引、没有向量、没有「找出最相似的片段喂给模型」。文献与章节的对应关系由**一次显式的调度判断**给出：模型只能从**库内已有的文献**里挑、只能挑**大纲里已有的章节**，白名单校验（标题逐字命中叶子、`doc_id` 在库内、同篇只取第一条）之后仍由代码补齐缺口，因此**引用始终能溯源到确定的文档与页码**——页码由代码按文献页序给出，模型看不到页内容。判断失败（未配置模型 / 超时 / 输出不可解析）不报错，整步退回确定性算法（按上传顺序均匀铺开），响应里的 `method` 如实区分走了哪条路 |
| 不引状态管理库（Redux / Zustand） | 页面只有一条主流程，props 足够 |
| 不做公式与代码的专门渲染（LaTeX） | 本轮取舍：理论推导与工程设计两类只做「推导步骤完整」「给出具体参数与接口」这类同义表述层面的约束 |
| 不做全文查重、多用户协作 | MVP 范围外，需要外部服务或另一套架构 |
| 不在引用变动后**自动重写**正文 | 生成完之后改了文献或重做调度，正文里的角标就与文末列表对不上了。此时"顺手把正文刷新一遍"看着省事，实际是替用户决定"哪一版才是他要的"——而那篇正文本是自洽的完整一篇。做法改为**不静默改写**：告警条把事实摆出来、生成前拒绝并说清原因，出路交给用户，且**给的是与改动相称的那一个**（只有排版变了 → 重排列表，正文一个字都不用动；编号或成员变了 → 作废正文并重写，大纲与绑定保留）。两条动作用户各自点一下才发生 |
| 不给「有未绑定文献」在**生成入口**加第二条拒绝 | 生成前已经有一条 400（正文与当前编号不一致）。再按"文献库里有东西没进绑定"拦一道，会让一个**故意跳过引用**的项目每次点生成都被拦——正是那种训练用户闭眼点确认的拦。这条事实的补救动作是重新调度，提示就该摆在引用调度那一步：主按钮让给「执行引用调度」，按「确认引用绑定」时弹一次告知后**放行** |
| 不把「有 N 篇未绑定」做成**全局常驻条** | 它在"刚上传完文献、还没轮到调度"和"文献综述正在批量上传"时同样为真，那是完全正常的中间态；一条琥珀色警告挂在每个步骤上，只会变成用户学会无视的噪声。代价是"不刷新页面、从步骤条直接跳到生成步"这条窄路看不到它——与 `citations_stale` 一直以来的暴露面同级，接受 |
| 不修「绑定里有一篇文献库里已不存在」（悬空 `doc_id`） | 绑定是**请求体**给的，`/citations/confirm` 不校验 `doc_id` 是否在库内，于是手工构造的请求体能造出"有标题、作者/年份/来源全空"的编造参考文献。界面从不自己编绑定（那一步没有编辑器），所以只能由脚本触达；而新加的 `unbound_documents` **结构上**只算一个方向，看不见它。真要管应当管在**写入口**（丢弃或 400），而不是再加一个读取侧字段——那是另一件事（已记入 PRD §4.5.5 与本文件，供日后决定） |
| 不把引用调度的**相关性判断**做成后台任务 | 这是「长耗时操作一律走后台任务」那条纪律的**唯一例外**，写在这里免得下一个人以为漏了：它是一次小调用（单轮、只发标题与摘要、超时上限 120 秒），结果被**同一步立即消费**，且失败必然退回确定性兜底——用户永远拿到一份可确认的绑定、并且知道是哪一种（`method`）。后台任务化的收益（可离开页面、可轮询进度）在这里没有对象。按钮的 `loading` 已够。**但「例外」只免掉了进度落库，没免掉登记**：它仍然占用这个项目，所以协程照样派进 `tasks._TASKS`（键 `citations:<id>`）再由同一个请求 `await` 它自己——于是另外五个任务与所有查 `_busy_task` 的入口一行都不用改就都看得见它，而那个绑定值由 `_guard` 原样传回来（此前它被丢弃）。漏登记的现场：调度收尾用的是等待**之前**取的项目快照，于是它把这段期间别人写下的状态顶回去。第 15 条验证命令锁住了这条。**（v1.29 追加）这一行里"可轮询进度在这里没有对象""按钮的 `loading` 已够"两句已经作废**：执行引用调度那 120 秒的真实阶段现在写进**既有的**共享进度槽（四笔写入：闸门与"零文献"那道 400 之后一笔、进模型调用之前一笔、绑定落库之后一笔终态、唯一那个错误写者；见 PRD 决策 51），界面在那期间显示一条**不确定条**（`done` / `total` 全程为 `None`——一次模型调用给不出百分比），`kind="citations"` 也进了前端轮询的白名单。**"不做后台任务"这个决定本身没有翻**：它仍然由同一个请求 `await` 到结束、仍然不承诺"可离开页面"，变的只是**顺手把阶段落了库**；于是这一行末尾那条"登记键前端看不见、按钮亮着点了才 409"的盲区一并关闭（`serverTaskRunning` 在那段里为真 ⇒ 别处的按钮是灰的）。**代价如实记档**：同一个"在飞"从此有**两处**记录（登记表与进度槽），进程被杀在两次写入之间时可能不一致 ⇒ 退化成旧盲区。第 15 条那条验证命令照旧锁着**登记**这一半 |
| 不暗改启动时的**参考文献快照重算** | `db._migrate_reference_snapshots` 在启动时按项目*当前*的 `citation_format` / `cite_style` 就地重算落盘快照的排版。于是"只改了列表格式"这类不一致会随重启被**静默修好**——提示出不出现，取决于后端重启过没有。本轮**只记档、不改它**：新的显式重排与它结果一致，只是**立刻且可见**。改它属于另一件事（启动迁移要不要有可见记录），不该顺手夹带。**v1.25 改过这个函数，但改的不是这件事**：新增四列后它的 SELECT 要一起扩（漏了就会让用户的编辑在每次启动被压回旧值），并且原本 `doc.get("authors") or ref.get("authors")` 的写法里 `or` 让**空串退回快照旧值**——"把抽错的作者清空"于是永远落不了地，填一个错的反而能生效。同一次改动**没有**让它变得可见，这一条仍然成立（见 §八 第 19 条）。**v1.27 修掉了其中一个具体成因，但这一条仍然成立**：那次"重启后被静默修好"还有一条更隐蔽的分支——运行期写出的文末列表与启动重算用的**元数据字段清单不一致**（运行期少一项英译题名 `title_en`），于是同一份绑定的告警条亮不亮取决于后端**重启过没有**，而「重排文末列表」走的正是缺项的那一处、怎么按都修不好。清单现已逐项对齐（`_SNAPSHOT_META_FIELDS` 是唯一那份，见 §八 第 24 条），重算里那个绕开 `effective_format` 的默认值也一并删掉。**重算本身仍然在启动时静默发生**——要不要让它可见是另一件事，没有被这次改动夹带 |
| 不做 `[J/OL]` / `[EB/OL]`、不做著者-出版年制（**"不给非期刊类型做完整著录模板"这一件已在 v1.28 移出本行**） | GB/T 7714-2015 的顺序编码制里，v1.25 只有**期刊**这一条做到逐项合规（`刊名, 年, 卷(期): 起止页码.`）。`[M]` / `[D]` / `[C]` / `[N]` 的完整模板需要出版地、出版者、版本、学位授予单位——**这一件 v1.28 做了**，三条判断写进 PRD 决策 48 与本文件 §八 第 30 条：新增**三列**（出版地 `place` / 版本项 `edition` / 报纸出版日期 `publish_date`），而出版者、学位授予单位、论文集名、报纸名**一律复用已有的 `source`**——它本来就是出版者（抽取提示词早把它当"期刊 / 会议 / 来源"索取、编辑界面的标签也早写着"刊名 / 出版社 / 报纸名"），再加同义列会让模型把出版社抽进旧列、新列**恒空**，触发条件于是永不成立，功能上线等于没上线；**触发条件只读本类型专属的新列、绝不读 `source`**，因为存量 `[C]` / `[N]` 的 `source` 基本都非空，把它算进触发会让这类条目**集体走形**（逗号变句点）；新列全空则**逐字节**落回原来那条兜底路径，所以存量与"模型没抽到新字段"的条目输出与 v1.27 一个字符都不差。`[J/OL]` / `[EB/OL]` 仍然不做：它们还要 URL 与引用日期，而那两个字段与录入界面仍不做——宁可如实少印，也不印一个没有 URL 的 `[J/OL]`，那比 `[J]` 更不合规。同理**不做著者-出版年制**：当时记的理由是"它属于『写英文论文』那一轮的待办，而那一轮天然需要先把 APA/MLA 对齐"，**v1.26 之后这句话要分开看**——前半句**已被证伪**（英文能力在这一轮做了），后半句**只对了一半**：APA / MLA 确实对齐了，但对齐的是**文末列表的著录形态**，正文里仍是生成时烘死的 `[n]` 角标，而著者-出版年制改的恰恰是正文（`(Smith, 2020)`）。所以它**没有**随英文能力一起到期，留到"动正文角标"那一轮（PRD 决策 42 的两处有意偏离之一）。斜体这一条**v1.28 起收窄为"md / txt 不做"**：APA 的刊名与卷号、MLA 的容器名本该斜体，而 `.docx` 那条路径原先按整段渲染、斜体范围需要结构化判断——**这一件也做了**，做法与理由见 §八 第 29 条（斜体范围由服务端算好、随响应下发，`doc.references` 仍是 `string[]`）；md 与 txt 仍不做，它们是纯文本、斜体**没有落点**，`citation_format.py` 的模块 docstring 把这一点记作有意取舍 |
| 文献判重**不按文件名、也不按题名** | 判重键取**内容**（`db.document_fingerprint`：每页页码 + 该页前 300 字的 sha256）。按题名判会**真的丢文献**——实测同一项目里 2023 与 2024 两份**不同**文件都解析成「Annual Report」，一次误判就是一篇文献消失且用户无从察觉；按文件名判则挡不住"同一份文件换个名字重传"。判错的方向不对称：多跳过一篇会**点名报出来**（文件名 + "与《…》内容相同"），少一篇是静默的。判重也不是**并发防线**：查库与入库之间隔着一次 `await extract_metadata`，"先查后写"本身不原子，并发上传仍由 `upload_documents` 的两道忙闲互斥守 |
| 判重指纹不**打在原始 PDF 字节上** | 原始字节在上传结束时就丢了（`upload_documents` 读完只在内存里往下传，从不落盘），只有 `pages_json` 还在。打在字节上看起来更强，但**存量文献这一列永远是空的**——用户重传一篇改动之前传过的文献照样会重复，而那正是这轮要修的现场。改打在落库形态上，启动时的 `db._migrate_document_hashes` 就能把所有老行反算出同一个指纹，判重对老文献一样生效。代价是判据弱于字节哈希（要撞上得页数相同、且每页前 300 字逐字相同），已如实记档 |
| 导出**不引第三方库**（Word 也不例外） | 与上面那条「不引 `python-docx` / `openpyxl`」同一条理由，只是换到前端：`.docx` 就是一个 zip 里放几份 XML，`exporters.js` 里手写 CRC32 与两处 zip 头部（**不压缩**，store）、再按 schema 的次序拼 `document.xml`，Word 与 WPS 都能正常打开。另一条看着更省事的路是「HTML 改名叫 `.doc`」，但新版 Word 会弹「文件格式与扩展名不匹配」——给客户用的导出不该带这种提示。**不做 PDF 导出**：PRD §4.7 里那句「至少支持 Markdown 与 Word」已经兑现，PDF 需要真正的排版引擎（或引入一个大依赖），不在本轮 |
| 导出**不放后端** | 导出是纯前端动作：内容全在浏览器里已经有了，放后端只会多一次往返、多一份「正文与导出内容谁才是最新的」问题。代价是导出逻辑进不了 pytest —— 所以 `exporters.js` 刻意不含 React，可以直接用 Node 跑起来做真实校验（见 §八 第 13 条）。**v1.21 起后端多了一个 `POST /projects/{id}/export`，它不产文件、只写回执**（`completed → exported`）：那是状态机的事，与"把字节拼出来"不是一回事——终态此前被定义过、被读过，却没有一处写过它（见 §八 第 16 条）。**前端照着后端那道闸禁用按钮**（同一个 `serverBusy` 传遍七步），而每种任务自己的 running 标记只喂它那条进度条——按"自己这一类任务在不在跑"判会把十几个入口做成"灯亮着、点下去必被拒" |
| 不把「导出失败」与「回执失败」合成一句话 | 文件此刻已经在用户的下载目录里了，回执失败只说明"这次导出没记进项目状态"，写成「导出失败」会让人以为文件是坏的、转头再导一遍。回执路由对 `exported` **幂等**也是同一条考虑：同一份稿子导两次是常事，返回 400 等于把一次成功的导出报成失败。回执响应还**不带** `citations_stale` / `unbound_documents` 这类现算字段，所以前端只把它**合并**进本地副本，不能整份替换 |

---

## 七、规模

| 指标 | 数值 |
|---|---|
| 后端（`backend/app`） | 29 个 Python 文件，约 9.6k 行 |
| 后端测试 | 26 个文件，约 13.3k 行，**1041 个用例全绿** |
| 前端测试 | `frontend/tests/` 4 个 `.mjs`，**39 个 `node --test` 用例**（Node 24 内置，零依赖） |
| 前端（`frontend/src`） | 7 个文件，约 5.6k 行（其中 `App.jsx` 约 3,925 行、`exporters.js` 约 0.35k 行） |
| 论文类型 | 7 类，每类一条差异化工作流 |
| 处理环节（"Agent"） | 10 个模块，其中 9 个调用模型、共 10 个调用点 |
| 状态机 | 10 个状态 |
| 数据库 | 3 张表（SQLite 单文件） |
| 后端依赖 | 10 个库（8 个运行时 + 2 个测试） |

---

## 八、如何自行验证以上说法

下面每条都是可以直接跑的 grep / 命令，验证的是**代码**，不是本文档。
注释里出现的 `v1.2x` 是"这条断言当初为什么加"的痕迹，属**有意保留**——删掉它，后人就不知道这条断言在防什么。

```bash
# 1) 没有任何工具调用 —— 应为 0 命中
grep -rn "tools=\|tool_choice\|function_call" backend/app

# 2) 模型调用点 —— 会命中 11 行 / 10 个文件，其中 app/llm.py 那行是文档字符串里的用法
#    示例；真实调用点为 10 处，分布在 9 个模块，且 schedule_agent.py 不在其中
#    （有一条也藏在里面：relevance_agent.py 那次包在 asyncio.wait_for(…) 里，
#      所以它抓不到 "await llm."，这行用 llm.chat 才抓得全）
grep -rn "llm\.chat" backend/app

# 3) 后端依赖里没有 Agent 框架
cat backend/requirements.txt

# 4) 测试全绿（默认离线，不调用模型）
cd backend && venv/Scripts/python -m pytest -q    # Windows
cd backend && venv/bin/python -m pytest -q        # macOS / Linux

# 5) 论文类型数量 = 7
cd backend && python -c "from app.paper_types import PAPER_TYPES; print(len(PAPER_TYPES))"

# 6) 论文标题的推导只有一处（定义 1 行 + 挂在各写入接口上的调用点若干）
grep -rn "_display_title" backend/app

# 7) 引用一致性的两条出口都在后端 —— 生成前的拒绝与读取时现算的告警字段
grep -rn "citations_stale\|_refs_key" backend/app

# 8) 引用一致性的第二条（文献 vs 绑定）：推导只有一处（_unbound_documents），
#    「哪些 doc_id 在绑定里」也只有一处（_binding_doc_ids，_binding_uses 也走它）。
#    绑定是**请求体**给的，所以「读它」也必须只有一处（_binding_entries：跳过读不懂的
#    条目）——三个读取点只加固一个比不加固更坏，它看起来已经修过了。
#    前端那一半同理（bindingRefs：对 null 调 .map 会让整页白屏）
grep -rn "unbound_documents\|_binding_doc_ids\|_binding_entries\|bindingRefs" backend/app frontend/src

# 9) 三个事实必须同时下发，所以裹在 _citation_facts 里整包发出；
#    挂载点数应为 6：get_project + 五个引用写接口（调度 / 确认 / 跳过 / 重排列表 /
#    作废章节），list_projects 刻意不挂。这个数会随新写接口增长 —— 数不对就说明
#    有一个新接口只返回了自己那件事，前端读到的形状与详情接口对不上
grep -rn "_citation_facts" backend/app

# 10) 引用一致性的第三个事实（到底哪一样变了）：判据只有一处（_refs_diff），
#     告警字段 _citations_stale 只是它的一行委托（不留第二份实现）；
#     重排写下去的值与护栏比对的是同一个函数（_refs_target），所以完成即一致
grep -rn "_refs_diff\|_refs_target\|def _citations_stale" backend/app

# 11) 两个相称的补救动作各一条路由；重排的准入门槛（只放行 render）就是它的安全性
grep -rn "references/rerender\|sections/reset\|_refs_stale_message" backend/app

# 12) 文献判重：指纹的推导只有一处（document_fingerprint），三个调用点各司其职 ——
#     写入时算（add_document）、上传前查（existing_document_title）、启动时补存量
#     （_migrate_document_hashes）。凡出现第二个拼指纹的地方，判重就已经失效了
grep -rn "document_fingerprint\|existing_document_title\|content_hash" backend/app

# 13) 导出：格式表只有一处（exporters.js 的 EXPORT_FORMATS —— 菜单项、扩展名、MIME
#     都从它来），三种格式共用同一份「导出文档」形状，「拼标题与参考文献」这件事只在
#     App.jsx 的 ExportStep 里发生一次。凡出现第二个拼导出内容的地方，三种格式迟早各说各话。
#     .docx 是手写的 zip + OOXML，所以校验要真按 zip 规范读一遍（CRC + XML 解析），
#     再让 Word 自己打开一次 —— 光看代码看不出 zip 头部有没有写错
grep -rn "EXPORT_FORMATS\|buildMarkdown\|buildTxt\|buildDocx\|exportDocument" frontend/src

# 14) 作者的写作思路：注入点**只有两处**（大纲第一遍的 _ideas_block + 正文的 _ideas_part /
#     _core_question_line），两处各自持有一份措辞骨架（跨模块不共享提示词块是本项目既有
#     惯例），因此两边各有一条测试锁住同一句锚点话术，防漂移。凡出现第三处注入点，
#     或者两处里有一处悄悄改了那句话说法的，都该在这里被看见
grep -rn "writing_ideas\|_ideas_block\|_ideas_part\|_core_question_line" backend/app backend/tests/test_frontend_mirror.py

# 15) 两套忙闲登记 + 引用调度的登记：_busy_task 是**唯一**的通用在跑判据，它下面
#     每一个 key 都必须真的有人登记（前五个走 tasks.spawn，第六个 citations:<id>
#     由调度路由自己派进去再 await）。判「有没有漏登记」的办法不是读代码，而是把
#     判据当断言用：test_citation_schedule_blocks_the_other_entries 直接往
#     tasks._TASKS 里塞一个假任务，再逐条打其余入口，全部必须 409 —— 哪天某个入口
#     改成自己查别的表，这条会红。另一半是"闸门不能变成锁"：每个 409 用例后面都跟
#     一条"任务清空后必须能正常通过"
grep -rn "_busy_task\|_GEN_TASKS\|_citations_key\|tasks.spawn" backend/app
grep -rn "def test_.*rejected_while\|def test_citation_schedule_blocks" backend/tests/test_api.py

# 16) 终态 exported 的唯一写入点（mark_exported）：三条分支（completed → 写 / exported
#     → 幂等原样返回成功 / 其余 400）+ 两道忙闲闸。前端那一半的两条不变量同样只有一处：
#     **按钮的禁用判据只有 serverBusy 一处**（per-kind 的 running 标记只喂它那条进度条），
#     **失败原因一律经 errorDetail 取后端的 detail**（三处入口共用，不留第二句写死的文案）。
#     出现第二处判据或第二句写死文案，就是这一版修掉的缺陷又回来了
grep -rn "def mark_exported\|ProjectStatus.EXPORTED" backend/app
grep -rn "serverBusy\|errorDetail\|markExported" frontend/src

# 17) 大纲卡片的「谁蓝」只有一处判据（clustersNeedConfirm = 本步是综述 && 有文献 &&
#     已有聚类 && 聚类未确认），三个按钮（确认聚类 / 生成大纲 / 确认大纲）各按它取样式。
#     命中数本身当断言用：**恰好 4 处**（1 处定义 + 3 处使用）。判据被抄成第二份，就等于
#     有人在自己那一格里重新推一遍"现在该点哪一个"——而这张卡片上并排着前后两个阶段的
#     出口（聚类确认只是盖章；大纲确认会作废绑定与材料计划并推进一步），各判各的就会
#     同时亮起两个蓝按钮，用户无法从界面判断先后（这是 v1.23 修掉的缺陷 A8 的形状）。
#     另一半是"文案不猜原因"：横幅里的每一句都必须在**每一种**会渲染它的情形下成立，
#     所以那两句只对"调度之后又加了文献"成立的断言被删掉，且测在**去注释**的源码上
#     （本项目的注释刻意把"这里曾经说过一句假话"的原文逐字留着当记录）
grep -rn "clustersNeedConfirm" frontend/src/App.jsx
grep -rn "def test_outline_card_has_no_hardcoded_primary_button\|def test_citation_banner_does_not_assert_a_cause" backend/tests/test_frontend_conventions.py

# 18) 目标总字数的下限：数字的真身只有一处（后端 projects.MIN_TARGET_WORDS），经 /api/meta
#     的 min_target_words 下发；前端那份 FALLBACK_MIN_TARGET_WORDS 只在元信息到达前用，
#     并且**必须与后端同值**（test_fallback_min_target_words_matches_the_backend 直接拿
#     两者比）。它是这套系统里**唯一**被镜像到前端的上下限 —— 标题与写作思路的上限刻意
#     不镜像，因为镜像成 maxLength 会**静默吃掉**粘贴进来的尾巴（用户永不知情），而下限
#     镜像出来只驱动一句提示，没有这种风险。另一半是"这串字符是多少字"的归一也只有一处
#     （wordsNumber：Number 而不是 parseInt，因为 type="number" 交回来的本来就可以是
#     "1e3" 这种浮点字面量，parseInt 会把它读成 1 —— 那是关于用户输入的一句假话）；两个
#     发送点（推荐选题 / 确认选题）都发归一后的数字，判据 wordsTooFew 也只有一处。
#     **界面给的是提示不是禁用**：确认按钮的 disabled 里不出现 wordsTooFew（真正的拒绝是
#     后端那道 400），所以那一格不许有人自己加一句写死的 "1000" 文案
grep -rn "MIN_TARGET_WORDS\|min_target_words\|wordsNumber\|wordsTooFew" backend/app frontend/src
grep -rn "def test_target_words_input_keeps_a_text_draft\|def test_topic_confirm_button_explains_instead_of_disabling\|def test_target_words_is_always_sent_as_a_number\|def test_fallback_min_target_words_matches_the_backend" backend/tests

# 19) 文献元数据编辑（PATCH /projects/{id}/documents/{docId}）：它的**新不变量**是
#     「绑定可以被非调度路径写入」——这是本项目第一次让非调度的代码动引用绑定。落库后
#     若题名确实变了，必须刷新绑定里同 doc_id 条目的 doc_title（顺序与编号一个字不动）。
#     不刷新就等于改题名毫无反应：_plan_citations 是**绑定优先于 documents.title**，而
#     "两份题名不同"这件事既不告警也不提示（_refs_diff 判出来的 kind 仍是 none），用户
#     会以为保存失败。判据是 _refs_diff 只在 [(num, doc_id)] 两侧逐位相同时才放行 render，
#     所以改题名**复用**现成的「重排文末列表」（零 LLM、正文一个字不动），不需要新机制。
#     另一半在迁移上：四列之后 _migrate_reference_snapshots 的 SELECT 必须一起扩（漏了
#     用户的编辑每次启动被压回），且合并写法不许用 `or`（空串会退回快照旧值 → "清空一个
#     抽错的字段"永远落不了地）。这两条各有一条专项回归。
#     第三半：编辑框里的失败原因不许被吃掉 —— 这条路径**刻意不走 run()**，run() 把错误
#     写进顶部横幅，而横幅在模态遮罩（z-index: 1000）后面，那正是 v1.21 修掉的 A7 的形状。
#     所以 saveDocument 返回 {ok, message}、由模态自己渲染（test_frontend_conventions 钉住）
grep -rn "def update_document\|_validated_doc_patch\|_refresh_binding_title" backend/app
grep -rn "def test_title_edit_is_not_a_no_op\|def test_patch_title_refreshes_the_binding_copy\|def test_migrate_reference_snapshots_does_not_push_a_cleared_field_back\|def test_page_range_is_not_overwritten_by_the_pdf_pages_column\|def test_document_edit_shows_the_failure_inside_the_modal" backend/tests

# 20) GB/T 7714-2015 的著录只有一处实现：期刊形态 _journal_segment、页码归一 normalize_pages、
#     姓名规范化 names.normalize_authors；类型字母的白名单也只有一处（citation_format.SOURCE_TYPES），
#     前端的 SOURCE_TYPE_LETTERS 是它的镜像（test_frontend_mirrors_source_type_whitelist
#     直接拿两者比），而编辑界面的下拉选项**由它派生**、不许手写 —— 白名单抄错一个字母，
#     用户就能选到一个后端会 400 拒掉的类型；少一个字母则那个合法类型永远选不到，两种在
#     界面上都完全正常。**降级必须逐字节复现旧输出**：test_db.py 里那三条"无卷无期无页、
#     作者为空或中文"的用例**一个字都没改**，它们继续绿本身就是这条的回归线；凡出现第二个
#     拼卷期页的地方，这条会红。另一处单向依赖：抽取侧的提示词要索取卷/期/页/类型，但它
#     **不做白名单**（越界的字母在这里放行、由渲染侧回落 J），所以抽查取提示词的那条测试
#     叫 test_source_type_is_not_whitelisted_at_extraction，别把它当遗漏
grep -rn "_journal_segment\|normalize_pages\|normalize_authors\|SOURCE_TYPES" backend/app
grep -rn "SOURCE_TYPE_LETTERS" frontend/src/App.jsx
grep -rn "def test_frontend_mirrors_source_type_whitelist\|def test_gb7714_journal_segment_degrades_by_missing_fields\|def test_document_edit_type_options_are_derived_not_hand_written\|def test_reference_list_preview_turns_off_the_ol_numbering" backend/tests

# 21) 写作语言：每条判据只有一处。writing_lang.normalize 回答"这个项目是什么语言"（未知值
#     一律收敛到 zh —— en-US 这类地区标签两边都落中文，前端 buildDocx 走同一条判据，不自己
#     发明第二套匹配规则）；citation_format._FORMATS_BY_LANG 回答"这种语言能选哪些格式"；
#     effective_format 是格式解析的**唯一入口**（确认 / 生成 / 重排 / 启动迁移四处共用 ——
#     漏掉迁移那一处，启动重算与运行期就会各写一种格式，citations_stale 会常年亮着）；
#     count_units 是字数口径的唯一分发点（en 走空白分词、zh 仍走 count_chars，所以英文正文
#     不再"永远字数不足 → 扩写 → 撞满重试上限 → 落成占位文本"）。另一半是"中文侧逐字节
#     不变"：output_rule("zh") 返回空串、cap_chars 在 zh 下回原值 —— 凡在中文路径上多加一个
#     字，test_db 那三条逐字节迁移用例与 test_outline_agent 按中文串分发的 _stub_llm 会同时红，
#     而这两处**一个字都没改**，它们继续绿本身就是本轮的回归保障
grep -rn "def normalize\|def is_en\|def words_unit\|def cap_chars\|def output_rule" backend/app/writing_lang.py
grep -rn "_FORMATS_BY_LANG\|_DEFAULT_FORMAT_BY_LANG" backend/app
grep -rn "def count_units\|def effective_format" backend/app
grep -rn "def test_writing_lang_defaults_to_chinese\|def test_english_section_words_are_counted_as_words\|def test_english_project_renders_apa_references" backend/tests

# 22) 英文骨架与中文骨架**逐位同形**：PAPER_TEMPLATES_EN 与 PAPER_TEMPLATES 的章数、每章的
#     节数必须一一对应（34 章 74 节），漏译一节当场变红。三条断言各管一件事：形状不变式逐类
#     成立、英文骨架与两条类型说明（structure_note_en / writing_note_en）里不含 CJK、
#     theme_section_count(t, "en") 与 zh 同值。**最后一条是抓一个静默陷阱**：theme_section_count
#     用中文串做**子串匹配**，英文骨架下它会静默返回 0 → 文献综述的主题聚类合并检查整段被
#     跳过，而界面上一切正常（没有报错、没有告警，只是少了一道校验）。凡出现第二处按中文串
#     去匹配骨架的地方，这条会红
grep -rn "PAPER_TEMPLATES_EN\|theme_chapter_en\|structure_note_en\|writing_note_en" backend/app
grep -rn "def test_english_skeleton_has_the_same_shape\|def test_english_skeleton_and_notes_contain_no_cjk\|def test_theme_section_count_is_language_invariant" backend/tests

# 23) 界面上的"可选格式 / 角标样式"与后端同一来源（v1.26 收口）：下拉选项由 /api/meta 下发的
#     常量 .map() 派生，**不许手写 <option value="gb7714">** —— 抄错一项在界面上完全正常
#     （用户能选到一个后端必 400 的组合，或者永远选不到一个合法项）。后端那道 400 的文案与
#     界面用的是**同一张显示名表**，且两样都印（GB/T 7714（gb7714）：只印标识则用户对不上
#     自己刚选过的那一项，只印显示名则直接调接口的人不知道请求里该填什么）。
#     另一半是"兜底不许号称与后端同步"：formatRef 那条自己拼参考文献的兜底**降级成一句提示**
#     （"本条格式未生成，请到引用调度步重排文末列表"）—— 它原先是一份与后端不同步的第二实现，
#     而 APA/MLA 要的是姓名倒装 + 缩点 + & + 20 作者规则，等于把 names.py 那 60 行启发式在 JS
#     里再实现一遍：做不到同步却号称同步，比没有更坏（formatted 一缺，英文预览就会印出 GB/T
#     形态的姓名）。最后一条 grep **应为 0 命中**：只服务于那个兜底的 FALLBACK_SOURCE_TYPE 与
#     normalizePages 已随它删掉（test_frontend_mirror 的注释里留着它们的名字当记录）
grep -rn "props.citeStyles.map(\|props.citeStyleLabels\[" frontend/src/App.jsx
grep -rn "def test_cite_style_options_are_derived_not_hand_written\|def test_fallback_writing_lang_mirrors_match_the_backend\|def test_fallback_cite_styles_match_the_backend" backend/tests
grep -rn "FALLBACK_SOURCE_TYPE\|normalizePages" frontend/src backend/app

# 24) 「这一轮会写出的文末列表」与「启动时重算出来的」必须是同一个字符串（v1.27）：落盘快照有两个
#     产出点 —— 运行期构造 ref 的 _plan_citations 与启动时的 _migrate_reference_snapshots —— 两边的
#     元数据字段清单必须**逐项相等**，以后者那份 db._SNAPSHOT_META_FIELDS 为准。运行期少一项
#     title_en 时，同一份绑定在运行期印不出 APA 7 §9.38 的方括号、在启动重算时却印得出：
#     citations_stale 亮不亮取决于后端**重启过没有**，而用户按多少次「重排文末列表」都修不好
#     （重排走的正是缺项的那一处）。判据是**用旧形态的数据走新代码**：把**运行期产出的**快照写回
#     references_json，再跑一次 _migrate_reference_snapshots，改动数必须为 **0**（写进缺该项的旧形态
#     快照则为 1）。第二半是同源的：语言与格式的解析只在 effective_format 一处做，所以重算里那句
#     `proj["citation_format"] or "gb7714"` 是**第二个、方向还错的**默认值，下面第二条 grep 应 0 命中。
#     凡出现第二个拼文末列表字段的地方，这条会红
grep -rn "_SNAPSHOT_META_FIELDS\|def test_runtime_reference_snapshot_is_already_final_for_the_migration" backend/app backend/tests
grep -rn "or \"gb7714\"" backend/app

# 25) 前端不许出现第二份判据（v1.27 收口）：四条断言都跑在**剥掉注释**的源码上，且每一条都验过
#     "改回修复前的写法就变红"。① **使用处必须与本文件的 import 行同名**：referencesHeading 在
#     App.jsx 里被用到，**且它的 import 行里有它** —— 只断言"导出模块里有这个名字"是拦不住白屏的
#     （少一行 import 不是退回中文，是渲染期 ReferenceError + 整个 root 卸载；frontend/src 全树
#     没有 error boundary）。② **读数字只有一处入口**：剥注释后 App.jsx 里**不出现 parseInt(**，
#     两个每节字数输入框与「目标总字数」都走 wordsNumber，且"删空 = 未分配"（0 是合法值）必须保留。
#     ③ **格式合法性只有一处**：formatsFor(...).includes 全文件命中**恰好 1 次**（就在
#     effectiveFormatFor 里），loadProject 与改语言两处都读它、不许各自内联一遍。
#     ④ **语言说明来自后端而非前端手写**：App.jsx 里**不出现 writingLang === 'en'**，"被本语言
#     排除掉的格式"的说明由 citation_format_notes_by_lang 给 —— 它的键集固定为**全部格式**（英文下
#     那条 GB/T 7714 说明的用途正是解释它为什么不在列表里），所以 /api/meta 下发的键集要与
#     SUPPORTED_FORMATS 相等，前端的 FALLBACK_SUPPORTED_FORMATS 与后端逐项对账
grep -rn "referencesHeading\|parseInt(\|formatsFor(\|writingLang === 'en'\|FALLBACK_SUPPORTED_FORMATS" frontend/src/App.jsx
grep -rn "def test_export_layout_language_comes_from_the_project\|def test_numbers_are_read_through_wordsNumber_only\|def test_effective_citation_format_has_a_single_derivation_point\|def test_topic_step_language_note_comes_from_the_backend_table\|def test_fallback_supported_formats_match_the_backend" backend/tests

# 26) 新加的断言"有牙"要**当场跑出来**，不靠推断（v1.27 的做法，可复用）：说"这条断言在修复前必然
#     红"是一句断言、不是证据。做法是把**当前**的源码文本各自改回修复前的形状（少一行 import、
#     wordsNumber 换回 parseInt、把一个判据还原成内联的那一份），再用**同一个测试函数**去判那份
#     变异文本（importlib 加载测试模块，把变异后的字符串直接喂进去）—— 四条全部转红，并各自给出
#     预期的那句 message。变异**必须有 assert 兜住**（assert old in text），否则源码一重构、变异
#     静默失败，探针就退化成"拿原样判原样"，全绿而毫无意义。后端侧的同类做法是**用旧形态的数据走
#     新代码**（见第 24 条）与**把新增占位符从模板里抠掉再渲染**、与现在的中文提示词逐字节比对 ——
#     后者是"中文路径零变化"这条不变量的实证，比"我看着没改"强
grep -rn "def test_allocation_prompt_carries_the_language_declaration\|def test_recommend_topics_follows_the_language_the_form_sends\|def test_set_topic_without_writing_lang_means_do_not_change\|def test_apa_with_author_keeps_one_ending_mark\|def test_mla_ending_mark_stays_inside_the_quotes" backend/tests

# 27) 前端的两层 error boundary（v1.28）：`frontend/src/ErrorBoundary.jsx` 是全树**唯一**做异常兜底的地方。
#     两层挂载各管一件事：外层（main.jsx 包住 <App />）接渲染期与生命周期里的异常 —— 没有它，一次
#     ReferenceError 会把整个 root 卸载、界面变成一片空白，而**打包态用户看不到控制台**（v1.27 的 F1
#     「少一行 import」造成的正是这个现场）；内层（App.jsx 的 <main className="content"> 外面）保住顶栏、
#     项目列表与步骤条，用户能切走、能重试，而不是整个应用消失。**重置判据只有一处**：内层的 key 取
#     projectId，切项目即复位 —— 否则一次异常之后切换项目仍停在错误卡片上，看起来像"这软件坏了"。
#     卡片文案照本项目口径：不猜原因要并列穷举、补救无条件给（「重试」+「复制错误详情」，用户拿不到
#     控制台，那是他把信息交出来的唯一通道）、出路写明地点与按钮名（"左侧项目列表里换一个项目"）。
#     第三条 grep 数的是**实现处**，应为 1 个文件：出现第二处 componentDidCatch 就是有人另造了一个兜底
grep -rn "ErrorBoundary" frontend/src/main.jsx frontend/src/App.jsx
grep -rn "componentDidCatch\|getDerivedStateFromError" frontend/src
grep -rn "def test_app_is_wrapped_in_an_error_boundary_at_the_root\|def test_content_region_has_its_own_error_boundary\|def test_error_boundary_hooks_exist_in_exactly_one_file" backend/tests

# 28) 关闭桌面版不再往 %TEMP% 留约 28 MB（v1.28）：PyInstaller onefile 启动时解出 %TEMP%\_MEIxxxxxx，
#     由**父进程**（Electron spawn 的那层 bootloader）在子进程退出后负责删除；而原先的关闭路径走
#     `taskkill /PID <pid> /T /F`，`/T` 把整棵树**一次**打死，父进程没机会清理，于是每次启动-关闭留
#     约 28 MB。新路径三层：run_desktop.py 启动时把 os.getpid() 写进 ~/.academic_writer/backend.pid
#     （这个进程正是真正解释 Python 的那个子进程）→ 关闭时读它 → 用 tasklist **核对镜像名确实是**
#     academic_backend.exe（父子同名，所以核对是必须的，不能只信 pid）→ **只杀这一个 pid**，父进程
#     随即自行清理并退出。判据里**不许只剩一条路**：pid 文件不存在 / 读不出 / 不是正整数 / 核对不通过 /
#     tasklist 自己起不来 / 父进程数秒内没退出 —— 每一种都**回落今天那条 /T /F**。宁可留残渣，也不能
#     关不掉。下面那条 grep 应当列出**六条** Windows 用例：一条"正常 → 单 pid 杀"，五条"异常 → 回落"
#     （**不回头清理已经攒下的 _MEI\***：通配符删除误删风险不值当，这一条写在 PRD 的决策里）
grep -rn "backend.pid" backend/run_desktop.py frontend/electron/stop-backend.cjs
grep -rn "tasklist\|/T /F" frontend/electron/stop-backend.cjs
grep -rn "Windows：" frontend/tests/stop-backend.test.mjs

# 29) 斜体**只有 .docx 做**，且范围由服务端算好下发（v1.28）：判断一条著录里哪几段该斜是**格式标准**的
#     事（APA 7 §9.34 是刊名与卷号一起斜、中间那个逗号也斜；MLA 9 只斜容器名，两个 `, ` 分隔符不斜 ——
#     两条规则刻意不统一），所以它只能有一个实现点：citation_format.py 的 `_parts` / `_flatten`。关键在于
#     `formatted` **定义为**那些片段文本的拼接（`"".join(...)`），**不是**另开一条渲染线 —— 于是
#     `_refs_key`、`_refs_diff`、快照比对与 test_citation_format 里那 6 条逐字节哨兵**一个字都不用改**
#     （它们比的都是 `formatted`）。`_flatten` 是**唯一的排序点**：顶层片段之间插一个普通空格 Run，
#     使拼接结果与旧的 `" ".join(parts)` 逐字节相同。片段**只在组装响应时现算、不落库**
#     （references_json 里仍然只有 formatted 与那十来个元字段），由 `_with_reference_runs` 挂在**恰好
#     两个**端点上：GET /projects/{id} 与 GET /projects/{id}/generate/progress —— 挂载点少一个，那条
#     路径上的斜体就静默消失。前端拿的是**并行字段** referenceRuns，`doc.references` 仍是 `string[]`
#     （三个 zh sha256 哨兵长在这个形状上）；docx builder 在**顶部**归一化一次（整块缺席 / 比 references
#     短 / 某一条是 null 或空数组，一律**逐条**退回整条正体），下面只有一条路径。**OOXML 的 rPr 子元素
#     顺序是 schema 约束**：`<w:i/>` 必须插在 `<w:b/>` 之后、`<w:sz` 之前，不能追加到末尾。md 与 txt
#     **完全不读**这个字段（纯文本里斜体没有意义），那条断言是"这个字段对它们不存在"、不是"它们碰巧没用到"
grep -rn "def reference_runs\|def _parts\|def _flatten" backend/app/citation_format.py
grep -rn "_with_reference_runs" backend/app/routers/projects.py
grep -rn "def test_apa_italic_span_is_the_journal_name_and_volume\|def test_mla_italicizes_the_container_only\|def test_gb7714_never_italicizes\|def test_corpus_output_is_byte_identical\|def test_reference_runs_are_served_on_both_bodies_and_pair_with_the_string" backend/tests

# 29b) 斜体的另一半在前端：**每一处 setReferences 都必须配一次 setReferenceRuns**（三处注入 + 作废正文
#      那一处清空，共**四处**）。漏配的后果不是报错而是**印错** —— 留着的片段还是上一版的，docx 于是在
#      新的正文与新的预览旁边，把上一份文末列表的斜体范围印上去，只有打开 Word 才看得见。断言是
#      **计数相等 + 相邻三行内**：纯计数可以靠在别处多写一行空写骗过去。另有一条 node 断言把"只含一个
#      正体片段的 referenceRuns"与"整段一个 run"比成逐字节相同 —— 它成立，前端那条单路径才敢走
grep -rn "setReferenceRuns" frontend/src/App.jsx
grep -rn "def test_every_references_write_has_a_twin_referenceRuns_write" backend/tests
grep -rn "只含正体片段的 referenceRuns 与整段一个 run 逐字节相同" frontend/tests/exporters.test.mjs

# 30) 期刊以外那几类的著录模板（v1.28）：`[M]` / `[D]` / `[C]` / `[N]` 各一个纯函数，集中在
#     backend/app/citation_gb_types.py，`_gb7714` 收敛成"分流 → 有模板且触发成立就调，否则原样兜底"。
#     三条硬约定写在那个模块的 docstring 里：**触发条件只读本类型专属的新列、绝不读 `source`**
#     （存量 `[C]` / `[N]` 的 `source` 基本都是非空的，把它算进触发会让这类条目从 `论文集名, 2023.`
#     变成 `论文集名. 2023.` 集体走形）、**新列全空 → 逐字节落回 `_journal_segment`**（于是存量与
#     "模型没抽到新字段"的条目与 v1.27 一个字符都不差，这也是"上线不惊动已有项目"的全部依据）、
#     **`_journal_segment` 只作 fallback、不改成模板的零件**（新增的复杂度与被哨兵盯死的冻结路径
#     物理隔开）。出版者 / 学位授予单位 / 论文集名 / 报纸名**一律复用 `source`**（它本来就是这几样
#     东西，另开同义列的后果见 §六 那一行）。`publish_date` 的归一化放在**渲染侧**（照 normalize_pages
#     的成例）：`metadata.clean_meta_value` 明确"不做任何值改写"，而渲染侧能同时覆盖"用户手填"那条
#     不过清洗函数的路径。最脆的一环是**四份手写字段清单跨两个文件表达同一件事**（以
#     db._SNAPSHOT_META_FIELDS 为唯一那份），错配的症状是 `citations_stale` 常年亮、按重排也修不好
#     （见第 24 条）。**已知偏离如实记档**：`[N]` 不印版次、`[C]` 不印论文集主要责任者
grep -rn "citation_gb_types\|def render" backend/app/citation_gb_types.py backend/app/citation_format.py
grep -rn "place\|edition\|publish_date" backend/app/db.py
grep -rn "def test_gb7714_monograph_template\|def test_gb7714_dissertation_template\|def test_gb7714_conference_paper_template\|def test_gb7714_newspaper_template\|def test_gb7714_non_journal_legacy_output_is_byte_identical\|def test_runtime_snapshot_with_non_journal_columns_survives_the_migration\|def test_doc_edit_fields_match_the_backend_limits" backend/tests
# 31) 执行引用调度期间的进度条（v1.29）：**共享进度槽只有一个**，这条路不许另开一个。
#     四笔写入都必须经 _write_task(project_id, "citations", …)：① 在闸门与"零文献"那道 400
#     **之后**、tasks.spawn **之前**（被拒的这一次不留痕 —— 本仓既有约定，有专项用例钉住
#     "task_state_json 与调用前逐字相等"）；② 在 _build_binding **内部**、进那次模型调用之前
#     （**不改它的签名**：既有用例把它猴补成两个形参）；③ 成功路径在 db.update_project **之后**
#     写终态（终态先落库的话，轮询会先看到 done、去取一份还没写上绑定的项目）；④ **唯一一个
#     错误写者**：try/except（不是 finally —— 那会把成功那条 done 盖成 error）、必须覆盖
#     updated is None 那条 404（否则它是唯一一条会把槽留在 running 的出口）、必须 raise
#     （既有的 409 用例靠它）。**不给 done / total**：一次模型调用给不出百分比，给一个就是编造，
#     有断言专门钉住它（"不确定条"必须被锁住，否则将来补个假百分比没人拦）。
#     **两端各有各的真相、所以不需要 nonce**：发起侧用**本地**在飞判据渲染（同步请求没有可轮询
#     的服务端状态），另一侧（另一个标签页 / 切走再切回 / 刷新）由共享槽种子 + 轮询驱动；本地
#     那一半记的是**哪个项目**（否则切到别的项目会看到一条属于别人的进度条）。配套三件事缺一
#     不可：'citations' 进前端轮询的 kind 白名单（`serverTaskRunning` 于是在那 120 秒里为真 ⇒
#     §六 那条"登记键前端看不见"的盲区关闭）、轮询看到终态时**回填绑定**（少了它，"切走再切回"
#     会拿过期绑定去盖章，下一个「确认引用绑定」就把刚跑完的结果顶回去）、**只给这一个调用**加
#     150 秒客户端超时（没有它，一条死掉的 TCP 路径会让进度条永远转，那比没有进度条更假）。
#     另一半是**同步动作**也照这条办：本仓先例是「AI 推荐选题」（一次同步请求 → 本地记下起始
#     时刻触发同一条进度条）。**代价如实记档**：同一个"在飞"从此有**两处**记录（登记表与进度
#     槽），进程被杀在两次写入之间时可能不一致 ⇒ 退化成旧盲区（条在转、按钮亮的、点下去 409）
# 32) 生成步"正文写完以后蓝按钮位站谁"（v1.29）：判据**只有一处**（父层算 viewPrimary =
#     fix === 'none' && complete，各按钮按它取样式），而"已完整落库"的定义里有两处刻意排除：
#     resumable（error / interrupted 是既有的两种「可续写」终态，那时点一次「继续生成」正好把
#     状态修回来）与 generating（否则蓝按钮会在写到一半时跳成「查看导出结果」）。凡出现第二个
#     判据、或有人把它改成"节数够了"，这条会红 —— 失效方式是**静默**的：一个站错位的蓝按钮不
#     报错，只让用户按了等于没按（原来的「开始分段生成」在正文写完时点下去，后端一路走完循环、
#     发现一节都不用写，只把 status 原样写回 completed；项目已导出过时还会把 exported 顶回
#     completed）。「重写正文」在任何情况下都还在，蓝位被那两个补救占着时它与「查看导出结果 →」
#     一起退到次要位
grep -rn "POLLED_TASK_KINDS\|scheduleInFlight" frontend/src/App.jsx
grep -rn "viewPrimary\|开始分段生成\|查看导出结果" frontend/src/App.jsx
grep -rn "def test_citation_schedule_reports_progress_while_it_waits\|def test_citation_schedule_marks_progress_done\|def test_citation_schedule_failure_does_not_leave_a_running_bar\|def test_citation_schedule_rejected_leaves_no_trace\|def test_citations_is_a_polled_task_kind\|def test_citation_step_renders_its_progress_bar\|def test_finish_task_reconciles_the_binding_for_citations\|def test_schedule_citations_has_a_client_side_timeout\|def test_generate_step_view_button_takes_over_the_primary_slot" backend/tests
```

---

## 附：学术伦理立场

本系统定位为**辅助写作**而非代写。所有引用可溯源，杜绝 AI 虚构文献（聚类结果里的文献 id 按库内实际文献做白名单校验，模型编出来的一律丢弃）。作者自有材料只融入正文、**不充当引用来源**（不加角标、不进参考文献列表），避免把自己的数据伪装成文献证据。分析类章节只能依据作者已给出的设计字段展开，用户未填设计时系统须明确告知后果后再生成，不得为了「跑通流程」而在用户不知情的情况下让模型把分析章节编满。
