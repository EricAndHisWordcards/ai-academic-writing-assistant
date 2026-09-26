import React, { useEffect, useRef, useState } from 'react'
import { api } from './api'
// referencesHeading 必须在这里导入：预览区那句「参考文献 / References」与导出走
// **同一个**函数（见下面的断言测试），少这一行的话它不是退回中文，而是渲染期
// ReferenceError。
//
// 那句 ReferenceError 的后果**自本轮起变了**，这里如实记下两个阶段：本轮之前
// frontend/src 全树没有兜底，React 会卸载整棵 root，用户看到的是一片空白；本轮
// 起正文区与整个 App 各有一层 ErrorBoundary（见下面的挂载点），同一个缺陷现在
// 表现为「正文区换成一张错误卡片、顶栏与项目列表还在」。
// **两段都不是无害的** —— 所以这条 import 仍然要钉住，只是从「整页白屏」降级成
// 「一部分显示不出来」。
import { EXPORT_FORMATS, exportDocument, referencesHeading } from './exporters'
// 正文区那一层兜底（见下面 <main className="content"> 处的注释）。它在主 bundle 里、
// 不额外分包，所以这一行写成静态 import：渲染期它必须已经就绪，动态 import 会让
// 「兜底」本身也成为一个可能失败的异步步骤。
import ErrorBoundary from './ErrorBoundary.jsx'
import './App.css'

import {
  EditableTitle,
} from './components/EditableTitle'
import {
  RefsStaleBanner,
} from './components/RefsStaleBanner'
import {
  SettingsModal,
} from './components/SettingsModal'
import {
  WorkingBar,
  formatElapsed,
  formatEta,
} from './components/WorkingBar'
import {
  CitationStep,
  bindingRefs,
} from './steps/CitationStep'
import {
  DesignStep,
} from './steps/DesignStep'
import {
  ExportStep,
} from './steps/ExportStep'
import {
  GenerateStep,
} from './steps/GenerateStep'
import {
  ClusterPanel,
  OutlineStep,
  Reason,
  clearReason,
  cloneOutline,
  outlineTotal,
  updateBudget,
  updateSubBudget,
} from './steps/OutlineStep'
import {
  DocumentEditModal,
  MaterialPanel,
  ResourceStep,
} from './steps/ResourceStep'
import {
  TopicStep,
} from './steps/TopicStep'

// ==================== 各阶段组件（v1.32 起在别的文件里） ====================
//
// 上面这一组 import 就是原文件里「各阶段组件」那一节。拆成 src/steps/ 与
// src/components/ 之后，这个文件只剩根组件 App 自己 —— 它原先同时装着 27 张常量表、
// 10 个纯函数、23 个组件，任何一处改动都要在一个近四千行的文件里找。
//
// **拆分的边界不是"行数好看"，而是"谁和谁共享状态"**：
//   * 常量表与纯函数（与 React 无关）→ src/constants.js、src/helpers.js
//   * 各阶段组件（只收 props、不持有跨步状态）→ src/steps/、src/components/
//   * 根组件留下的恰好是**不能拆的那部分**：35 个 useState、两套在跑登记，以及把它们
//     分发给各步的 renderStep。这些状态彼此耦合很紧（改选题要作废下游产物、引用一致性
//     有三个现算字段），抽成自定义 hook 属于另一件事，本轮没做。
//
// 拆完之后 test_frontend_conventions.py / test_frontend_mirror.py 的读法也跟着变了：
// 它们断言的是「这件事在**前端**只有一处实现」，所以从 v1.32 起读**整个 src/ 树**，
// 而不是只读这个文件 —— 否则搬走的那部分会不再被覆盖，而且是**静默**的。

import {
  FALLBACK_PAPER_TYPES,
  FALLBACK_TYPE_LABELS,
  FALLBACK_MIN_TARGET_WORDS,
  FALLBACK_WRITING_LANGS,
  FALLBACK_WRITING_LANG_LABELS,
  FALLBACK_DEFAULT_WRITING_LANG,
  FALLBACK_CITATION_FORMATS_BY_LANG,
  FALLBACK_DEFAULT_CITATION_FORMAT_BY_LANG,
  FALLBACK_SUPPORTED_FORMATS,
  FALLBACK_CITATION_FORMAT_LABELS,
  FALLBACK_WORDS_UNIT_BY_LANG,
  FALLBACK_CITE_STYLES,
  FALLBACK_CITE_STYLE_LABELS,
  DEFAULT_TYPE_KEY,
  SOURCE_TYPE_LETTERS,
  SOURCE_TYPE_LABELS,
  DOC_EDIT_FIELDS,
  FALLBACK_TYPE_CONFIG,
  POLLED_TASK_KINDS,
  TASK_POLL_MS,
  TASK_POLL_MAX_MS,
  STATUS_ANCHOR,
  STEP_DONE_STATUSES,
} from './constants'
import {
  wordsNumber,
  isMissingMeta,
  initialSourceType,
  projectLabel,
  safeFilename,
  configFor,
  buildSteps,
  statusToStepKey,
  materialsAnalyzing,
  materialsPendingOf,
} from './helpers'

export default function App() {
  const [projects, setProjects] = useState([])
  const [project, setProject] = useState(null)
  const [stepKey, setStepKey] = useState('topic')
  // 已经走到过的最远步骤。步骤条上「哪些格子能点」由它和服务端进度共同决定：
  // 若只看当前所在步骤，用户点回第 1 步之后，后面每一步都会立刻变灰、✓ 也没了，
  // 看起来像已经做过的东西被撤销了 —— 于是重新生成一遍，把好端端的正文覆盖掉。
  const [maxReachedStep, setMaxReachedStep] = useState(0)
  const [loading, setLoading] = useState(false)
  // 正在侧栏卡片里改名的那个项目 id。侧栏卡片整块是「打开项目」的点击区，编辑态
  // 必须把整行点击**冻结**：否则用户点一下卡片内边距（标题之外的地方）就会跳进项目页，
  // 而输入框随侧栏一起卸载 —— React 卸载节点不触发 onBlur，草稿会静默丢掉。
  const [editingId, setEditingId] = useState(null)
  const [error, setError] = useState('')
  // 提示而非报错：删除文献导致引用绑定作废这类「操作成功但有后果」的消息走这里
  const [notice, setNotice] = useState('')
  const [meta, setMeta] = useState({ llm_configured: false, llm_model: '' })

  // 状态数据
  const [paperType, setPaperType] = useState(DEFAULT_TYPE_KEY)
  const [targetWords, setTargetWords] = useState(8000)
  // 写作语言。它和论文类型一样是「选题与设定」的一部分，跟着 confirmed 的选题一起
  // 提交（见 confirmTopic），改动会作废下游产物（决策：生成过就锁定，走确认框）。
  const [writingLang, setWritingLang] = useState(FALLBACK_DEFAULT_WRITING_LANG)
  const [domain, setDomain] = useState('')
  const [topic, setTopic] = useState('')
  // 作者自己写的写作思路 / 论证思路（选填）。选题那一步此前只有一个标题输入框，而
  // 一个标题装不下「打算怎么论证」。它与选题同口径：改了就会作废下游六项产物。
  //
  // 长度上限刻意**不在前端镜像**（不加 maxLength、不做计数器）：它是后端
  // MAX_IDEAS_CHARS 一个真相 + 400 消息一个出口。加 maxLength 会让 400 永远不触发、
  // 粘贴超长时尾巴被无声吃掉，而那正是这个项目一路在消灭的静默失配。
  const [writingIdeas, setWritingIdeas] = useState('')
  const [topics, setTopics] = useState([])
  const [outline, setOutline] = useState(null)
  // 作者已给出的一手内容：研究设计 / 质性设计 / 工程设计 / 理论模型 / 技术方案。
  // 字段名随类型变化（见 TYPE_CONFIG.design_fields），形状恒为 {字段名: 值}。
  // 它必须先于大纲交给模型：否则第四章「实证分析」只能靠编数据。
  const [design, setDesign] = useState({})
  const [designNotes, setDesignNotes] = useState('')
  const [documents, setDocuments] = useState([])
  // 文献主题聚类（仅文献综述）：结构由文献决定，所以聚类是**先于大纲**的一份显式
  // 产物，作者可改可确认。未确认就让模型自己归纳也能出大纲，但作者对主题的掌控
  // 就没了 —— 这份数据的存在本身就是「让人能确认」。
  const [clusters, setClusters] = useState(null)
  // 作者自有的研究材料（数据 / 成果 / 核心思路）：与文献是两条独立的线，
  // 它们不进引用调度、不进参考文献列表，只作为正文素材。
  const [materials, setMaterials] = useState([])
  const [materialsPlan, setMaterialsPlan] = useState(null)
  const [materialDraft, setMaterialDraft] = useState({ label: '', text: '' })
  const [binding, setBinding] = useState(null)
  const [citeStyle, setCiteStyle] = useState('bracket')
  const [citationFormat, setCitationFormat] = useState('gb7714')
  const [sections, setSections] = useState(null)
  const [references, setReferences] = useState(null)
  // 文末列表的**片段形态**（[[{text, italic}]]），与 references 逐条并排，只有 docx
  // 排版读它（txt / md 是纯文本）。三项都在服务端算好下发 —— 前端不自己判哪一段该
  // 斜体：那等于把 APA/MLA 的斜体规则抄成第二份，而后端改格式时它不会跟着改。
  // **每一次 setReferences 都必须紧跟着配一次它**（三处注入 + 作废正文那一处清空，
  // 共四处）。漏配的后果不是报错而是印错：留着的片段还是上一版的，docx 于是在新的
  // 正文与新的预览旁边，把上一份文末列表的斜体范围印上去 —— 只有打开 Word 才看得见。
  // 这条配对由 test_frontend_conventions 的 setReferences/setReferenceRuns 用例看住。
  const [referenceRuns, setReferenceRuns] = useState(null)
  // 生成进度（后台任务 + 轮询）
  const [progress, setProgress] = useState(null)
  const [generating, setGenerating] = useState(false)
  // 通用后台任务进度：大纲生成 / 文献解析（后端 projects.task_state_json）
  const [task, setTask] = useState(null)
  const [showSettings, setShowSettings] = useState(false)

  const paperTypes = meta.paper_types || FALLBACK_PAPER_TYPES
  const typeConfig = meta.paper_type_config || FALLBACK_TYPE_CONFIG
  // 库里存短名、界面显示长名（含适用范围）。这只是显示层：提交给后端的仍是短名。
  const typeLabels = meta.paper_type_labels || FALLBACK_TYPE_LABELS
  // 目标总字数的下限（后端一个真相，见 FALLBACK_MIN_TARGET_WORDS）
  const minTargetWords = meta.min_target_words || FALLBACK_MIN_TARGET_WORDS
  // 写作语言与参考文献格式（后端一个真相，见 FALLBACK_* 那一段）
  const writingLangs = meta.writing_langs || FALLBACK_WRITING_LANGS
  const langLabels = meta.writing_lang_labels || FALLBACK_WRITING_LANG_LABELS
  const formatsByLang = meta.citation_formats_by_lang
    || FALLBACK_CITATION_FORMATS_BY_LANG
  const defaultFormatByLang = meta.default_citation_format_by_lang
    || FALLBACK_DEFAULT_CITATION_FORMAT_BY_LANG
  const formatLabels = meta.citation_format_labels || FALLBACK_CITATION_FORMAT_LABELS
  const formatNotes = meta.citation_format_notes_by_lang || {}
  // 全部格式（不分语言），只用来算「本语言下排除了哪几个」。见 FALLBACK_SUPPORTED_FORMATS。
  const allFormats = meta.citation_formats || FALLBACK_SUPPORTED_FORMATS
  const wordsUnits = meta.words_unit_by_lang || FALLBACK_WORDS_UNIT_BY_LANG
  // 角标样式：选项与文字都从后端来，与格式那一对同款（后端报错时用的是同一张表里的
  // 名字，这里是第二处落点，同源才不会让用户拿着报错文案找不到自己选过的那一项）。
  const citeStyles = meta.cite_styles || FALLBACK_CITE_STYLES
  const citeStyleLabels = meta.cite_style_labels || FALLBACK_CITE_STYLE_LABELS
  // 「这个语言下能选哪些格式」与「它的默认格式」各只有这一个出口：格式下拉框的选项、
  // 改语言时的回落、以及导出侧都不另写一份判据。写成函数而不是各处的下标取值，
  // 是因为 meta 里那两张表都可能还没有值（首屏），取下标会当场抛出 undefined。
  const formatsFor = (lang) =>
    formatsByLang[lang] || formatsByLang[FALLBACK_DEFAULT_WRITING_LANG] || []
  const defaultFormatFor = (lang) => defaultFormatByLang[lang] || formatsFor(lang)[0] || ''
  // 「库里那份格式在这个语言下还合不合法」——与后端 citation_format.effective_format
  // 同名同义，也是前端**唯一**一处这个判断：播种项目时（loadProject）与提交选题前
  // 算改动摘要（confirmTopicChange）读的都是它。两处各写一遍的后果很具体：界面上
  // 显示 APA、state 里还是 gb7714，或者反过来 —— 用户按着一个假值做决定。
  const effectiveFormatFor = (lang, fmt) =>
    formatsFor(lang).includes(fmt) ? fmt : defaultFormatFor(lang)
  // 计数单位（中文「字」/ 英文「words」）。认不出的语言按中文，与后端
  // writing_lang.words_unit 同一判据。
  const unitOf = (lang) => wordsUnits[lang] || FALLBACK_WORDS_UNIT_BY_LANG.zh
  // 选题确认后以服务端存的类型为准，避免用户改了下拉框却还没提交
  const effectiveType = project?.paper_type || paperType
  // 写作语言同一条尺子：**论文**是库里那一份，不是选题步上还没提交的草稿。引用调度步
  // 的格式选项、各处的计数单位、导出排版都要跟着论文走；后端那道 400（英文项目不许选
  // GB/T）读的也是库里这一列，两边判据因此是同一个。
  // 例外是选题步自己：它编辑的就是草稿，那里的单位要跟着**草稿**实时变 —— 否则用户
  // 选了英文，框里还写着「5000 字」，他没法知道自己该填什么。
  const effectiveLang = project?.writing_lang || writingLang
  const cfg = configFor(effectiveType, typeConfig)
  const hasDesign = !!cfg.design_fields
  const designLabel = cfg.design_label || '研究设计'
  // 结构由文献决定的类型（文献综述）：先注入文献、再生成大纲，摘要在前。
  const isReview = !!cfg.structure_from_literature
  // 已确认的簇（confirmed_at 有值才算）。列表页刷新后仍是同一份判断 —— 靠服务端字段，
  // 不靠本地的「刚刚确认过」标记。
  const confirmedClusters = clusters?.confirmed_at ? (clusters.clusters || []) : []
  const steps = buildSteps(effectiveType, typeConfig)
  const stepIndex = Math.max(0, steps.findIndex((s) => s.key === stepKey))
  // 服务端进度给出的前沿：status 随工序单调前进，所以它就等于「合法走到过哪一步」。
  // 刷新页面后本地记忆清零，这一步全靠它兜回来。
  const frontierIndex = Math.max(
    0,
    steps.findIndex((s) => s.key === statusToStepKey(project?.status || 'draft', steps)),
  )
  // 材料那一组的判定在这里算**一次**，往下传给两个消费点（文献注入步与引用调度步）：
  // 各算一遍就会各漂一次，而 CitationStep 连 task 都收不到、根本算不了它。
  const analyzing = materialsAnalyzing(task)
  const materialsPending = materialsPendingOf({ isReview, materials, materialsPlan, task })
  // 取三者之大：当前所在、本地走到过的最远处、服务端前沿。
  // 本地那项不能省 —— 「不注入文献，进入引用调度」这类跳过不会改 status，只靠服务端
  // 前沿的话，用户跳过之后回看一眼上一步，就再也点不回引用调度了。
  const reachableIndex = Math.max(stepIndex, maxReachedStep, frontierIndex)
  // 服务端后台任务在跑（大纲/文献解析/聚类…）。它们的进度与服务端任务共用
  // task_state_json 这一列，而「AI 推荐选题」的本地进度条也借用同一槽位、结束时
  // setTask(null) —— 会把正在轮询的服务端任务状态一并清掉，进度条凭空消失。
  const serverTaskRunning = task?.status === 'running' && POLLED_TASK_KINDS.has(task.kind)
  // 后端那**两套**忙闲登记在界面上的镜像：_busy_task()（大纲/文献解析/材料分析/设计
  // 提炼/聚类/引用调度）与 _gen_running()（正文生成）。凡是被这两道闸 409 的写入口，
  // 按钮都必须按这一条来禁 —— 各步此前只认**自己那一种**任务（outline / documents /
  // …），于是别的任务在跑时按钮是亮的，点下去必然 409。
  //
  // 与 serverTaskRunning 只差一处：把正文生成也算进来。生成任务不写 task_state_json
  // （它写 generation_state_json），kind 也不在 POLLED_TASK_KINDS 里，所以上面那一行
  // 看不见它，而它在后端是真的占着闸。
  //
  // 已知且刻意留着的一处不对齐：引用调度那个键（后端叫 citations:<id>）前端看不见 ——
  // 它不落 task_state_json、界面上也没有进度条（见 projects._citations_key）。它那
  // 120 秒里按钮仍可能是亮的，点下去会拿到 409，而那句 409 的文案现在能到用户眼前
  // （api.js 的 errorDetail）。要真正对齐得给它造第二条状态通道，代价与收益不相称。
  //
  // **上面这段在 v1.29 被推翻了**：那条盲区现在是关着的 —— 引用调度也写那一列、也在
  // POLLED_TASK_KINDS 里（见 _citations_key 与本文件 POLLED_TASK_KINDS 的注释），所以
  // 这 120 秒里 serverTaskRunning 为真、按钮是灰的，不必再走过去点一下拿 409 那条路。
  // 当初"要真正对齐得造第二条状态通道"这个判断的错处在于：它以为只能靠新通道，其实
  // 同一个槽 + 同一张登记表就够了（不多一个字段、不多一张表）。
  const serverBusy = serverTaskRunning || generating

  // 「我这一趟引用调度发出去了、回执还没回来」。存的是**项目 id**，不是布尔。
  //
  // 为什么还需要它（既然槽里已经有进度）：这条路由是**同步**的 —— 它自己 await 满一次
  // 上限 120 秒的模型调用才返回，所以**发出请求的那个标签页里没有任何可轮询的服务端
  // 状态**：此时 task 里还停着上一次任务的终态（多半是上一次调度留下的 done），轮询
  // 守卫（status === 'running'）根本不会通过。这一侧的进度条只能由本地这个判据渲染，
  // 本仓既有先例是「AI 推荐选题」（同样是"一次同步请求、没有服务端进度"，见
  // recommendTopics 里那句本地 setTask）。两端分工是干净的：本地这一半不读服务端状态，
  // 服务端那一半只由种子 + 轮询驱动 —— 所以不需要 request-id 之类的东西去对账。
  //
  // 存 id 而不是 true：这 120 秒里用户完全可能切到别的项目去，一个全局布尔会让那个
  // 项目上也冒出一条「正在执行引用调度」。比较点是当前 project?.id（见 renderStep 的
  // citation case），所以换项目即自动失效。
  const [scheduleInFlight, setScheduleInFlight] = useState(null)
  // 「当前打开的是哪个项目」。给 scheduleCitations 收尾时判用：那笔回执只属于发起它的
  // 那个项目。侧栏卡片点击**不受 loading 拦**（只有删除按钮拦），所以这 120 秒里切走
  // 是能做出来的，而回执落到另一个项目上会把它的绑定、错误横幅、甚至项目对象一起写歪。
  const projectIdRef = useRef(null)
  useEffect(() => {
    projectIdRef.current = project?.id
  }, [project?.id])

  useEffect(() => {
    api.meta().then(setMeta).catch(() => {})
    refreshProjects()
  }, [])

  // 走到过的最远处**只增不减**：若直接写成 setMaxReachedStep(stepIndex)，用户点回
  // 上一步时它就会跟着缩小，后面那些格子重新变灰 —— 正是要修的那个毛病。
  // 起算点由 loadProject 给（那里和 stepKey 一起从服务端重算）。
  useEffect(() => {
    setMaxReachedStep((m) => Math.max(m, stepIndex))
  }, [stepIndex])

  // 生成进度轮询。条件是「项目正在生成」而非「本地点了按钮」——这样刷新页面后
  // statusToStepKey 把人送回生成步，effect 自动重新起轮询，进度不丢。
  useEffect(() => {
    if (!project || project.status !== 'generating') return
    let alive = true
    let timer = null
    // 与下面的通用任务轮询同一套退避与「只撤自己那条错误」的处理（见那边的注释）。
    // 这里尤其不能失败即停：正文生成动辄几分钟，中途一次断连若终止轮询，用户看到的是
    // 一个永远停在某个百分比的进度条，而正文其实还在后台一节一节地写。
    let fails = 0
    let pollErr = ''

    async function tick() {
      if (!alive) return
      let res
      try {
        res = await api.generateProgress(project.id)
        if (!alive) return
      } catch (e) {
        if (!alive) return
        fails += 1
        pollErr = `生成进度查询失败（已连续 ${fails} 次）：${e.message}。生成仍在后台继续，恢复连接后进度会自动接上。`
        setError(pollErr)
        timer = setTimeout(tick, Math.min(TASK_POLL_MS * 2 ** fails, TASK_POLL_MAX_MS))
        return
      }
      if (pollErr) {
        const mine = pollErr
        pollErr = ''
        setError((prev) => (prev === mine ? '' : prev))
      }
      fails = 0
      setProgress(res)
      if (res.status === 'running') {
        setGenerating(true)
      } else {
        setGenerating(false)
        if (res.status === 'error') {
          setError(res.error || '生成失败')
        } else if (res.status === 'interrupted') {
          // D5：生成被服务重启打断。后端只在 done/completed 时才回传正文，interrupted
          // 不带 sections —— 若照旧代码 `setSections(res.sections || null)`，会把界面上
          // 已生成的章节整块清空，导出步随即改口说「尚无正文」，而库里其实还有逐节
          // 落盘的章节。这里反过来：用库里的 sections_json / references_json 恢复。
          setError(res.error || '生成任务因服务重启而中断，可重新发起生成')
          try {
            const p = await api.getProject(project.id)
            setProject(p)
            setSections(p.sections_json)
            setReferences(p.references_json)
            setReferenceRuns(p.referenceRuns || null)
          } catch {}
          return
        } else {
          setSections(res.sections || null)
          setReferences(res.references || null)
          setReferenceRuns(res.referenceRuns || null)
        }
        // 终态：拉一次最新项目状态，effect 依赖 status 变化后自然停轮询
        try {
          setProject(await api.getProject(project.id))
        } catch {}
        return
      }
      timer = setTimeout(tick, TASK_POLL_MS)
    }

    tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
    // 依赖 id 与 status 而非整个 project 对象：终态里会 setProject 换掉对象引用，
    // 若依赖对象本身，effect 会重挂载并无限重启轮询。
  }, [project?.id, project?.status])

  // 通用后台任务的轮询。与上面的生成进度同一套思路：条件是「项目上真的有任务在跑」
  // 而非「本地点了按钮」—— 刷新页面后 loadProject 从 task_state_json 种入初值，
  // effect 自动重新起轮询，几十秒的等待不会因为刷新而失去反馈。
  useEffect(() => {
    if (!project || task?.status !== 'running') return
    if (!POLLED_TASK_KINDS.has(task.kind)) return
    let alive = true
    let timer = null
    // 连续失败次数。既用来算退避，也写进提示语 —— 用户看到「已连续 3 次」才知道
    // 这是网络/服务的问题而不是自己点错了什么。
    let fails = 0
    // 本 effect 自己写进 error 的那一条。恢复时要**只撤这一条**：用字符串比对而不是
    // 清空全部，因为轮询失败期间用户完全可能在别处触发另一条报错（比如点了某个按钮
    // 被后端 409 拒绝），那条不该被轮询的恢复顺手抹掉。
    let pollErr = ''

    async function tick() {
      if (!alive) return
      let next
      try {
        const res = await api.taskProgress(project.id)
        if (!alive) return
        next = res.task || { status: 'idle' }
      } catch (e) {
        if (!alive) return
        fails += 1
        pollErr = `进度查询失败（已连续 ${fails} 次）：${e.message}。任务仍在后台运行，恢复连接后进度会自动接上。`
        setError(pollErr)
        // **任何一次失败都不结束轮询**，只是下一次来得更晚。原先这里写完 setError 就
        // 直接掉出 tick —— 一次网络抖动、一次后端重启，进度轮询就永久消失了：进度条
        // 停在原地、按钮一直禁用、也再没有第二次请求去发现服务已经回来。刷新页面是
        // 用户唯一的自救手段，而刷新恰好会丢掉刚问出的那些状态。
        timer = setTimeout(tick, Math.min(TASK_POLL_MS * 2 ** fails, TASK_POLL_MAX_MS))
        return
      }
      if (pollErr) {
        const mine = pollErr
        pollErr = ''
        setError((prev) => (prev === mine ? '' : prev))
      }
      fails = 0
      setTask(next)
      if (next.status === 'running') {
        timer = setTimeout(tick, TASK_POLL_MS)
        return
      }
      await finishTask(next)
    }

    tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
    // 与生成轮询同理：依赖 id / status / kind 而非 task 对象本身，否则每次
    // setTask 换掉对象引用都会重挂载 effect，轮询永远重启。
  }, [project?.id, task?.status, task?.kind])

  // 任务落定后收尾：结果都写在后端，这里只负责把它们取回来。
  async function finishTask(next) {
    try {
      const p = await api.getProject(project.id)
      setProject(p)
      if (next.kind === 'outline') {
        // 成功时是新大纲；失败时后端退回 TOPIC_SET，原大纲原样保留
        setOutline(p.outline_json)
      }
      if (next.kind === 'documents') {
        // 解析是逐篇入库的，中途失败也已有部分文献可用
        setDocuments(await api.listDocuments(project.id))
        // 新文献进来，旧的聚类就不完整了（后端已清空），这里同步跟上
        setClusters(p.clusters_json || null)
      }
      if (next.kind === 'material_analysis') {
        setMaterialsPlan(p.materials_plan_json)
      }
      if (next.kind === 'design_extract') {
        // 提炼结果直接回填表单，用户可以就地改；失败时后端没写库，表单保持原样
        setDesign(p.design_json?.fields || {})
        setDesignNotes(p.design_json?.notes || '')
      }
      if (next.kind === 'clusters') {
        // 生成完成即落库（confirmed_at 仍为空）：用户可以就地改标题再确认，
        // 失败时后端写的是 status=error，clusters_json 保持原样
        setClusters(p.clusters_json || null)
      }
      if (next.kind === 'citations') {
        // 库里的绑定是这一趟刚写下的那一份，而本地这份可能还是旧的（这个标签页在这次
        // 调度之前就开着、或者在等待期间切走又切回来）。**必须跟着换**：「确认引用
        // 绑定」提交的正是本地这份 binding（见 confirmCitations），不换就等于用户
        // 下一次盖章把刚调度好的结果顶回去 —— 而界面上没有任何迹象能让他看出来。
        setBinding(p.citation_binding_json)
      }
    } catch {}
    // ⑦ 逐篇的结果必须说出来。文献解析是逐篇入库的，「5 篇里 2 篇没进来」若一声
    // 不吭，任务栏一停就什么都看不出来，用户会以为全成功了 —— 而缺文献这件事要到
    // 引用调度那步才会露马脚，那时已经查不回原因了。
    // saved / failed / skipped / message 都在载荷里（后端 _write_task 把 extra 平铺到
    // 了顶层），缺的只是没人渲染。
    if (next.status === 'error') {
      setError(next.error || next.message || '任务失败')
    } else if (next.status === 'interrupted') {
      setError(next.error || '任务因服务重启而中断，可重新发起')
    } else if (next.kind === 'documents' && (next.failed?.length || next.skipped?.length || next.partial?.length)) {
      const summary = next.message || '文献解析完成'
      // 只列「哪几篇、为什么」，不再复述篇数：计数已经在 summary（后端任务文案）里
      // 说过了，这里再说一遍就成了「跳过 1 篇重复：跳过 1 篇重复：…」。哪条是重复、
      // 哪条是失败，靠 reason 本身分辨（「与《X》内容相同」/「元数据提取失败」），
      // 不用再加前缀标签。
      const parts = []
      if (next.failed?.length) {
        parts.push(next.failed.map((f) => `${f.filename}（${f.reason}）`).join('；'))
      }
      // 被跳过的重复要**点名到篇**：用户传了 10 篇、进来 8 篇，不点名就分不清
      // 哪两篇没进来，而这正是他下一步要据以写正文的清单。
      if (next.skipped?.length) {
        parts.push(
          next.skipped.map((s) => `${s.filename}（${s.reason}）`).join('；'),
        )
      }
      // 「进来了，但有几页没提取到文本」是第三类，此前后端一声不吭、界面也无从显示：
      // 文献明明在库里，可它的页码索引在缺的那几页上是空的，而正文的角标与页码就取自
      // 这份索引 —— 到引用调度那步才发现缺页，那时已经查不回原因了。同样点名到篇，
      // 页码就在 reason 里（「第 3、7 页文本提取失败」）。
      if (next.partial?.length) {
        parts.push(
          next.partial.map((p) => `${p.filename}（${p.reason}）`).join('；'),
        )
      }
      const detail = parts.join('；')
      // 一篇都没进来 ≠ 部分成功。全失败和部分失败都不该静默通过，但级别不同：
      // 前者是「这一步没做成」（红），后者是「做成了，但有缺口」（提示）。
      // **全是重复**既不属于「没做成」也不属于「有缺口」—— 库里本来就有这些文献，
      // 用户要的状态已经在了，用红字报错会让他以为上传挂了。
      if (next.saved === 0 && next.failed?.length) setError(`${summary}：${detail}`)
      else setNotice(`${summary}：${detail}`)
    }
  }

  async function refreshProjects() {
    const list = await api.listProjects()
    setProjects(list)
  }

  // 把服务端最新的项目对象取回来覆盖本地那一份。**只换 project 这一个 state**：
  // 与 loadProject 不同，它不重算步骤定位、不重置 maxReachedStep —— 那两件事属于
  // 「打开项目」，在用户刚点完某个按钮之后顺手做一遍，会把用户拽回服务的锚点、还把
  // 「走到过的最远处」抹掉。
  // 它存在的理由是：有些字段是后端每次 GET 才算出来的（status、citations_stale、
  // unbound_documents），它们不在任何写接口的返回值里，只有这一趟 GET 能让它们露面。
  // 后两个是**一对**（后端 _citation_facts 一起发），必须同时到齐，否则顶部那条常驻
  // 提示与引用调度步的高亮会各自按半份事实说话。
  async function syncProject() {
    const p = await api.getProject(project.id)
    setProject(p)
    return p
  }

  // 改名（标题 = 论文标题，列表与导出下载文件名都用它）。页头与侧栏卡片共用一个入口。
  // 更新本地副本时**只并 title / display_title 两个字段**，不做整份 setProject：POST
  // /title 的响应是路由返回的库行，不含 GET 详情那三个现算的事实（citations_stale、
  // unbound_documents、status），整份替换会让顶部那条「正文与当前引用设置不一致」常驻
  // 提示和引用调度步的高亮一起凭空消失。display_title
  // 一律取服务端给的值，前端不自己拼一份（那就是第二份推导，正是本项目栽过的坑）。
  async function renameProject(id, nextTitle) {
    await run(async () => {
      const p = await api.renameProject(id, nextTitle)
      setProject((prev) =>
        (prev && prev.id === id ? { ...prev, title: p.title, display_title: p.display_title } : prev),
      )
      // 侧栏列表是另一份 state：不刷新的话，改名后「← 返回」看到的还是旧名单
      await refreshProjects()
    })
  }

  // 删除整个项目（后端按外键级联删掉它的文献与材料）。列表项本身是「打开项目」的
  // 点击区，删除按钮在 onClick 里必须 stopPropagation，否则点删除会顺带把项目打开。
  async function deleteProject(p) {
    const label = projectLabel(p)
    if (!window.confirm(
      `确定删除《${label}》？该项目的文献、研究材料与已生成正文会一并删除，此操作不可撤销。`,
    )) {
      return
    }
    await run(async () => {
      await api.deleteProject(p.id)
      // 删的正好是当前打开的项目时退回首页，否则界面会停在一个已经不存在的数据上
      if (project?.id === p.id) setProject(null)
      await refreshProjects()
    })
  }

  async function loadProject(item) {
    // 打开项目时以**详情**为准，而不是列表项。列表接口给的是库里的列，而
    // citations_stale / unbound_documents 都是详情接口每次现算的（后端 get_project）
    // —— 不取详情，「正文已按旧编号生成」的常驻提示和引用调度步的「执行引用调度」
    // 高亮在刚打开项目时就是空的，要等下一次操作触发 syncProject 才突然冒出来。
    // 取不到就退回列表项，至少别打不开。
    // 列表接口**刻意**不挂这两个字段（见 projects.py 的 list_projects）：为一张侧栏
    // 卡片白查一遍文献库不划算。
    let p = item
    try {
      p = await api.getProject(item.id)
    } catch {}
    setProject(p)
    setTopic(p.topic || '')
    // 必须播种：confirmTopic 走的是完整的 loadProject，而它提交时会把这个 state 一起
    // 发回去。不播种的话，用户重新打开项目后框里是空的，点一下确认就把库里的思路
    // 清成空串 —— 而这次"清空"会被判定成真的改了，平白作废六项产物。
    setWritingIdeas(p.writing_ideas || '')
    setPaperType(p.paper_type || DEFAULT_TYPE_KEY)
    // 必须播种（与 writing_ideas 同理）：confirmTopic 走的是完整的 loadProject，而它
    // 提交时会把这个 state 一起发回去 —— 不播种的话，用户重新打开项目后语言选择框
    // 会落到浏览器的第一个选项上，点一下确认就把库里那篇英文论文改成中文。
    // 语言先落一个局部值：播种格式与播种语言必须是**同一个**语言值，各写一遍
    // `p.writing_lang || FALLBACK_...` 就有两处判据（下面那行就是靠它算的）。
    const langNow = p.writing_lang || FALLBACK_DEFAULT_WRITING_LANG
    setWritingLang(langNow)
    setTargetWords(p.target_words || 8000)
    setOutline(p.outline_json)
    setDesign(p.design_json?.fields || {})
    setDesignNotes(p.design_json?.notes || '')
    setClusters(p.clusters_json || null)
    setBinding(p.citation_binding_json)
    setCiteStyle(p.cite_style || 'bracket')
    // 归一化再播种：库里存的组合可能是非法的（乱改过的旧库、或改过语言没跟着改格式
    // 的存量项目），直接塞进 state 会得到一个不在下拉选项里的值 —— 界面显示第一个
    // 选项、state 却是另一个，直到提交才被后端纠正。判据与提交前那句摘要同一处。
    setCitationFormat(effectiveFormatFor(langNow, p.citation_format))
    setSections(p.sections_json)
    setReferences(p.references_json)
    setReferenceRuns(p.referenceRuns || null)
    setProgress(p.generation_state_json)
    // 通用任务的初值也来自服务端，刷新页面后进度条与已等待秒数自动续上
    setTask(p.task_state_json || null)
    // ③ 服务重启会把在飞任务标成 interrupted 并写好原因，但此时**没有轮询在跑**
    // （轮询的守卫只认 running），所以加载时必须自己说一声 —— 否则那个任务就无声
    // 消失了，用户面对一个空文献列表，连「刚才那批解析被中断了」都无从知道。
    const seededTask = p.task_state_json
    if (seededTask?.status === 'interrupted') {
      setError(seededTask.error || '有任务因服务重启而中断，可重新发起')
    }
    // 刷新页面时若项目仍在生成，先按「生成中」显示，避免按钮在首次轮询回来前
    // 短暂可点（点了会被后端以 409 拒绝）
    setGenerating(p.status === 'generating')
    // 步骤条的记忆和所有产物一样，也在这里从服务端重新起算。两个理由：
    // 换项目时上一个项目走到过的步骤不该继续可点；改选题把下游产物作废之后，
    // 后面那些格子若还亮着 ✓，看起来就像正文还在。
    // **必须与下一行在同一次同步提交里**：下面「只增不减」那条 effect 正是以新
    // 步骤为起点把值抬起来的，分两次提交就会互相盖。
    setMaxReachedStep(0)
    // 步骤定位在类型确定之后算，保证用的是这个项目自己的工序顺序
    setStepKey(
      statusToStepKey(
        p.status,
        buildSteps(p.paper_type || paperType, typeConfig),
      ),
    )
    // 文献列表拉取。它有一个**条件**（见下），而 documents 是**项目级** state ——
    // 清与拉必须同进同退，用的是同一个判据，少任何一半都会出事：
    //
    // ① 换项目（含首次打开）：**先清空、再无条件拉一次**。不清的后果不是「显示旧
    //    数据」这么轻 —— 列表里每一条右边的「删除」（ResourceStep）只拿得到 doc id，
    //    而它可能来自上一个项目的列表，于是「在 Y 的界面上点删除」会真删掉 X 的文献，
    //    并按 /documents 的既有逻辑把 X 的引用绑定整体作废（后端 v1.19 起在路径上
    //    也要项目 id，两边各一道）。
    // ② 同一个项目：**不重拉也不清**。confirmTopic 走的就是完整的 loadProject，而它
    //    刚把大纲与绑定作废、状态退回 topic_set —— 下面三个条件全假。但此时手上的
    //    列表本来就是本项目的（它只被本项目自己的上传 / 删除 / 解析收尾改过），清了
    //    就再也取不回来：界面停在「已上传文献（0 篇）」而库里还有 N 篇，文献综述更会
    //    因此被 needsDocs 挡在原地。不重拉的理由是那份载荷：list_documents 会把每篇
    //    文献的页码全文解析出来（实测最重的项目 9 篇 228 KB），不值得为一次没变过的
    //    列表再拉一遍。
    // 三个条件本身是旧的判据，保留原意：resources_loading 覆盖「解析跑完但还没生成
    // 大纲」的项目（文献综述正是如此），否则刷新后文献列表是空的。
    const projectSwitched = project?.id !== p.id
    if (projectSwitched) {
      setDocuments([])
      // 换项目时把上一个项目的「推荐选题」与「研究领域」一并清掉。它们都是本地瞬态
      // （不进库，loadProject 从服务端也拿不到），不清就留在 B 的选题步：A 的推荐卡
      // 一点就把 B 的「确定研究核心方向」填成 A 的建议，A 的研究领域也照样回填进
      // 输入框。
      setTopics([])
      setDomain('')
    }
    if (projectSwitched
      || p.outline_json
      || p.citation_binding_json
      || p.status === 'resources_loading') {
      try {
        setDocuments(await api.listDocuments(p.id))
      } catch {}
    }
    // 材料列表无条件拉取：它不依赖大纲，用户完全可能先把数据传好再走前面的步骤
    try {
      setMaterials(await api.listMaterials(p.id))
    } catch {}
    setMaterialsPlan(p.materials_plan_json || null)
  }

  // 唯一的前进入口：顺序变了不用改任何导航代码
  function goNext(fromKey) {
    const i = steps.findIndex((s) => s.key === fromKey)
    if (i < 0 || i + 1 >= steps.length) return
    setStepKey(steps[i + 1].key)
  }

  async function handleCreate() {
    setLoading(true)
    setError('')
    try {
      // 不传标题：新建项目的名字由「选题」跟随而来（后端 set_topic）。写死一个
      // 「未命名论文」会让它永远非空，跟随条件（空着或还等于旧选题）就永不成立 ——
      // 于是项目永远叫未命名，用户填的研究核心方向白填。
      const p = await api.createProject()
      await refreshProjects()
      await loadProject(p)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  async function run(fn) {
    setLoading(true)
    setError('')
    setNotice('')
    try {
      await fn()
      // 返回成败：调方据此决定要不要继续下一步。此前这里吞掉异常却不吱声，
      // 「作废并重写」就在作废那步失败后照样发起生成（D6）。
      return true
    } catch (e) {
      setError(e.message)
      return false
    } finally {
      setLoading(false)
    }
  }

  // 阶段一：选题
  async function recommendTopics() {
    // 按钮已经禁掉了，这里再挡一道：进度槽位是共享的，一旦覆盖掉正在轮询的服务端
    // 任务状态，那条任务的进度条就再也回不来了（轮询不会自己恢复）。
    if (serverTaskRunning) {
      setError('有任务正在进行，请等它结束后再重新推荐')
      return
    }
    // 同上，按钮也已经按字数禁掉了，这里再挡一道（旧标签页上的界面可能绕过来）。
    // 走 error 横幅而不是静默返回：目标字数不足时那次推荐的 match 是拿一个 0 评的，
    // 用户拿到手也看不出来它为什么都不对。
    if (wordsNumber(targetWords) < minTargetWords) {
      // 单位跟着**草稿**语言（与 TopicStep 里的那两句同源）：英文项目下说「最少 1000 字」，
      // 用户会以为自己填错了数，而实际是单位不对。
      setError(
        `目标总字数最少 ${minTargetWords} ${unitOf(writingLang)}，请先在上面填好再推荐选题`,
      )
      return
    }
    // 一次同步请求，没有后端任务可轮询 —— 本地记下起始时刻，触发同一条进度条
    setTask({
      kind: 'topics', status: 'running', message: '正在生成推荐选题…',
      started_at: new Date().toISOString(),
    })
    try {
      await run(async () => {
        const res = await api.recommendTopics(project.id, {
          domain, paper_type: paperType, target_words: wordsNumber(targetWords), count: 3,
          // 语言也要发：这一步的候选选题标题会成为论文标题，而项目还没确认过 ——
          // 库里那份 writing_lang 还是建项目时的默认值（中文）。不发的话，用户在
          // 这个表单里选了英文、点「推荐」，拿回来的是中文选题。
          writing_lang: writingLang,
        })
        setTopics(res.topics)
      })
    } finally {
      setTask(null)
    }
  }

  // ④ 选题/类型/字数/语言都在大纲上游：改动它们，后端会把大纲、引用绑定、材料计划、
  // 聚类、正文与参考文献整份作废（覆盖式，不可撤销）。这是全流程唯一会毁数据的操作，
  // 值得一次打断：先把「会失去什么」列清楚，并请用户先去存一份。
  //
  // **格式的级联在前端一处都不做**（虽然它看起来该在这里）：语言与格式是一对写在一个
  // 事务里的值，后端 set_topic 会在同一个事务里把不再合法的格式落成该语言的默认值。
  // 前端跟着改一份，就多了一个"改了、但没提交"的中间态 —— 用户在选题步切了英文又按下
  // 取消，本地格式已经变成 APA 而库里还是 GB/T，而界面上没有任何东西能让他看出这件事。
  // 提交后 loadProject 会把两个值一起从服务端重新播种，那才是唯一真相。
  function confirmTopicChange() {
    // 写作思路比最后要 trim 两边再比：后端就是这个口径（多行输入框里敲完顺手回车，
    // 末尾带个换行是常态）。不 trim 的话会出现"弹了作废警告、后端却什么都没作废"
    // 的假警报——方向是安全的，但用户会白紧张一次，以为刚毁了一份大纲。
    const langChanged = (project?.writing_lang || FALLBACK_DEFAULT_WRITING_LANG)
      !== writingLang
    const changed = (project?.topic || '') !== topic
      || (project?.paper_type || '') !== paperType
      || Number(project?.target_words || 0) !== Number(targetWords || 0)
      || (project?.writing_ideas || '').trim() !== writingIdeas.trim()
      || langChanged
    if (!changed) return true
    const willLose = []
    if (sections?.length) willLose.push(`已生成的 ${sections.length} 节正文`)
    if (outline) willLose.push('大纲')
    if (binding) willLose.push('引用绑定')
    if (materialsPlan) willLose.push('材料融入计划')
    // 聚类**只在语言变了时才作废**（后端的 set_topic 就是这个条件）：它的主题名与概述
    // 会成为文献综述的节标题与节内容，是**产物文字** —— 留着一份中文聚类去写英文论文，
    // 正是本轮要消灭的那类。而改选题/改类型时它刻意留着（它由文献本身决定，与选题
    // 无关），所以这里不能无条件写进去 —— 那是一句对"只改了字数"的用户说的假话。
    if (langChanged && clusters?.clusters?.length) willLose.push('研究主题聚类')
    // 什么都没产出过就不必打扰：空项目改选题是常态
    if (!willLose.length) return true
    // 格式的级联**并列**说一条。判据取"格式是否真的变了"，不取"是不是改语言引起的"：
    // 后端 set_topic 每一次都会重算 `effective_format(lang, 库里那个格式)`，所以只要
    // 这句话出现，它就一定会发生。不猜原因，只陈述当前是什么、会变成什么、以及为什么
    // 原格式留不住。
    const formatAfter = effectiveFormatFor(writingLang, citationFormat)
    const label = (f) => formatLabels[f] || f
    const langLine = formatAfter === citationFormat ? ''
      : `\n\n参考文献格式会从「${label(citationFormat)}」改为「${label(formatAfter)}」：`
        + `${label(citationFormat)} 在${langLabels[writingLang] || writingLang}写作下不可选`
        + '（下拉框里也不会再出现它）。'
    return window.confirm(
      `改动选题与设定会作废这些下游产物：${willLose.join('、')}。${langLine}\n\n`
      + '作废后需要重新生成，且不可撤销。若当前正文已经写得差不多，'
      + '建议先到「预览导出」步复制或下载保存一份。\n\n确定继续？',
    )
  }

  async function confirmTopic() {
    if (!confirmTopicChange()) return
    await run(async () => {
      const p = await api.setTopic(project.id, {
        // 发出去的一律是数字：草稿可以是空串，数字不行（后端的 target_words 是 int，
        // 空串会被 pydantic 判成 422，而前端拿到的 detail 是数组、渲染成 [object Object]）。
        // 归一只有 wordsNumber 那一处；这里它已经把「空」落成 0，交由后端那道 400 拒绝。
        paper_type: paperType, target_words: wordsNumber(targetWords), topic,
        writing_ideas: writingIdeas, writing_lang: writingLang,
      })
      // 走完整的 loadProject，而不是只 setProject：改选题会把下游产物整份作废，
      // 前端必须把 outline / sections / references / binding 这些**本地副本**一起
      // 清掉。只 setProject 的话，界面上还挂着一份已经不在库里的旧大纲，用户会
      // 拿它继续往下点。
      // 步骤定位也交给它：status=topic_set 经 statusToStepKey 正好是「按本类型的
      // 工序前进一步」（文献综述去文献注入，带设计步的类型去设计录入）。
      await loadProject(p)
      // 确认选题可能刚把标题跟着改掉（跟随），而侧栏列表是另一份 state
      await refreshProjects()
    })
  }

  // 阶段二前置：文献主题聚类（仅文献综述）。一次 LLM 调用，走后台任务 + 轮询。
  async function generateClusters() {
    await run(async () => {
      await api.generateClusters(project.id)
      setTask({
        kind: 'clusters', status: 'running',
        message: '正在从文献中归纳研究主题…',
        started_at: new Date().toISOString(),
      })
    })
  }

  // 确认聚类：把（可能已改过标题的）簇写回后端并盖章。此后生成的大纲会以它为优先。
  async function confirmClusters(nextClusters) {
    await run(async () => {
      const res = await api.confirmClusters(
        project.id, nextClusters, clusters?.notes || '',
      )
      // 回读服务端的版本而不是用本地这份：后端会重算 doc_titles、重排 id，
      // 并丢弃「文献已被删光」的簇 —— 本地这份不知道这些
      const p = await api.getProject(project.id)
      setProject(p)
      setClusters(p.clusters_json || null)
      // 一条提示说清两件事，不叠两次 setNotice（后一次会把前一次覆盖掉）
      const parts = [`主题聚类已确认（${res.cluster_count} 个主题）`]
      if (res.outline_stale) {
        // 非破坏性：大纲还在，只是不再反映这份聚类。提示而非作废，改哪一条由用户决定
        parts.push('当前大纲是按旧聚类生成的，建议重新生成大纲')
      } else {
        parts.push('生成大纲时将按它组织研究主题脉络章')
      }
      if (res.dropped) {
        parts.push(`另有 ${res.dropped} 个主题因所属文献已被删除而未保存`)
      }
      setNotice(`${parts.join('；')}。`)
    })
  }

  // 阶段二：大纲。后端「点火即返回」，两遍 LLM 约 36 秒，进度靠上面的轮询 effect。
  async function generateOutline() {
    // 设计字段是分析类章节唯一的依据。一个都没填就生成，模型只能编——编变量、
    // 编显著性、编结论，而这份虚构在确认点①上看起来完全合理。后端不硬拦
    // （用户可能确实想先看看结构），但后果必须说清楚再让用户决定。
    if (hasDesign && !Object.values(design).some((v) => (v || '').trim())) {
      const ok = window.confirm(
        `还没填写${designLabel}，大纲里的分析/结果章节可能编造数据与结论。\n` +
        `建议先填写${designLabel}（也可上传附件让 AI 提炼）。仍要生成大纲？`,
      )
      if (!ok) return
    }
    // 文献综述的主题脉络章本该由**作者确认过的**聚类来组织。未确认就往下走，模型
    // 会自己归纳一套主题 —— 结构上仍然完整，只是作者对主题的掌控没了。不硬拦
    // （与设计未填同款：告警放行），但要让用户知道自己放弃了什么。
    if (isReview && !confirmedClusters.length) {
      const has_any = (clusters?.clusters || []).length > 0
      const ok = window.confirm(
        has_any
          ? '主题聚类尚未确认，生成大纲时会由模型自行从文献中归纳主题，' +
            '你改过的簇标题不会被采用。仍要生成？'
          : '尚未生成主题聚类，大纲里的研究主题脉络将由模型自行从文献中归纳。' +
            '建议先生成并确认聚类。仍要生成？',
      )
      if (!ok) return
    }
    await run(async () => {
      await api.generateOutline(project.id)
      // 乐观置位，让进度条立刻出现，不必等第一次轮询（1.2 秒后）
      setTask({
        kind: 'outline', status: 'running',
        message: '正在拟定章节结构与标题…',
        started_at: new Date().toISOString(),
      })
    })
  }

  // 研究设计：保存手改后的字段。提示写在回调内部 —— run 会吞掉异常，
  // 写在后面的话保存失败也会说「已保存」。
  async function saveDesign() {
    await run(async () => {
      const res = await api.saveDesign(project.id, design)
      setDesign(res.design?.fields || design)
      setNotice('研究设计已保存，生成大纲时会作为已知条件交给模型')
    })
  }

  // 上传设计附件：文件进的是 materials 表（与「资源注入」步同一份存储），
  // 所以后文「材料融入分析 → 按节注入正文」那条链路一行都不用改。
  async function uploadDesignFiles(e) {
    const files = Array.from(e.target.files || [])
    if (!files.length) return
    await run(async () => {
      const res = await api.uploadMaterials(project.id, files)
      setMaterials(res.materials)
      setMaterialsPlan(null)
      if (res.failed?.length) {
        setNotice(
          `${res.saved} 份材料已添加，${res.failed.length} 份未能解析：` +
          res.failed.map((f) => `${f.filename}（${f.reason}）`).join('；'),
        )
      }
    })
    e.target.value = ''
  }

  // 提炼：一次 LLM 调用，可能要几十秒，走后台任务 + 轮询
  async function extractDesign() {
    await run(async () => {
      await api.extractDesign(project.id)
      setTask({
        kind: 'design_extract', status: 'running',
        message: `正在从材料中提炼${designLabel}…`,
        started_at: new Date().toISOString(),
      })
    })
  }

  async function confirmOutline() {
    await run(async () => {
      const res = await api.confirmOutline(project.id, outline)
      if (!res.matched) {
        setError(`字数总和(${res.total_budget})与目标(${res.target_words})不一致`)
        return
      }
      if (res.binding_invalidated) {
        // 引用绑定以章节标题为 join key，标题改了旧绑定就全废了
        setBinding(null)
        setError('大纲章节已变更，原有引用绑定已失效，请重新执行引用调度')
      }
      if (res.material_plan_invalidated) {
        // 材料分析计划同样以章节标题为键，失效后材料会静默进不了正文
        setMaterialsPlan(null)
        setNotice('大纲章节已变更，研究材料的分析结果已作废，请重新分析材料')
      }
      setProject(await api.getProject(project.id))
      goNext('outline')
    })
  }

  // 阶段三：文献。N 篇 PDF = N 次串行解析，可能好几分钟，同样交给后台任务。
  async function uploadFiles(e) {
    const files = Array.from(e.target.files || [])
    if (!files.length) return
    await run(async () => {
      await api.uploadDocuments(project.id, files)
      setTask({
        kind: 'documents', status: 'running',
        message: `正在解析文献 0/${files.length}…`,
        done: 0, total: files.length,
        started_at: new Date().toISOString(),
      })
    })
    // 允许重复选择同一批文件（否则第二次不会触发 change）。与 uploadMaterials 同一行
    // 处理同源，而这里更要紧：上传**失败**时用户手上就是那几个文件（比如正文生成中
    // 被 409 拒了），不清空的话他连原样重试一次都做不到。
    e.target.value = ''
  }

  // 删除文献。若它已被引用绑定引用，后端会作废绑定并退回待调度，这里同步回来。
  async function deleteDocument(doc) {
    if (!window.confirm(`确定删除《${doc.title || doc.filename}》？此操作不可撤销。`)) {
      return
    }
    await run(async () => {
      const res = await api.deleteDocument(project.id, doc.id)
      setDocuments(await api.listDocuments(project.id))
      const p = await api.getProject(project.id)
      setProject(p)
      setClusters(p.clusters_json || null)
      setBinding(p.citation_binding_json)
      // 两处「已作废」必须累进到同一句提示，不能各写一句 setNotice —— 删掉一篇
      // 既被引用绑定、又进了某簇的文献时，binding_invalidated 与 clusters_invalidated
      // 同时为真，第二句会把第一句整个盖掉，用户就只看到「聚类已作废」、漏掉「绑定
      // 已作废、请重新调度」，照着一句不完整的提示去操作。
      const invalidationNotes = []
      if (res.binding_invalidated) {
        invalidationNotes.push(
          '该文献已被引用绑定引用，绑定已作废，请在「引用调度」步骤重新调度后再生成正文。',
        )
      }
      if (res.clusters_invalidated) {
        invalidationNotes.push('该文献属于已生成的主题聚类，聚类已作废，请重新生成主题聚类。')
      }
      if (invalidationNotes.length) setNotice(invalidationNotes.join(' '))
    })
  }

  // 保存一篇文献的元数据（编辑界面的提交口）。**刻意不走 run()**：那条路把失败原因
  // 写进顶部横幅，而横幅在模态框的遮罩后面 —— 用户看不到，只会觉得点了保存没反应。
  // 所以返回 {ok, message}，由模态框自己把原因显示在按钮上方。
  async function saveDocument(doc, patch) {
    try {
      await api.updateDocument(project.id, doc.id, patch)
    } catch (e) {
      return { ok: false, message: e.message }
    }
    // 写库已经成功。下面只是把界面上的副本拉回来，这一段失败**不能**报成「保存失败」
    // —— 库里确实改了，说失败会让用户以为白改一遍、再去改一次。
    try {
      setDocuments(await api.listDocuments(project.id))
      // 改题名时后端会顺带刷新绑定里冻结的那份 doc_title，本地这份要跟上，否则引用
      // 调度步显示的还是旧题名。这一趟 getProject 顺带取回 citations_stale —— 它是
      // 详情接口现算的事实，顶部那条「文末列表要重排」的常驻提示正由它驱动。
      const p = await syncProject()
      setBinding(p.citation_binding_json)
      setNotice('文献元数据已保存。')
    } catch (e) {
      setNotice(
        `文献元数据已保存，但界面上的副本没能刷新（${e.message}）；`
        + '重新打开这个项目就能看到最新值。',
      )
    }
    return { ok: true }
  }

  // 研究材料：解析是纯本地文本提取，同步返回，不需要轮询
  async function uploadMaterials(e) {
    const files = Array.from(e.target.files || [])
    if (!files.length) return
    await run(async () => {
      const res = await api.uploadMaterials(project.id, files)
      setMaterials(res.materials)
      setMaterialsPlan(null)
      if (res.failed?.length) {
        setNotice(
          `${res.saved} 份材料已添加，${res.failed.length} 份未能解析：` +
          res.failed.map((f) => `${f.filename}（${f.reason}）`).join('；'),
        )
      }
    })
    // 允许重复选择同一个文件（否则第二次不会触发 change）
    e.target.value = ''
  }

  async function addMaterialText() {
    const text = materialDraft.text.trim()
    if (!text) return
    await run(async () => {
      setMaterials(await api.addMaterialText(project.id, {
        label: materialDraft.label.trim(),
        text,
      }))
      setMaterialsPlan(null)
      setMaterialDraft({ label: '', text: '' })
    })
  }

  async function deleteMaterial(m) {
    if (!window.confirm(`确定删除材料「${m.label}」？`)) return
    await run(async () => {
      await api.deleteMaterial(project.id, m.id)
      setMaterials(await api.listMaterials(project.id))
      setMaterialsPlan(null)
    })
  }

  // 材料分析是一次 LLM 调用，可能要几十秒，走后台任务 + 轮询
  async function analyzeMaterials() {
    await run(async () => {
      await api.analyzeMaterials(project.id)
      setTask({
        kind: 'material_analysis', status: 'running',
        message: '正在分析研究材料如何融入正文…',
        started_at: new Date().toISOString(),
      })
    })
  }

  // 阶段四：引用调度
  //
  // 这一趟最长要等 120 秒（后端那次相关性判断的上限），是本应用唯一一个"正常也要等
  // 一两分钟"的动作，所以它自己做三件别的动作不需要做的事：本地置起进度条的判据、
  // 收尾时按项目 id 对账、失败也要对一次账（见下）。
  async function scheduleCitations() {
    const pid = project.id
    setScheduleInFlight(pid)
    try {
      const ok = await run(async () => {
        const res = await api.scheduleCitations(pid)
        // 用户可能已经切到别的项目去了。这笔回执只属于 pid：往另一个项目的界面上写
        // 绑定/提示，再 await syncProject()（它读的是闭包里那个 project.id）把项目
        // 对象也换成 pid 的，就会留下"侧栏停在 B、正文与按钮是 A"的混合状态。
        if (projectIdRef.current !== pid) return
        setBinding(res.binding)
        // 归属是模型判的还是兜底铺的，只有这一趟回执说得出来（method）。走兜底时**必须
        // 说出来**：不做声的话，界面上「按相关性归好了」与「按上传顺序铺开了」长得一模
        // 一样，而后者未必贴合章节主题，用户得自己核对 —— 那正是绑定列表里那些 reason
        // 该出现却没有出现的时候。
        if (res.method === 'uniform') {
          setNotice(
            '这次没拿到模型的相关性判断（未配置模型、超时或输出不可解析），已退回确定性兜底：'
            + '按上传顺序把文献均匀铺到大纲各节。引用照样可用，但归属未必贴合章节主题，请核对下面的绑定。',
          )
        }
        // 三种引用改动（重新调度 / 确认 / 跳过）都要把项目重取一遍：citations_stale
        // 是后端按「当前绑定 vs 正文那一轮的编号」现算的，只有 GET 才拿得到。少了这步，
        // 用户把绑定改回原样、警告该消失时它还在，反之亦然。
        await syncProject()
      })
      if (projectIdRef.current !== pid) return
      // **失败也要对账**：这一趟的失败有两条很不一样的来路 —— 后端拒了（409/500，绑定
      // 一字没动），或者请求根本没走完（网络、客户端超时）而后端照样把这次调度跑完并
      // 写进了库。后一条下本地那份 binding 是旧的，而「确认引用绑定」提交的正是本地
      // 这份（见 confirmCitations）—— 不去对账的话，用户下一次盖章会把刚调度好的绑定
      // 悄悄顶回去，而且界面上没有任何迹象能让他看出来。
      if (!ok) {
        const p = await syncProject()
        if (projectIdRef.current === pid && p) setBinding(p.citation_binding_json)
      }
    } finally {
      setScheduleInFlight(null)
    }
  }

  // 只改了文末列表排版（refs_change.kind === 'render'）：按当前设置重排列表。
  // 后端零模型调用、不碰正文，且只对「编号与指向都没变」开放 —— 其余情形会被 400，
  // 前端在那种时候给的是另一个按钮（见 GenerateStep 的三态）。
  async function rerenderReferences() {
    await run(async () => {
      await api.rerenderReferences(project.id)
      await syncProject()
      setNotice('文末参考文献列表已按当前引用设置重排，正文一个字都没有动。')
    })
  }

  // 作废已生成的正文并立刻重写一版。此前想重写正文只能回「选题与设定」改一个字段
  // 绕路 —— 那会连大纲、引用绑定、设计与材料计划一起覆盖式作废，与「只想重写正文」
  // 完全不相称。这里两步连成一次点击：先作废（后端只清正文/列表基线/生成进度），
  // 再走既有的生成流程（含进度轮询）。
  async function rewriteSections() {
    const list = sections || []
    const words = list.reduce((s, x) => s + (x.actual_words || 0), 0)
    const ok = window.confirm(
      `作废并重写正文？\n\n`
      + `将作废已生成的 ${list.length} 节正文${words > 0 ? `（约 ${words} ${unitOf(effectiveLang)}）` : ''}，`
      + `然后立刻按当前设置重新生成一版。\n\n`
      + `大纲、引用绑定、设计与材料计划、已注入的文献与材料都会保留。`,
    )
    if (!ok) return
    // 作废那步失败（409 忙 / 网络）时不许继续发起生成 —— 否则生成会在一份**没作废的
    // 旧正文**上继续跑，与「作废并重写」这个承诺相反。run 现在返回成败，据此拦截。
    const resetOk = await run(async () => {
      await api.resetSections(project.id)
      // 正文与文末列表基线一起没了（后端没动绑定），本地这两份副本要跟上 ——
      // 否则生成一开始，界面上还挂着上一版的章节列表与参考文献列表。
      setSections(null)
      setReferences(null)
      // 清空要**成对**：只清字符串那半的话，片段那半还挂着上一版，下面 syncProject
      // 落地之前那一瞬，任何按位置读 referenceRuns 的路径都会拿到已经作废的斜体范围。
      setReferenceRuns(null)
      await syncProject()
    })
    if (!resetOk) return
    await generate()
  }

  async function confirmCitations() {
    await run(async () => {
      await api.confirmCitations(project.id, binding, citeStyle, citationFormat)
      await syncProject()
      goNext('citation')
    })
  }

  // 跳过引用调度：本文不产生参考文献列表。
  // 七类都允许跳过（用户定），但后果必须写在确认框里 —— 「跳过」听起来只是略过
  // 一步，实际是这份稿子永远不会有参考文献列表，而学位论文几乎都需要它。
  async function skipCitations() {
    const label = typeLabels[effectiveType] || effectiveType
    const ok = window.confirm(
      `跳过引用调度？（当前类型：${label}）\n\n` +
      '跳过后：全文不产生参考文献列表，正文不出现任何引用角标，\n' +
      '正文将按「不引用任何文献」撰写（连一般性的文献观点佐证也不会出现）。\n\n' +
      '学位论文、期刊投稿与多数课程作业都需要参考文献。确定跳过？',
    )
    if (!ok) return
    await run(async () => {
      await api.skipCitations(project.id)
      setBinding(null)
      await syncProject()
      goNext('citation')
    })
  }

  // 阶段五：生成。后端「点火即返回」，真正的进度靠上面的轮询 effect 推进。
  async function generate() {
    await run(async () => {
      const res = await api.generate(project.id)
      setGenerating(true)
      setProgress({
        status: 'running', done: res.done || 0, total: res.total || 0,
        current: '', words_so_far: 0, words_target: targetWords,
        eta_seconds: null, error: null,
      })
      setProject(await api.getProject(project.id))
    })
  }

  // 阶段六：导出回执。文件已经由 ExportStep 交给浏览器了（exporters.js 在本地拼
  // Word / txt / Markdown），这里只把「走完了终点」记进后端状态机（completed →
  // exported）。**下载与记账必须分开对待**：记账失败不能回头说成「导出失败」——
  // 文件是真的导出成功了，那是假话。所以不走 run() 的报错通道，而是用提示如实说
  // 「文件已导出，只是没记上」，并让用户可以直接再点一次（接口幂等）。
  async function markExported() {
    try {
      const p = await api.markExported(project.id)
      // 只并进来，不整份替换：这个接口不回 citations_stale / unbound_documents
      // （那两条由 GET /projects/{id} 每次现算），整份替换会把顶部那条常驻提示
      // 凭空抹掉一次，下一次操作才又冒出来。
      setProject((cur) => ({ ...cur, ...p }))
    } catch (e) {
      setNotice(`文件已导出，但这次的导出没有记入项目状态（${e.message}）。`)
    }
  }

  // 按 key 渲染（而非序号），顺序调整时这里天然跟着走。
  // 序号 stepNo 由工序推出，供各卡片标题显示「②」之类的角标。
  function renderStep() {
    const stepNo = stepIndex + 1

    switch (stepKey) {
      case 'topic':
        return (
          <TopicStep
            stepNo={stepNo}
            paperType={paperType} setPaperType={setPaperType}
            // 语言这里给的是**草稿**（连同 setter），不是 effectiveLang：本步编辑的
            // 就是这份草稿，字数单位要跟着它实时变
            writingLang={writingLang} setWritingLang={setWritingLang}
            writingLangs={writingLangs} langLabels={langLabels}
            formats={formatsFor(writingLang)} formatLabels={formatLabels}
            // 被本语言排除掉的那几个格式的说明：由 formats 与 allFormats 现算，
            // 文案本身来自后端（见 TopicStep 里那段）。这两项与 formats 同源同语言。
            allFormats={allFormats} formatNotes={formatNotes[writingLang] || {}}
            targetWords={targetWords} setTargetWords={setTargetWords}
            wordsUnit={unitOf(writingLang)}
            minWords={minTargetWords}
            domain={domain} setDomain={setDomain}
            topic={topic} setTopic={setTopic}
            writingIdeas={writingIdeas} setWritingIdeas={setWritingIdeas}
            topics={topics}
            paperTypes={paperTypes}
            typeLabels={typeLabels}
            typeConfig={typeConfig}
            onRecommend={recommendTopics}
            onConfirm={confirmTopic}
            loading={loading}
            task={task}
            taskRunning={serverTaskRunning}
            busy={serverBusy}
          />
        )

      case 'design':
        return (
          <DesignStep
            stepNo={stepNo}
            designLabel={designLabel}
            fields={cfg.design_fields || []}
            design={design}
            setDesign={setDesign}
            notes={designNotes}
            onSave={saveDesign}
            onUpload={uploadDesignFiles}
            onExtract={extractDesign}
            materialCount={materials.length}
            llmConfigured={meta.llm_configured}
            onNext={() => goNext('design')}
            loading={loading}
            task={task}
            busy={serverBusy}
          />
        )

      // 大纲步的「目标字数」比对必须用**已落库**的 project.target_words，不是选题框里
      // 那个还没提交的本地草稿 targetWords：用户在选题步改了字数却没确认时两者会分家，
      // 而大纲是拿落库字数生成的，比对目标就得用落库值，否则「已匹配」会跟后端
      // （读库里 target_words）不一致。
      case 'outline':
        return (
          <OutlineStep
            stepNo={stepNo}
            outline={outline}
            setOutline={setOutline}
            onGenerate={generateOutline}
            onConfirm={confirmOutline}
            loading={loading}
            targetWords={project?.target_words || 8000}
            // 各节字数预算的单位（中文「字」/ 英文「words」）。它跟着**论文**的语言走，
            // 与导出、进度条上那些数字同一口径。
            wordsUnit={unitOf(effectiveLang)}
            isReview={isReview}
            hasDesign={hasDesign}
            designLabel={designLabel}
            designFilled={Object.values(design).some((v) => (v || '').trim())}
            documentCount={documents.length}
            clusters={clusters}
            themeSections={cfg.theme_sections || 0}
            onGenerateClusters={generateClusters}
            onConfirmClusters={confirmClusters}
            task={task}
            busy={serverBusy}
          />
        )

      case 'resources':
        return (
          <ResourceStep
            stepNo={stepNo}
            documents={documents}
            onUpload={uploadFiles}
            onDeleteDoc={deleteDocument}
            onEditDoc={saveDocument}
            materials={materials}
            materialsPlan={materialsPlan}
            materialDraft={materialDraft}
            setMaterialDraft={setMaterialDraft}
            onUploadMaterials={uploadMaterials}
            onAddMaterialText={addMaterialText}
            onDeleteMaterial={deleteMaterial}
            onAnalyzeMaterials={analyzeMaterials}
            onNext={() => goNext('resources')}
            loading={loading}
            isReview={isReview}
            citationsRequired={!!cfg.citations_required}
            nextLabel={
              isReview ? '完成文献注入，进入大纲生成' : '完成资源注入，进入引用调度'
            }
            task={task}
            busy={serverBusy}
            analyzing={analyzing}
            materialsPending={materialsPending}
          />
        )

      case 'citation':
        return (
          <CitationStep
            stepNo={stepNo}
            binding={binding}
            citeStyle={citeStyle}
            setCiteStyle={setCiteStyle}
            citationFormat={citationFormat}
            setCitationFormat={setCitationFormat}
            // 格式下拉框的数据源：这个语言下**能选哪些**、以及选中后显示成什么。
            // 用 effectiveLang（库里那一列）而不是草稿：后端那道 400 读的是库里这个值，
            // 两边判据必须同一个，否则会出现"界面让选、后端拒收"。
            formats={formatsFor(effectiveLang)}
            defaultFormat={defaultFormatFor(effectiveLang)}
            formatLabels={formatLabels}
            formatNotes={(formatNotes[effectiveLang] || {})}
            // 角标样式不随语言变，所以这里给的是整份清单，不是按语言取出来的那一份
            citeStyles={citeStyles}
            citeStyleLabels={citeStyleLabels}
            onSchedule={scheduleCitations}
            onConfirm={confirmCitations}
            onSkip={skipCitations}
            onNext={() => goNext('citation')}
            citationsRequired={!!cfg.citations_required}
            isReview={isReview}
            loading={loading}
            documents={documents}
            projectStatus={project?.status}
            generating={generating}
            busy={serverBusy}
            // 本步那条进度条的两个来源：task（服务端那份，见 CitationStep 里的
            // serverScheduling）与 scheduleInFlight（本地点了这一下、回执还没回来）。
            // 后者按项目 id 比一次，否则切到别的项目时那条条会跟着跑过去。
            task={task}
            scheduleInFlight={scheduleInFlight === project?.id}
            // 服务端现算的事实：库里还有几篇没进绑定。**必须是 `|| 0`** —— 写项目的
            // 那几个接口（新建 / 确认选题 / 改名）的回执里没有这个字段，loadProject 在
            // 详情取不到时会退回那份回执，`undefined > 0` 是 false 所以安全；改成 !== 0
            // 或 Number(...) 就会让一条提示凭空出现或消失。
            unboundDocuments={project?.unbound_documents || 0}
            materialsPending={materialsPending}
            materialsCount={materials.length}
          />
        )

      case 'generate':
        return (
          <GenerateStep
            stepNo={stepNo}
            onGenerate={generate}
            onRerender={rerenderReferences}
            onRewrite={rewriteSections}
            // 服务端现算的第三个事实（与 citations_stale / unbound_documents 同一组）：
            // 到底哪一样变了、该用哪个补救。`|| {}` 与 unbound_documents 那条同理 ——
            // 写项目的几个接口的回执里没有它，缺了按「没变」渲染，不会误报。
            refsChange={project?.refs_change}
            loading={loading}
            generating={generating}
            busy={serverBusy}
            progress={progress}
            sections={sections}
            wordsUnit={unitOf(effectiveLang)}
            onView={() => goNext('generate')}
          />
        )

      case 'export':
        return sections && sections.length > 0 ? (
          <ExportStep
            stepNo={stepNo}
            sections={sections}
            project={project}
            references={references}
            referenceRuns={referenceRuns}
            wordsUnit={unitOf(effectiveLang)}
            busy={serverBusy}
            onExported={markExported}
          />
        ) : (
          <div className="card">
            <h2>{stepNo}. 预览与导出</h2>
            <p className="muted">尚无正文内容，请先在「分段生成」中完成生成。</p>
          </div>
        )

      default:
        return (
          <div className="card">
            <p className="muted">未知步骤</p>
          </div>
        )
    }
  }

  if (!project) {
    return (
      <div className="app">
        <header className="header">
          <h1>AI 学术写作辅助系统</h1>
          <div className="header-right">
            <p className="muted">
              0 幻觉引用 · 精准字数控制 · 高确定性体验
              {!meta.llm_configured && (
                <span className="warn-badge"> LLM 未配置</span>
              )}
            </p>
            <button className="btn" onClick={() => setShowSettings(true)}>⚙ 设置</button>
          </div>
        </header>
        <div className="layout">
          <aside className="sidebar">
            {/* 这里原先放的是「+ 新建论文项目」按钮。创建入口同屏只留一个，现在
                统一挪到中间卡片上（见右栏），位置和文案都不再随项目数变——原先
                项目为空时它在卡片上叫「新建项目开始」、有项目时它在侧栏顶部叫
                「+ 新建论文项目」，同一个动作两处两个名字。
                这一栏于是只剩一件事：列出全部项目。标题写「我的论文项目」而不是
                「历史记录」——列表里既有草稿也有正在写的稿子，叫「历史」会让人
                以为里面只有已归档的东西。 */}
            <h3 className="sidebar-title">我的论文项目</h3>
            <div className="project-list">
              {projects.length === 0 && <p className="muted">暂无项目</p>}
              {projects.map((p) => (
                <div
                  key={p.id}
                  className="project-item"
                  // 编辑态冻结整行点击（见 editingId 的注释）
                  onClick={editingId === p.id ? undefined : () => loadProject(p)}
                >
                  <div className="project-body">
                    <div className="project-title">
                      <EditableTitle
                        value={projectLabel(p)}
                        topic={p.topic}
                        onRename={(t) => renameProject(p.id, t)}
                        onEditingChange={(on) => setEditingId(on ? p.id : null)}
                      />
                    </div>
                    <div className="muted">
                      {p.paper_type || '未设置'} · {p.target_words || '?'}
                      {unitOf(p.writing_lang)}
                    </div>
                  </div>
                  <button
                    className="btn btn-sm btn-danger"
                    title="删除该项目"
                    disabled={loading}
                    onClick={(e) => { e.stopPropagation(); deleteProject(p) }}
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>
          </aside>
          <main className="content">
            <div className="card empty-state">
              <h2>开始你的学术写作</h2>
              <p className="muted">
                通过「GUI 卡片引导 + 后台 Agent 工作流」，分步完成从选题到成稿的全过程。
              </p>
              <button className="btn btn-primary" onClick={handleCreate} disabled={loading}>
                新建论文项目
              </button>
              {projects.length > 0 && (
                <p className="muted">要接着写，从左侧「我的论文项目」里打开已建的项目。</p>
              )}
            </div>
          </main>
        </div>

        {showSettings && (
          <SettingsModal
            onClose={() => setShowSettings(false)}
            onSaved={() => { setShowSettings(false); api.meta().then(setMeta) }}
          />
        )}
      </div>
    )
  }

  return (
    <div className="app">
      <header className="header">
        <div className="header-left">
          <button className="btn" onClick={() => { setProject(null); setTask(null) }}>← 返回</button>
          <h1>
            {/* 生成期间也不禁用改名：后端明确允许（改名不写 status、不作废任何产物），
                前端禁掉就自相矛盾了 */}
            <EditableTitle
              className="title-edit-h1"
              value={projectLabel(project)}
              topic={project.topic}
              onRename={(t) => renameProject(project.id, t)}
            />
          </h1>
        </div>
        <div className="header-right">
          <p className="muted">
            {project.paper_type || '未设置'} · 目标 {project.target_words || '?'}
            {' '}{unitOf(effectiveLang)}
          </p>
          <button className="btn" onClick={() => setShowSettings(true)}>⚙ 设置</button>
        </div>
      </header>

      {/* 步骤条。走到过（i ≤ reachableIndex）的格子都能点，**包括往后跳回去**：
          用户回看前面步骤时后面并未撤销，只是被挡住了 —— 越是点不动，越容易让人
          以为要重做。未到达的格子显式锁住并给出原因，而不是「看着能点、点了没反应」。
          done 用 reachableIndex 而非 stepIndex 判定：回看第 1 步时，第 5 步的 ✓ 不该消失。 */}
      <div className="stepper">
        {steps.map((s, i) => {
          const reachable = i <= reachableIndex
          const done = reachable && i !== stepIndex
          return (
            <div
              key={s.key}
              className={`step ${done ? 'done' : ''} ${i === stepIndex ? 'active' : ''} ${reachable ? '' : 'locked'}`}
              onClick={() => reachable && setStepKey(s.key)}
              title={reachable ? undefined : '完成前面的步骤后即可查看'}
            >
              <div className="step-dot">{done ? '✓' : i + 1}</div>
              <div className="step-label">{s.label}</div>
            </div>
          )
        })}
      </div>

      {error && <div className="error-banner">{error}</div>}
      {/* 正文与当前引用编号不一致的常驻条。两版的分界与措辞都由服务端给（refs_change
          的 kind 与 summary），前端只渲染 —— 见 RefsStaleBanner 上面的注释。 */}
      {project.citations_stale && (
        <RefsStaleBanner change={project.refs_change} sections={sections} />
      )}
      {/* 正文与当前大纲的章节标题分家（body_outline_mismatch）：终态项目改过标题、
          正文没重写。与 citations_stale 同是「当下算出来的事实」，但补救是重写正文。 */}
      {project.body_outline_mismatch && (
        <div className="stale-banner">
          <b>正文与当前大纲不一致。</b>
          {' '}章节标题改过后正文还没重写，导出的仍是旧标题的正文 —— 请到「分段生成」步点一下「重写正文」，再重新生成。
        </div>
      )}
      {notice && (
        <div className="notice-banner">
          {notice}
          <button className="btn btn-sm" onClick={() => setNotice('')}>
            知道了
          </button>
        </div>
      )}

      {/* 正文区的 boundary：**顶栏、左侧项目列表、步骤条都在它外面**，所以某一节
          渲染炸了的时候，用户还能切项目、还能重试，而不是整个应用消失。

          `key` 取项目 id 是这一层唯一的重置判据（见 ErrorBoundary.jsx 的注释）：
          换项目就自动复位，否则一次异常之后切到别的项目仍停在错误卡片上，
          看起来像「这软件坏了」，而坏的其实只是上一个项目的那一次渲染。 */}
      <main className="content">
        <ErrorBoundary key={project?.id || 'no-project'} label="正文区">
          {renderStep()}
        </ErrorBoundary>
      </main>

      {showSettings && (
        <SettingsModal
          onClose={() => setShowSettings(false)}
          onSaved={() => { setShowSettings(false); api.meta().then(setMeta) }}
        />
      )}
    </div>
  )
}

/** 绑定里某一章的条目；读不懂的形状一律跳过。
 *
 * 这份绑定由后端从请求体原样收下（只保证最外层是对象），所以 `{"绪论": null}`、
 * `{"绪论": 5}`、`{"绪论": [null]}` 都可能出现在 props 里。而在渲染期对一个 null
 * 调 .map 抛的是渲染异常 —— 代价是**整个界面白屏**，远大于少显示一条归属。
 * 与后端 projects._binding_entries 是同一条规矩：非对象条目跳过，判据只此一份。
 */

