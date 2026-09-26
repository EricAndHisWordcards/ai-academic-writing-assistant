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

// 各阶段的显示名（key 是稳定标识，序号随工序变化，见 buildSteps）
const STEP_LABELS = {
  topic: '选题与设定',
  design: '研究设计',
  outline: '大纲与字数',
  resources: '文献注入',
  citation: '引用调度',
  generate: '分段生成',
  export: '预览导出',
}

// 元信息加载完成前 / 加载失败时的兜底（正常以后端 /api/meta 为准，内容与
// backend/app/paper_types.py 的 TYPE_CONFIG 对齐）。
// 工序的真身在**后端**：后端的闸门（谁必须先有文献、谁必须有引用绑定）与前端
// 步骤条是同一件事的两面，各存一份清单迟早漂移成「前端让点、后端 400」。
//
// **只镜像前端真用得上的字段**：steps / design_label / design_fields /
// structure_from_literature / citations_required / flow_hint。
// TYPE_CONFIG 里的 structure_note 与 writing_note 是进提示词的文本，前端从不读，
// 抄一份到这里是纯粹的漂移来源（改了后端忘了改前端，两份还会长得一模一样）。
const FALLBACK_PAPER_TYPES = [
  '定量/实证研究', '质性研究/案例分析', '工程设计/系统实现', '理论推导/数理建模',
  '文献综述', '课程论文/小论文', '技术/工程报告',
]
// 短名 → 下拉框里显示的长名（含适用范围），正常由 /api/meta 的 paper_type_labels 下发。
const FALLBACK_TYPE_LABELS = {
  '定量/实证研究': '定量/实证研究（社科、经管、医学、部分理科）',
  '质性研究/案例分析': '质性研究/案例分析（人文社科、新闻、教育、公共管理）',
  '工程设计/系统实现': '工程设计/系统实现（计算机、电子、机械、建筑等工科）',
  '理论推导/数理建模': '理论推导/数理建模（数学、理论物理、理论经济学）',
  '文献综述': '文献综述（全学科通用）',
  '课程论文/小论文': '课程论文/小论文（全学科通用[短篇]）',
  '技术/工程报告': '技术/工程报告（工科、实践类项目）',
}
// 目标总字数的下限。正常由 /api/meta 的 min_target_words 下发（下限的真身在
// 后端 projects.MIN_TARGET_WORDS —— 同 paper_types / steps 那两条：清单散落两处
// 迟早漂移，而这里的漂移形态很具体：改了后端，界面上的提示会静静地继续报旧数字）。
// 这一份只在元信息到达前、或它失败时用，处境与 FALLBACK_TYPE_* 相同。
//
// **它与上限的处理刻意不同，不是双标**：写作思路那个上限（后端 MAX_IDEAS_CHARS）
// 在前端一个字段都不镜像，因为镜像成 maxLength 会**静默吃掉**粘贴进来的尾巴、让
// 后端那道 400 永远不触发；下限则**不改动用户敲进去的任何字符**，只是提前把结果
// 告诉他 —— 后端那道 400 仍然照旧在那儿（前端这份就算过期，用户也只是多等一次往返）。
const FALLBACK_MIN_TARGET_WORDS = 1000

// 写作语言与参考文献格式的兜底镜像。真身在**后端**（app/writing_lang.py 与
// app/citation_format.py），正常由 /api/meta 下发；这一份只在元信息到达前、或它
// 失败时用，处境与 FALLBACK_PAPER_TYPES 相同（test_frontend_mirror.py 与后端对账）。
//
// **为什么非镜像不可**：格式下拉框必须按语言派生，而它派生不出东西来的时候是**空的**
// 下拉框（`[].map()` 一个选项都没有），比给错了还糟。这几张表加起来的体积骗不了人，
// 但它们不是「第二份格式清单」—— 清单只有后端一份，这里只是它到达之前的那一帧。
const FALLBACK_WRITING_LANGS = ['zh', 'en']
const FALLBACK_WRITING_LANG_LABELS = { zh: '中文', en: '英文' }
const FALLBACK_DEFAULT_WRITING_LANG = 'zh'
// 语言 → 该语言下符合习惯的著录格式（顺序即下拉框顺序）。英文写作里没有 GB/T 7714 ——
// 这条判据在后端也只有一份（citation_format._FORMATS_BY_LANG），后端那道 400 读的
// 就是它。
const FALLBACK_CITATION_FORMATS_BY_LANG = {
  zh: ['gb7714', 'apa', 'mla'],
  en: ['apa', 'mla'],
}
const FALLBACK_DEFAULT_CITATION_FORMAT_BY_LANG = { zh: 'gb7714', en: 'apa' }
// 后端认识的全部著录格式（不分语言）。用途只有一个：算出**本语言下被排除掉**的那几个，
// 好把「它们为什么不在这儿」说清楚 —— 那句话本身也来自后端（见 formatNotes），
// 前端只管挑哪几个该说。真身是 citation_format.SUPPORTED_FORMATS。
const FALLBACK_SUPPORTED_FORMATS = ['gb7714', 'apa', 'mla']
// 格式标识 → 选项文字。「（默认）」**不在标签里**：默认格式随语言变，写进标签就会在
// 另一种语言下印着一个错的默认标记 —— 它由前端按上面那张 default 表现判。
const FALLBACK_CITATION_FORMAT_LABELS = {
  gb7714: 'GB/T 7714', apa: 'APA（第 7 版）', mla: 'MLA（第 9 版）',
}
// 目标字数的计数单位：中文按字、英文按词。它同时是**显示单位**与后端 count_units 的
// 计数口径，所以英文项目里显示「目标 5000 字」不只是文案不对 —— 用户会照着填 5000，
// 而产出的是 5000 个词（约等于中文七千多字的体量）。
const FALLBACK_WORDS_UNIT_BY_LANG = { zh: '字', en: 'words' }
// 角标样式（不随语言变，所以只有一份）。与 FALLBACK_CITATION_FORMAT_LABELS 同款：
// 名字的真身在后端（citation_format.CITE_STYLE_LABELS），这里只是它到达前的那一帧。
// 它不是可有可无的装饰 —— 后端那道 400 用的是同一张表里的名字，两边同源才不至于出现
// 「报错说你选的 [1] 方括号角标不支持」这种用户对不上号的句子。
const FALLBACK_CITE_STYLES = ['bracket', 'superscript']
const FALLBACK_CITE_STYLE_LABELS = {
  bracket: '[1] 方括号角标', superscript: '纯数字上标（¹ ² 风格）',
}

// 「目标总字数」框里那串字符 → 数字。空串、'0'、敲了一半的 '-' 都归一到 0：
// 库里没有「空」这个表示（同 writing_ideas 的空串落 NULL），0 就是「还没填」。
// 归一只有这一处：草稿回灌与提交前的检查都读它，两处各写一遍就迟早不一致。
//
// 用 Number 而不是 parseInt：type="number" 的框交回来的本来就是**浮点字面量**，
// 而 parseInt 是按十进制前缀截的 —— 它会把 "1e3" 读成 1。用户敲 "1e3" 时框里
// 明明写着 1e3，拒绝语却会说「当前 1 字」，那是对用户刚敲的东西的一句假话。
// 小数截断（1000.5 → 1000）是刻意的：字数没有半个。
const wordsNumber = (v) => Math.trunc(Number(v)) || 0

// 必须是 FALLBACK_TYPE_CONFIG 的既有键，否则 configFor 兜不回来
const DEFAULT_TYPE_KEY = '课程论文/小论文'
const DESIGN_STEPS = ['topic', 'design', 'outline', 'resources', 'citation', 'generate', 'export']
// 与后端 app/metadata.py 的 MISSING_META_VALUES **同源**：模型在文献里找不到作者/年份时
// 不会留空，而是写「未提及」「Not specified」这类占位词——它们是普通字符串，不加判断
// 就会被当成真实作者名显示或导出。test_frontend_mirror.py 会断言两边逐字一致。
const MISSING_META_VALUES = [
  '未提及', '未提供', '未说明', '未标注', '未给出', '未注明', '未知', '不详',
  '无', '空', '暂无', '缺失',
  'not specified', 'not given', 'not stated', 'not provided', 'unspecified',
  'unknown', 'n/a', 'na', 'none', 'null', 'nil',
  '-', '--', '—', '－',
]
const isMissingMeta = (v) =>
  !v || MISSING_META_VALUES.includes(String(v).trim().toLowerCase())

// 文献类型标识白名单，**与后端 app/citation_format.py 的 SOURCE_TYPES 同源**
// （test_frontend_mirror.py 对账）。它现在只有一个消费点：文献编辑界面的类型下拉框
// （选项由它派生，见 DocumentEditModal）。越界值回落 J 那一半在**后端**
// `clean_source_type` —— 前端不再自己拼参考文献，也就没有第二处需要收敛。
const SOURCE_TYPE_LETTERS = ['J', 'M', 'D', 'C', 'N', 'R', 'S', 'P', 'G', 'Z']

// 类型标识的中文名，只给编辑界面的下拉项当注解。**不是第二份白名单**：下拉项的字母
// 由 SOURCE_TYPE_LETTERS 派生（见 DocumentEditModal），这里漏一个键只会让那一项少了
// 注解文字，不会少一个可选项 —— 两份清单因此不可能漂移。
const SOURCE_TYPE_LABELS = {
  J: '期刊文章', M: '专著', D: '学位论文', C: '会议录', N: '报纸',
  R: '报告', S: '标准', P: '专利', G: '汇编', Z: '其他',
}

// 类型下拉框的初值：库里那份标识**收敛之后**的形态。抽出来的值可能不在白名单里
// （模型写了别的字母），那种值在下拉框里没有对应选项、React 会把选中项显示成空白；
// 小写字母（如 'j'）同理，下拉框里只有大写。两种都收敛成空 —— 界面上明确显示为
// 「未设置」，而不是一个看起来像空白、实际存着脏值的框。
const initialSourceType = (doc) => {
  const upper = String(doc?.source_type || '').trim().toUpperCase()
  return SOURCE_TYPE_LETTERS.includes(upper) ? upper : ''
}

// 文献元数据编辑表单的字段表。顺序即界面顺序：前八项按期刊的著录顺序排
// （题名 → 作者 → 英译题名 → 年份 → 来源 → 卷期页），非期刊类型专属的三项排在最后。
//
// **这张表同时是提交范围**：buildPatch 遍历它来构造 PATCH，所以一个后端收得下、
// 却不在表里的字段，用户在界面上**根本填不进去** —— 表现为「模型没抽到就永远空着」，
// 而界面看起来完全正常。加列时这里与后端 DOC_FIELD_LIMITS 必须成对改
// （test_frontend_mirror 有一条断言盯着这对清单）。
//
// `source_type` 也不在这张表里，但理由是另一个：它是下拉框，不能手输 ——
// 手输越界后端会 400，而用户看不出「J」与「j」的区别。
const DOC_EDIT_FIELDS = [
  { key: 'title', label: '题名', hint: '留空的话，文末列表会退回用文件名当题名。' },
  {
    key: 'authors',
    label: '作者',
    // 三种格式**各自**整理姓名，形态互不相同 —— 此前这里写的是「其余格式原样输出」，
    // 那句在本轮之后就是假话（APA / MLA 也整理）。并列穷举三种形态，不指认哪一种。
    hint: '姓名会按文末列表选用的格式整理成规范形态：'
      + 'GB/T 7714 → 姓全大写、名缩写（SMITH J K），超过 3 位只列前 3 位并加「等 / et al.」；'
      + 'APA → 姓前名后、缩写带点（Smith, J. K.），末位作者前用 &，超过 20 位只列前 19 位与末位；'
      + 'MLA → 首位倒装、名全写（Smith, John K.），两位用 and 连接，三位以上用 et al.。',
  },
  {
    key: 'title_en',
    label: '英译题名（选填）',
    // 这一项只对英语的两种格式有意义。**留空是正常的**：题名本身是英文的文献，
    // 以及本轮之前上传的存量文献（抽取时还没索取这个字段），这里都是空的。
    hint: '题名的英译（不是题目换一种写法）。只有 APA / MLA 用得上：非英语文献按 APA '
      + '第 7 版要在原题名后以方括号附英译。GB/T 7714 不用它；题名本身是英文的留空即可。',
  },
  { key: 'year', label: '年份' },
  {
    key: 'source',
    // 五个名字并列穷举，因为 GB/T 7714 的**五种类型各往这一项里装不一样的东西**。
    // 只写「刊名 / 出版社 / 报纸名」的话，填学位论文的用户看不出学校名该放哪 ——
    // 而这一项是那一项唯一的入口。
    label: '来源（刊名 / 出版社 / 学位授予单位 / 论文集名 / 报纸名）',
  },
  { key: 'volume', label: '卷' },
  { key: 'issue', label: '期' },
  { key: 'page_range', label: '页码', placeholder: '56-75' },
  // ---- 以下三项只服务于非期刊类型（GB/T 7714 的 [M]/[D]/[C]/[N]）----
  // 每一项都写清「哪种类型用得上」：这三项与上面八项不同，它们**不是普适的**，
  // 在期刊论文上填了不会被任何格式读出来（触发条件只按类型读自己的那几列，
  // 见 citation_gb_types._TRIGGERS）。所以提示要说到每一步，不能只说「选填」。
  {
    key: 'place',
    label: '出版地',
    hint: '只有 GB/T 7714 的专著 [M]、学位论文 [D]、会议论文 [C] 用得上：'
      + '填城市名，如「北京」。期刊论文与报纸没有这一项，留空即可。',
  },
  {
    key: 'edition',
    label: '版本项',
    hint: '只有 GB/T 7714 的专著 [M] 用得上：如「第3版」「修订本」「2nd ed.」。'
      + '第 1 版不必著录，留空即可。',
  },
  {
    key: 'publish_date',
    label: '报纸出版日期',
    hint: '只有 GB/T 7714 的报纸 [N] 用得上：填到日，如「2023-05-04」。'
      + '写成「2023年5月4日」也会按这个形态打印。其余类型留空即可 —— '
      + '年份填在上一项「年份」里，不要填在这里。',
  },
]

// 这里原先有一个 normalizePages（页码归一化），它只服务于前端那份兜底的参考文献
// 拼接实现。那一份已删，理由写在 ExportStep 的 formatRef 上；页码归一化现在只有
// 后端 citation_format.normalize_pages 一处。

// 项目显示名（同时也是论文标题：用户起的名字优先，其次是他填的研究核心方向）。
// **推导只有后端一份**（projects._display_title，跟着每个项目接口下发 display_title），
// 这里纯粹是「前后端版本不一致时别整屏空白」的最后一手，不是第二份推导 —— 原先的
// 5 个消费点各写一遍 `|| '未命名论文'`（其中导出两处写的是 `|| '论文'`）就是漂移现场。
const projectLabel = (p) => p?.display_title || '未命名论文'

// 下载文件名。Windows 上 \ / : * ? " < > | 都是非法字符，结尾的点和空格也存不下去，
// 清洗后可能变空（标题整串都是 `???`）→ 回退到兜底词，免得下载出一个 `.md` 隐藏文件。
// 只用于 a.download：md 正文里的一级标题要用原标题，冒号在 markdown 里完全合法。
const safeFilename = (name, fallback = '论文') =>
  String(name || '')
    .replace(/[\\/:*?"<>|]/g, '_')
    .trim()
    .replace(/[.\s]+$/, '')
    .slice(0, 80) || fallback
const FALLBACK_TYPE_CONFIG = {
  '定量/实证研究': {
    steps: DESIGN_STEPS,
    citations_required: false,
    design_label: '实证设计',
    design_fields: ['数据源与样本', '研究假设', '模型与方法', '主要结果'],
    flow_hint: '选题 → 变量与数据/假设 → 拟合模型 → 回归/统计分析',
  },
  '质性研究/案例分析': {
    steps: DESIGN_STEPS,
    citations_required: false,
    design_label: '质性设计',
    design_fields: ['研究对象与个案', '质性方法', '资料来源与编码', '主题与发现'],
    flow_hint: '选题 → 质性方法（访谈/扎根/案例）→ 编码/主题分析',
  },
  '工程设计/系统实现': {
    steps: DESIGN_STEPS,
    citations_required: false,
    design_label: '工程设计',
    design_fields: ['需求分析', '架构设计', '功能实现', '测试验证'],
    flow_hint: '选题 → 需求分析 → 架构设计 → 功能实现 → 测试验证',
  },
  '理论推导/数理建模': {
    steps: DESIGN_STEPS,
    citations_required: false,
    design_label: '理论模型',
    design_fields: ['基本假设与公理', '推导框架与符号', '关键推导与证明', '机制与结论'],
    flow_hint: '选题 → 基本假设与公理 → 理论推导/证明 → 机制分析',
  },
  '文献综述': {
    steps: ['topic', 'resources', 'outline', 'citation', 'generate', 'export'],
    structure_from_literature: true,
    citations_required: false,
    flow_hint: '选题 → 上传文献 → 聚类生成大纲 → 引用映射 → 生成',
  },
  '课程论文/小论文': {
    steps: ['topic', 'outline', 'resources', 'citation', 'generate', 'export'],
    citations_required: false,
    flow_hint: '选题 → 快速生成大纲 → 文献匹配 → 生成',
  },
  '技术/工程报告': {
    steps: DESIGN_STEPS,
    citations_required: false,
    design_label: '技术方案',
    design_fields: ['项目背景与目标', '技术选型与架构', '关键参数', '实测结果'],
    flow_hint: '选题 → 输入工程方案/实验参数 → 生成报告',
  },
}

// 有后端后台任务、需要轮询进度的任务类型。选题推荐只在本地计时（它是一次
// 同步请求），所以不在其中 —— 否则轮询会去读一个并不存在的服务端任务。
//
// v1.29 起引用调度也在这里。它同样是同步请求（**发起那一侧**靠本地判据渲染进度条，
// 见 scheduleCitations 里 scheduleInFlight 的注释），但它会把进度写进槽
// （后端 projects._write_task，kind 就是这个 'citations'），所以**另一侧** —— 另一个
// 标签页、切走再切回、或状态已是 citation_pending 时刷新 —— 靠 loadProject 从槽里
// 种入初值、再由这条轮询一路走到终态。少了它，那一侧会渲染出一条**永远在转**的进度条：
// 没有任何东西会去发现那次调度早已结束。
const POLLED_TASK_KINDS = new Set([
  'outline', 'documents', 'material_analysis', 'design_extract', 'clusters', 'citations',
])
const TASK_POLL_MS = 1200
// 轮询**连续失败**时的退避上限：失败一次就慢一倍，最多这么慢。
// 为什么不是「失败几次就放弃」：这里的进度条是用户唯一的反馈，放弃等于把一个还要
// 跑几分钟的任务变成「永远转圈、按钮永远禁用」，自救手段只剩刷新页面。退避到 30 秒
// 已经不会给服务造成任何压力，而服务一旦回来它自己就恢复了。
const TASK_POLL_MAX_MS = 30000

// 未知类型（含已下线的「毕业论文」）退化为课程论文 —— 与后端 config_for 的兜底一致。
function configFor(paperType, typeConfig) {
  const table = typeConfig && Object.keys(typeConfig).length
    ? typeConfig : FALLBACK_TYPE_CONFIG
  return table[paperType] || table[DEFAULT_TYPE_KEY] || FALLBACK_TYPE_CONFIG[DEFAULT_TYPE_KEY]
}

function buildSteps(paperType, typeConfig) {
  const cfg = configFor(paperType, typeConfig)
  return cfg.steps.map((key) => ({
    key,
    // 设计步用类型专属的名字（实证设计 / 质性设计 / 工程设计 / 理论模型 / 技术方案）。
    // 七类里有五类带这一步，步骤条上若一律写「研究设计」，就会与卡片标题里的
    // design_label 对不上 —— 五比二之后，这个不一致从边角变成了常态。
    label: key === 'design'
      ? (cfg.design_label || STEP_LABELS.design)
      : STEP_LABELS[key],
  }))
}

// 状态 -> 语义锚点(key)。所有取值都同时存在于两套工序里，因此无论顺序如何，
// steps.findIndex 都能命中，不会出现 -1。
const STATUS_ANCHOR = {
  draft: 'topic',
  topic_set: 'topic',
  outline_pending: 'outline',
  outline_confirmed: 'outline',
  resources_loading: 'resources',
  citation_pending: 'citation',
  citation_confirmed: 'citation',
  generating: 'generate',
  completed: 'generate',
  exported: 'export',
}

// 已「完成」该锚点对应的动作 -> 前进到下一步。
// topic_set 也在其中：选题已确认，再回到「选题」只会让用户多点一次。它同时把
// 「刷新后落回哪一步」按各类型自己的工序算对 —— 实证研究去设计录入、文献综述去
// 文献注入、课程论文去大纲，同一句 advance 覆盖三种顺序。
//
// completed 同理，而且更要紧：正文写完之后「当前位置」本来就是终点，而 STATUS_ANCHOR
// 给它的锚是 generate。不进这个集合的话 frontierIndex 永远差最后一格 —— 步骤条上
// 「预览与导出」会顶着「完成前面的步骤后即可查看」锁住，而它前面的每一步都做完了，
// 那是一条假理由。导出那一步只有两个入口（生成卡片上的「查看导出结果 →」与这一格），
// 锁掉一个就少一条路。
const ADVANCE_AFTER = new Set([
  'topic_set', 'outline_confirmed', 'citation_confirmed', 'completed',
])

// 「这一步已经做过了」的状态。与 STATUS_ANCHOR 回答的问题不同：那个答「现在停在哪」，
// 这个答「走到过这里没有」—— 回看前面某一步时（生成中、生成完）仍然要能认出那里
// 做过什么，否则用户看到的是一句「请先上传文献」，像是自己做过的事被撤销了。
const STEP_DONE_STATUSES = new Set([
  'citation_confirmed', 'generating', 'completed', 'exported',
])

function statusToStepKey(status, steps) {
  const anchor = STATUS_ANCHOR[status] || 'topic'
  const i = steps.findIndex((s) => s.key === anchor)
  if (i < 0) return steps[0]?.key || 'topic'
  if (ADVANCE_AFTER.has(status) && i + 1 < steps.length) return steps[i + 1].key
  return steps[i].key
}

// 材料分析是否正在跑。判定只此一份：ResourceStep 自己要用（禁按钮、渲染进度条），
// 「有没有材料还没分析」也要用它。
function materialsAnalyzing(task) {
  return task?.status === 'running' && task.kind === 'material_analysis'
}

// 有材料还没分析。
//
// 这个判定原先只有 ResourceStep 一个消费点（它自己算，再向下传给 MaterialPanel），
// 现在有**两个**：文献注入步点的是「分析材料如何融入正文」，引用调度步则要说明
// 「材料作废了，回上一步重做」—— 用户完全可能已经越过那一步了。同一条判定不留两份
// 实现（与 MaterialPanel 里 analyzePromoted 的注释同一条道理）。
//
// 提到 App 里还有个**签名上的理由**：引用调度步收不到 task（见 renderStep 的 citation
// case），留在 ResourceStep 里它就只能靠一份过期副本来判断。
// 这条签名上的理由**v1.29 起不成立**（引用调度步从此也收 task，它要渲染自己那条进度
// 条），但提到 App 里这件事本身照旧 —— 上面那条"两个消费点、只留一份实现"才是主理由，
// 一个理由没了不代表结论要跟着退回去。
//
// 文献综述不渲染材料面板（它的主体就是文献本身），所以必须排除 isReview：否则引用
// 调度步会提示用户去做一件界面上根本不存在的操作。analyzing 也要排除 —— 分析正在跑
// 的时候计划当然是空的，那不是「没分析」，那是「正在算」。
function materialsPendingOf({ isReview, materials, materialsPlan, task }) {
  return !isReview && materials.length > 0 && !materialsPlan && !materialsAnalyzing(task)
}

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

// ==================== 各阶段组件 ====================

// 就地改名：默认渲染成文本 + ✎，点文本本身或 ✎ 进入输入态，回车保存、失焦保存、
// Esc 取消。清空输入 = 提交空串 = 回到「跟随研究核心方向」（后端的跟随条件就是
// title 为空 —— 清空是这条路的回车键，不是「把名字删掉」）。
//
// 两个调用点都在**可点击的容器**里（侧栏卡片整块是「打开项目」，页头 h1 也在
// 顶栏），所以这里自己把 click / mousedown / keydown 一律 stopPropagation：
// 否则点一下 ✎ 会顺带把项目打开、或者输入时触发外层快捷键。
function EditableTitle({ value, topic, onRename, onEditingChange, className = '' }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  // Esc 取消后会再收到一次 blur —— 不跳过它，「取消」会立刻变成一次保存
  const skipBlur = useRef(false)

  function open() {
    setDraft(value)
    setEditing(true)
    if (onEditingChange) onEditingChange(true)
  }

  function close() {
    setEditing(false)
    if (onEditingChange) onEditingChange(false)
  }

  async function save() {
    if (skipBlur.current) {
      skipBlur.current = false
      return
    }
    const next = draft.trim()
    close()
    // 没改就不发请求：改名会写 updated_at，而项目列表按它倒序排 —— 一次空改会把
    // 这个项目顶到侧栏最前面，用户却什么都没改
    if (next === value.trim()) return
    await onRename(next)
  }

  if (!editing) {
    return (
      <span className={`title-edit ${className}`}>
        <span
          className="title-edit-text"
          title="点击改名"
          onClick={(e) => { e.stopPropagation(); open() }}
        >
          {value}
        </span>
        <button
          type="button"
          className="title-edit-btn"
          title="改名（留空则跟随研究核心方向）"
          onClick={(e) => { e.stopPropagation(); open() }}
        >
          ✎
        </button>
      </span>
    )
  }

  return (
    <input
      // 文本态与输入态带同一个 className：宽度约束（页头限宽、卡片占满）在两种
      // 状态下都要成立，否则一进输入态宽度就跳
      className={`title-input ${className}`}
      value={draft}
      autoFocus
      placeholder={topic || '留空则跟随研究核心方向'}
      onChange={(e) => setDraft(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onBlur={save}
      onKeyDown={(e) => {
        e.stopPropagation()
        if (e.key === 'Enter') save()
        else if (e.key === 'Escape') { skipBlur.current = true; close() }
      }}
    />
  )
}

function TopicStep(props) {
  // 「目标总字数」这个框此前是**直接绑在数字状态上**的受控输入：
  //   value={props.targetWords} onChange={(e) => props.setTargetWords(parseInt(e.target.value) || 0)}
  // parseInt('') 是 NaN，`|| 0` 又把它落成 0 —— 于是框里永远至少挂着一个 "0"。
  // 用户全选删掉，框里立刻回到 "0"、光标停在它旁边：接着敲的数字要么接在 0 后面
  // （框里显示 "03000"），要么插在 0 前面变成 "30000" —— 后者是个**静默的错值**，
  // 目标字数从 3000 变成 3 万，用户看不出来。想改这个数就只有先删、再想办法把光标
  // 挪回去这一条别扭的路。
  //
  // 修法是把「用户正在敲的那串字符」与「库里那个数字」各归各位：框里放草稿文本，
  // 数字仍由父层持有。**不能顺手把父层的 targetWords 改成字符串** —— 大纲步那句
  // `total === props.targetWords` 会因此恒假（数字永远不等于字符串），确认大纲的
  // 按钮就此永久禁用，而界面上不会有任何报错。草稿留在这一层即可：它是纯粹的
  // 输入中间态，父层那个数字才是唯一真相。
  const [wordsDraft, setWordsDraft] = useState(
    props.targetWords ? String(props.targetWords) : '',
  )
  // 回灌只在「两边归一到不同的数字」时发生，正常是「切换项目」那一路（8000 → 3000）。
  // 判据必须与 onChange 用同一个 wordsNumber，否则 "1000.5" 这类中间态会被判成不同、
  // 在用户手底下把草稿改掉；而清空时草稿是 ''、父层是 0，两边都归一到 0 —— 于是
  // 刚被删掉的 "0" 不会被塞回来，那次回灌正是原来那个毛病本身。
  useEffect(() => {
    if (wordsNumber(props.targetWords) !== wordsNumber(wordsDraft)) {
      setWordsDraft(props.targetWords ? String(props.targetWords) : '')
    }
  }, [props.targetWords])
  // 按过按钮之后的拒绝语，就显示在按钮正上方。挂在这一层而不是走 App 级那条
  // error-banner：那条横幅在**步骤卡片之上**，而这张卡片很高（选题列表 + 两个大
  // 文本框），用户按的按钮在卡片最下面、横幅多半在视野之外 —— 那就成了「按了没反应」，
  // 正是这个项目一路在消灭的那类静默。生成步的 `failed && <div className="error-banner">`
  // 就是这个位置、这个写法。
  const [wordsRefused, setWordsRefused] = useState(false)
  const wordsTooFew = wordsNumber(props.targetWords) < props.minWords

  function submitTopic() {
    // 这道检查的职责是**把话说在按钮旁边**，不是闸门：真正的闸门是后端 set_topic 里
    // 那道同值的 400（前端这份若过期，用户也只是多等一次往返、消息落到顶部横幅里）。
    // 所以这里**不**复制一份阈值，只读下发下来的 props.minWords。
    if (wordsTooFew) {
      setWordsRefused(true)
      return
    }
    setWordsRefused(false)
    props.onConfirm()
  }

  return (
    <div className="card">
      <h2>{props.stepNo}. 选题与设定</h2>
      <div className="form-grid">
        <div className="field">
          <label>论文类型</label>
          {/* value 是短名（库里存的那一个），显示的是带适用范围的长名 ——
              用户靠括号里的「社科、经管、医学」判断该选哪个，靠短名落库。 */}
          <select value={props.paperType} onChange={(e) => props.setPaperType(e.target.value)}>
            {props.paperTypes.map((t) => (
              <option key={t} value={t}>{props.typeLabels[t] || t}</option>
            ))}
          </select>
          <p className="muted">
            大纲结构随类型变化。
            {props.typeConfig[props.paperType]?.flow_hint
              ? `本类型的写作主线：${props.typeConfig[props.paperType].flow_hint}。`
              : ''}
          </p>
        </div>
        <div className="field">
          <label>写作语言</label>
          {/* 它决定产出物用哪种语言（论文标题/摘要/正文/文末列表），界面文字仍是中文。
              选项由 meta 下发的表派生，与后端 writing_lang.SUPPORTED_LANGS 同源。 */}
          <select
            value={props.writingLang}
            onChange={(e) => props.setWritingLang(e.target.value)}
          >
            {props.writingLangs.map((l) => (
              <option key={l} value={l}>{props.langLabels[l] || l}</option>
            ))}
          </select>
          {/* 「能选哪些」与「排除掉的为什么不在」两句都必须由**同一个数据源**现算：
              前者是 formats（= 后端 citation_formats_by_lang[该语言]），后者是
              allFormats 减掉 formats、再逐个取后端下发的 formatNotes。原先后半句是前端
              手写的一句 `writingLang === 'en' ? 'GB/T 7714 是中文期刊的著录标准…'`——
              同一个事实判了两遍，而且手写的那句在后端改表之后就会变成假话，
              看起来却完全正常。 */}
          <p className="muted">
            论文标题、摘要、关键词、正文与文末参考文献列表按它撰写（界面文字仍是中文）。
            参考文献格式可选：
            {props.formats.map((f) => props.formatLabels[f] || f).join('、')}。
            {props.allFormats
              .filter((f) => !props.formats.includes(f))
              .map((f) => props.formatNotes[f])
              .filter(Boolean)
              .join('')}
          </p>
        </div>
        <div className="field">
          <label>目标总字数</label>
          <input
            type="number"
            min={props.minWords}
            value={wordsDraft}
            onChange={(e) => {
              const raw = e.target.value
              // 草稿存原样的字符（含空串 —— 这正是要修的那件事），数字仍交给父层。
              setWordsDraft(raw)
              props.setTargetWords(wordsNumber(raw))
              // 改过了就把上一句拒绝收回去：它说的是改之前那个值，挂在按钮上方会
              // 让用户以为改完还是不行。仍不够就再按一次按钮，话会照原样再说一遍。
              setWordsRefused(false)
            }}
          />
          {/* 单位不是文案问题：英文论文的字数口径是**词**，后端 count_units 也按词数。
              不显示的后果很具体 —— 用户给英文论文填「5000 字」，产出的是 5000 个词
              （约等于中文七千多字的体量），他拿到手才发现差了两倍。 */}
          <p className="muted">
            最少 {props.minWords} {props.wordsUnit}，少于这个数不能进入下一步。
            它是全篇总量，到「大纲与字数」步按节分配。
          </p>
        </div>
        <div className="field field-full">
          <label>研究领域（用于选题推荐）</label>
          <input
            placeholder="例如：人工智能、气候变化、教育公平..."
            value={props.domain}
            onChange={(e) => props.setDomain(e.target.value)}
          />
        </div>
      </div>

      <div className="actions">
        {/* 有服务端任务在跑时禁掉：推荐把进度写进同一个 task 槽位，跑完还会
            setTask(null)，会把正在轮询的服务端任务状态一起清掉（进度条凭空消失）。
            字数不足时也禁掉：这个框就是「输入先校验再消费」——推荐的 match 评分
            是拿目标字数当基准评的，把一个刚被提示「不许用」的数字喂进去，只会
            换回一份按 0 字评的推荐，而界面上看不出来。两个理由都要说给用户听，
            所以 title 按顺序取第一条成立的。 */}
        <button
          className="btn"
          onClick={props.onRecommend}
          disabled={props.loading || !props.domain || props.taskRunning || wordsTooFew}
          title={
            props.taskRunning ? '有任务正在进行，等它结束后再试'
              : wordsTooFew ? `先把目标总字数填到 ${props.minWords} ${props.wordsUnit} 以上：推荐的匹配度是拿这个字数评的`
                : undefined
          }
        >
          AI 推荐选题
        </button>
        {props.task?.status === 'running' && props.task.kind === 'topics' && (
          <WorkingBar
            message={props.task.message}
            startedAt={props.task.started_at}
          />
        )}
      </div>

      {props.topics.length > 0 && (
        <div className="topic-list">
          {props.topics.map((t, i) => (
            <div
              key={i}
              className={`topic-card ${props.topic === t.title ? 'selected' : ''}`}
              onClick={() => props.setTopic(t.title)}
            >
              <div className="topic-title">{t.title}</div>
              <div className="muted">研究问题：{t.question}</div>
              <div className="muted">可行性：{t.feasibility}</div>
              {/* 匹配度是模型自己给的，可能没有；兜底选题（未配置 LLM）也不给 ——
                  它没跟任何东西比对过。有才显示，否则会印出「匹配度 undefined」。 */}
              {t.match != null && <span className="tag">匹配度 {t.match}</span>}
            </div>
          ))}
        </div>
      )}

      <div className="field field-full">
        <label>确定研究核心方向</label>
        <textarea
          rows={3}
          placeholder="可手动填写或从上方推荐选题中选择"
          value={props.topic}
          onChange={(e) => props.setTopic(e.target.value)}
        />
      </div>

      {/* 这一步此前只有一个标题输入框，而一个标题装不下「打算怎么论证」。这个框喂两处：
          大纲第一遍（模型据此收敛 core_question，而不是自己替作者定主线）与每一节正文。
          上面那句「确定研究核心方向」是**标题**，模型只能从它反推主线；下面这段是作者的
          原话，两者冲突时以它为准（优先级写在后端块里，不在这里）。
          必须与「研究材料」区分开：材料在结构上**永远进不了大纲**（材料分析的前置条件
          就是大纲已存在，它只经 materials_plan_json 进正文），而粘贴材料的默认标签恰好
          就叫「粘贴的研究思路」——用户把论证思路写进那里，大纲会无声地忽略它。 */}
      <div className="field field-full">
        <label>写作思路与论证思路（选填）</label>
        <textarea
          rows={4}
          placeholder="例：先辨析 A 与 B 两个概念，再从机制层面论证二者是互补而非替代，最后用近五年的行业数据说明这种互补在什么条件下失效"
          value={props.writingIdeas}
          onChange={(e) => props.setWritingIdeas(e.target.value)}
        />
        <p className="muted">
          写你打算怎么论证，不是论文正文。它会进大纲，也会进每一节的正文；
          与模型自己的推断有出入时以你写的为准。
          想交数据、成果或成段的素材，请到「文献注入」步用「研究材料」——
          那一类只进正文，不会影响大纲。
        </p>
      </div>

      {/* 这个 busy 与上面「AI 推荐选题」的 taskRunning 是两把尺子，刻意不合并：
          那个管的是**本地进度槽位**（推荐把进度写进同一个 task 槽、跑完 setTask(null)，
          会把正在轮询的服务端任务状态一起清掉），这个管的是后端那两道忙闲闸 ——
          set_topic 在任一登记在跑时都 409（「改了也白改」：在飞的任务收尾时会把产物
          写回库，盖掉这次作废）。前者是后者的子集，但理由不同，各留各的。 */}
      {wordsRefused && (
        <div className="error-banner">
          目标总字数最少 {props.minWords} {props.wordsUnit}，当前
          {' '}{wordsNumber(props.targetWords)} {props.wordsUnit}。
          请先在上面的「目标总字数」里补足再进入下一步。
        </div>
      )}
      <div className="actions">
        {/* 字数不足时**不禁用**这个按钮，而是让它按下去把原因说出来：禁用一个主按钮
            等于什么都不说，用户只会看到一个点不动的按钮（这道工序里已经因为「按钮
            该亮不亮、该说不说」修过一轮）。所以判据只体现在 onClick 与文案上。 */}
        <button
          className="btn btn-primary"
          onClick={submitTopic}
          disabled={props.loading || !props.topic || props.busy}
        >
          确认选题，进入下一步
        </button>
      </div>
    </div>
  )
}

// 文献综述的「主题聚类」面板。放在大纲步内、生成按钮之前 —— 聚类本就是大纲的
// 上游产物，把它做成独立步骤会牵动 STATUS_ANCHOR / statusToStepKey 的锚点体系，
// 而它并不对应任何项目状态。
//
// **作者能改的只有簇标题**：文献归属（doc_ids）只读，因为它由模型按全文判断，
// 手改一个 id 只会让「这篇文献讲什么」与簇标题真的对不上，而这种错配在大纲里
// 看不出来——章节照样生成，只是主题是错的。
function ClusterPanel(props) {
  const data = props.clusters
  const stored = data?.clusters || []
  const confirmed = !!data?.confirmed_at
  const [draft, setDraft] = useState(stored)

  // 服务端版本变了（重新生成 / 刷新页面 / 删文献后作废）就重置草稿：
  // 草稿是「本次编辑中」的临时态，不是第二份事实来源。
  useEffect(() => { setDraft(stored) }, [data])

  // running 只用来渲染进度条（它答的是「正在做的这件事是不是聚类」）；按钮禁不禁由
  // props.busy 说了算（它答的是「后端现在收不收这一笔」）—— 聚类那两个入口在后端都
  // 只查 _busy_task，所以别的任务在跑时它们同样会被 409。
  const running = props.task?.status === 'running' && props.task.kind === 'clusters'
  const noDocs = props.documentCount === 0
  const dirty = JSON.stringify(draft) !== JSON.stringify(stored)
  const unassigned = data?.unassigned || []

  // 簇数与「研究主题脉络」章的节数未必相等：多了会被合并、少了会被拆分，
  // 两种都出得来大纲，但作者该先知道结果会怎样。
  const sections = props.themeSections
  let mismatch = ''
  if (sections > 0 && draft.length && draft.length !== sections) {
    mismatch = draft.length > sections
      ? `你确认了 ${draft.length} 个主题，而「研究主题脉络」章有 ${sections} 节，` +
        `多出的主题会被合并进主题相近的节。`
      : `「研究主题脉络」章有 ${sections} 节，而你确认了 ${draft.length} 个主题，` +
        `该章会按这些主题重新拆分与命名。`
  }

  function setTitle(i, value) {
    setDraft((cur) => cur.map((c, j) => (j === i ? { ...c, title: value } : c)))
  }

  return (
    <div className="guide-box cluster-panel">
      <h3>主题聚类（研究主题脉络章的依据）</h3>
      {noDocs ? (
        <p className="muted">
          还没有文献。文献综述的章节主题来自文献，请先回到「文献注入」步上传文献。
        </p>
      ) : (
        <>
          <p className="muted">
            先把 {props.documentCount} 篇文献归纳成若干研究主题，确认后据它组织
            「研究主题脉络」章的章节。也可以跳过这一步直接生成大纲，由模型自行归纳。
          </p>
          <div className="actions">
            <button className="btn" onClick={props.onGenerateClusters} disabled={props.loading || props.busy}>
              {stored.length ? '重新生成主题聚类' : '生成主题聚类'}
            </button>
            {confirmed && !dirty && <span className="tag tag-success">已确认</span>}
            {confirmed && dirty && <span className="tag">改动未确认</span>}
            {running && (
              <WorkingBar message={props.task.message} startedAt={props.task.started_at} />
            )}
          </div>

          {draft.length > 0 && (
            <>
              <div className="cluster-list">
                {draft.map((c, i) => (
                  <div key={c.id || i} className="cluster-item">
                    <input
                      className="cluster-title-input"
                      value={c.title || ''}
                      maxLength={60}
                      onChange={(e) => setTitle(i, e.target.value)}
                    />
                    <div className="muted">
                      {c.doc_titles?.length
                        ? `含文献：${c.doc_titles.join('、')}`
                        : '未归属任何文献'}
                    </div>
                    {c.summary && <div className="muted cluster-summary">{c.summary}</div>}
                  </div>
                ))}
              </div>
              {mismatch && <p className="muted">{mismatch}</p>}
              {unassigned.length > 0 && (
                <p className="muted">
                  未归入任何主题的文献 {unassigned.length} 篇：
                  {unassigned.map((u) => u.title).join('、')}
                  （{unassigned[0].reason}）
                </p>
              )}
              {data?.notes && <p className="muted">{data.notes}</p>}
              <div className="actions">
                {/* 蓝不蓝由**父层那一处**判据给（`confirmClustersPrimary`，定义见 OutlineStep）：
                    这张卡片下面还有「确认大纲（人工确认点①）」，两者同时是蓝的，用户没法从
                    界面判断先后 —— 而这两个动作的后果差一个量级（聚类确认只是盖章，非破坏性；
                    大纲确认会作废引用绑定与材料计划、并把流程推进一步）。 */}
                <button
                  className={props.confirmClustersPrimary ? 'btn btn-primary' : 'btn'}
                  onClick={() => props.onConfirmClusters(draft)}
                  disabled={props.loading || props.busy || !draft.length}
                >
                  {confirmed ? '✓ 更新确认的主题聚类' : '✓ 确认主题聚类'}
                </button>
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}

function OutlineStep(props) {
  const total = outlineTotal(props.outline)
  const matched = total === props.targetWords
  // outlineRunning 只喂进度条（它答「正在跑的是不是大纲」）。
  // 两个按钮禁不禁由 props.busy 说了算：后端 /outline/generate 查的是两套忙闲闸、
  // /outline/confirm 走 _require_outline_confirmable（同一组闸 + 状态白名单），两者
  // 都不止「大纲在跑」这一种情形。**两个按钮都必须禁**：生成转圈时点「确认大纲」，
  // 盖的章会在几秒后被 _run_outline 的收尾写掉（它无条件写 outline_json +
  // outline_pending），连带这次确认顺带作废的引用绑定与材料计划一起白作废一次。
  // 服务端各有一道闸（不只是靠这个禁用），这里是不让用户白点。
  const outlineRunning =
    props.task?.status === 'running' && props.task.kind === 'outline'

  // 这张卡片上并排着**前后两个阶段的出口**（文献综述还多出「确认主题聚类」），而第 22 条
  // 决策定的约定是「任何时刻卡片里恰好一个主按钮，且它一定是**你现在该点的那一个**」。
  // 聚类的理由（作者还没确认过主题）与大纲的理由（大纲已生成、可以盖章了）各自都成立，
  // 所以不能各判各的 —— 按流程前沿排，由**一处**判据分成互斥的三个布尔：
  //   ① 聚类已生成但还没确认 → 「确认主题聚类」是前沿
  //   ② 已确认但大纲还没生成 → 「生成大纲」是前沿
  //   ③ 其余 → 「确认大纲（人工确认点①）」是前沿
  // 此前①③同时是蓝的（聚类那个确认按钮无条件写死了主按钮样式），综述的卡片上并排两个
  // 主按钮，而这两个动作的后果差一个量级：聚类确认只是盖章（非破坏性，已存在的大纲只
  // 回 `outline_stale`），大纲确认会作废引用绑定与材料计划、并把流程推进一步。
  //
  // 判据只看**服务端事实**（clusters_json / outline_json），不看聚类草稿 —— 与材料步、
  // 引用步同一条口径（它们也只读落库的东西）。代价是一处如实记档的边界：「已确认、又
  // 手改了簇标题但没点确认」时蓝按钮留在「确认大纲」上 —— 那次编辑本来就没提交，旁边
  // 的「改动未确认」标签已经说了这件事，而且这里没有任何一个动作会把它悄悄吃掉。
  // `props.documentCount > 0` 那一条照着 ClusterPanel 的渲染条件抄（没有文献时它整段
  // 换成一句说明，确认按钮根本不渲染）—— 判它为主与它渲染得出来必须同源。
  const clustersNeedConfirm =
    props.isReview && props.documentCount > 0 &&
    (props.clusters?.clusters?.length || 0) > 0 &&
    !props.clusters?.confirmed_at
  const generatePrimary = !clustersNeedConfirm && !props.outline
  const confirmPrimary = !clustersNeedConfirm && !!props.outline

  return (
    <div className="card">
      <h2>{props.stepNo}. 大纲生成与字数分配</h2>
      {props.isReview && (
        <div className="guide-box">
          <p className="muted">
            文献综述的大纲由已上传的 {props.documentCount} 篇文献提炼主题脉络而成，
            因此必须先完成文献注入。章节结构为综述式（核心概念界定 / 研究主题脉络 / 研究述评），
            不含研究设计与假设检验。
          </p>
        </div>
      )}
      {props.isReview && (
        <ClusterPanel {...props} confirmClustersPrimary={clustersNeedConfirm} />
      )}
      {props.hasDesign && (
        <div className="guide-box">
          <p className="muted">
            {props.designFilled
              ? `已填写的${props.designLabel}会作为已知条件交给模型，分析/结果类章节只能依据你给的方法与数据展开，不会凭空编造。`
              : `尚未填写${props.designLabel}：分析/结果类章节里的变量、数据与结论都可能是模型编的，建议先回上一步填写。`}
          </p>
        </div>
      )}
      <div className="actions">
        {/* 它是主按钮还是次级，与两个确认按钮同源（见上面的 clustersNeedConfirm）：
            聚类待确认时它让位给「确认主题聚类」（大纲本来就该按作者确认过的主题组织），
            大纲还没生成时它就是你现在该点的那一个。 */}
        <button
          className={generatePrimary ? 'btn btn-primary' : 'btn'}
          onClick={props.onGenerate}
          disabled={props.loading || props.busy}
        >
          生成大纲
        </button>
        {outlineRunning && (
          <WorkingBar
            message={props.task.message}
            startedAt={props.task.started_at}
          />
        )}
      </div>

      {props.outline && (
        <>
          {/* 把主线摆在最前面：割裂一眼就能看见，用户不必自己反推全篇的主题 */}
          {props.outline.core_question && (
            <div className="core-question">
              <strong>核心研究问题（全篇围绕它展开）</strong>
              {props.outline.core_question}
            </div>
          )}

          <div className="budget-bar">
            <span>字数预算总计：<strong>{total}</strong> / 目标 {props.targetWords}</span>
            <span className={matched ? 'tag tag-success' : 'tag'}>{matched ? '已匹配' : '未匹配'}</span>
          </div>

          <div className="outline-tree">
            {props.outline.chapters.map((ch, ci) => (
              <div key={ci} className="chapter">
                <div className="chapter-title">{ch.title}</div>
                {ch.sections.map((sec, si) => (
                  <div key={si} className="section">
                    {sec.subsections && sec.subsections.length > 0 ? (
                      <>
                        <div className="section-row">
                          <span>{sec.title}</span>
                          <span className="muted">按子节分配</span>
                        </div>
                        {sec.subsections.map((sub, bi) => (
                          <div key={bi}>
                            <div className="section-row" style={{ paddingLeft: 24 }}>
                              <span>{sub.title}</span>
                              {/* 字数预算删空 = 未分配，不得归零；0 本身是合法值，故用 ?? 而非 || */}
                              <input
                                type="number"
                                className="budget-input"
                                value={sub.word_budget ?? ''}
                                onChange={(e) => updateSubBudget(props, ci, si, bi, e.target.value === '' ? undefined : wordsNumber(e.target.value))}
                              />
                              {/* 单位跟着写作语言（中文「字」/ 英文「words」）。预算数字
                                  进的是后端 count_units 的口径，与它同一份判据。 */}
                              <span className="muted">{props.wordsUnit}</span>
                            </div>
                            <Reason text={sub.rationale} indent={24} />
                          </div>
                        ))}
                      </>
                    ) : (
                      <>
                        <div className="section-row">
                          <span>{sec.title}</span>
                          {/* 同子节：删空 = 未分配，不得归零 */}
                          <input
                            type="number"
                            className="budget-input"
                            value={sec.word_budget ?? ''}
                            onChange={(e) => updateBudget(props, ci, si, e.target.value === '' ? undefined : wordsNumber(e.target.value))}
                          />
                          <span className="muted">{props.wordsUnit}</span>
                        </div>
                        <Reason text={sec.rationale} />
                      </>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </div>

          <div className="actions">
            <button className={confirmPrimary ? 'btn btn-primary' : 'btn'} onClick={props.onConfirm} disabled={props.loading || props.busy || !matched}>
              ✓ 确认大纲（人工确认点①）
            </button>
          </div>
          <p className="muted">
            提示：可手动调整各节字数预算{props.isReview ? '，确认后进入引用调度。' : '，确认后进入文献注入。'}
            {' '}若改动章节标题，原有引用绑定会失效，需重新执行引用调度。
          </p>
        </>
      )}
    </div>
  )
}

// 每节字数的分配依据。模型按「这一节要覆盖多少文献、论点有多复杂」推算，
// 把依据摆出来既便于判断分配是否合理，也让「凑字数」无处可藏。
function Reason({ text, indent = 0 }) {
  if (!text) return null
  return (
    <div className="budget-reason" style={indent ? { paddingLeft: indent } : undefined}>
      {text}
    </div>
  )
}

// 字数预算只落在最细粒度节点：有子节时累计子节，否则累计节本身。
// 需与后端 routers/projects.py 的 _sum_budget 保持一致。
function outlineTotal(outline) {
  if (!outline) return 0
  let total = 0
  for (const ch of outline.chapters || []) {
    for (const sec of ch.sections || []) {
      if (sec.subsections && sec.subsections.length > 0) {
        for (const sub of sec.subsections) total += sub.word_budget || 0
      } else {
        total += sec.word_budget || 0
      }
    }
  }
  return total
}

function cloneOutline(outline) {
  return JSON.parse(JSON.stringify(outline))
}

function updateBudget(props, ci, si, val) {
  const next = cloneOutline(props.outline)
  const sec = next.chapters[ci].sections[si]
  sec.word_budget = val
  clearReason(sec)
  props.setOutline(next)
}

function updateSubBudget(props, ci, si, bi, val) {
  const next = cloneOutline(props.outline)
  const sub = next.chapters[ci].sections[si].subsections[bi]
  sub.word_budget = val
  clearReason(sub)
  props.setOutline(next)
}

// 依据是模型为某个具体数字给出的论证，数字被手动改掉之后那句话就不再成立，
// 留着它等于在界面上摆一句假话。
function clearReason(node) {
  delete node.rationale
}

// 研究设计 / 技术方案录入（实证研究、技术报告）
//
// 为什么排在大纲之前：这一类论文的核心章节写的正是作者本人给出的一手内容 ——
// 实证的数据与结果、质性的访谈与编码、工程的需求与实现、数理的假设与推导。
// 不先把这些交给模型，它只能编——编变量、编显著性、编结论，而这份虚构在确认点①上
// 看起来完全合理，且会一路带进正文。字段名随类型而定，渲染逻辑完全通用。
function DesignStep(props) {
  const extracting =
    props.task?.status === 'running' && props.task.kind === 'design_extract'
  const filled = props.fields.filter((f) => (props.design[f] || '').trim()).length

  return (
    <div className="card">
      <h2>{props.stepNo}. {props.designLabel}录入</h2>
      <p className="muted">
        这里填的是<strong>你自己已经定下的东西</strong>：研究对象与资料、假设与模型、
        需求与实现、参数与实测结果。它们会作为已知条件交给大纲与正文生成——
        不填的话，核心章节里的内容都只能是模型编的。
      </p>

      <div className="guide-box">
        <h3>怎么填最省事</h3>
        <p className="muted">
          把回归结果表、实验记录、技术方案文档直接拖上来（支持 PDF / Word / Excel /
          txt 等），点「让 AI 自动填写」即可由模型提炼成字段，提炼完还可以自己改。
          这些附件同时会留在项目里，后文「资源注入」步可让模型分析它们该写进哪一节。
        </p>
      </div>

      <div className="actions">
        <label className="upload-zone">
          <input
            type="file" multiple
            onChange={props.onUpload}
            disabled={props.loading || extracting}
          />
          <span>上传{props.designLabel}附件（支持多选）</span>
        </label>
        {/* 这个按钮用 busy 而不是 extracting：/design/extract 在后端查的是 _busy_task，
            别的任务在跑时它一样会被 409。上面那个上传格反过来 —— /materials/files 在
            后端没有任何忙闲闸（纯本地文本提取、同步返回），所以那边只按 extracting 禁。 */}
        <button
          className="btn"
          onClick={props.onExtract}
          disabled={props.loading || props.busy || props.materialCount === 0 || !props.llmConfigured}
          title={
            props.materialCount === 0
              ? '请先上传附件'
              : (!props.llmConfigured ? '未配置 LLM' : '从已上传的附件中提炼字段')
          }
        >
          让 AI 自动填写
        </button>
        {extracting && (
          <WorkingBar
            message={props.task.message}
            startedAt={props.task.started_at}
          />
        )}
      </div>
      <p className="muted">
        已上传 {props.materialCount} 份附件（同时作为研究材料，供后文融入正文）。
      </p>

      <div className="design-form">
        {props.fields.map((name) => (
          <div className="field" key={name}>
            <label>{name}</label>
            <textarea
              rows={3}
              value={props.design[name] || ''}
              placeholder={`${name}（留空表示暂未确定，模型会被要求标出「需作者补充」）`}
              onChange={(e) => props.setDesign({ ...props.design, [name]: e.target.value })}
            />
          </div>
        ))}
      </div>

      {props.notes && (
        <p className="muted">提炼说明：{props.notes}</p>
      )}

      <div className="actions">
        <button className="btn" onClick={props.onSave} disabled={props.loading}>
          保存{props.designLabel}
        </button>
        <button className="btn btn-primary" onClick={props.onNext} disabled={props.loading}>
          进入大纲生成
        </button>
        <span className="muted">
          已填 {filled}/{props.fields.length} 项
        </span>
      </div>
    </div>
  )
}

// 文献元数据编辑框。
//
// **为什么得有它**：元数据全部由一次 LLM 调用抽取，抽错是常态；而此前改正的唯一
// 办法是删掉重传 —— 那条路走不通，同一份 PDF 会被内容指纹判重静默跳过（正文一个字
// 没变）。也就是说抽错的作者其实改不掉，这个框是那条断路的接口。
//
// 提交走 PATCH，只发**改动过的**字段：后端把「字段缺席」解释成「不修改这一项」、
// 把空串解释成「清空」。逐字段比对同时让「什么都没改就点保存」变成一次纯粹的关闭。
function DocumentEditModal(props) {
  const { doc } = props
  const initialForm = () => Object.fromEntries(
    DOC_EDIT_FIELDS.map((f) => [f.key, doc?.[f.key] ?? '']),
  )
  const [form, setForm] = useState(initialForm)
  const [type, setType] = useState(() => initialSourceType(doc))
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')

  function buildPatch() {
    const patch = {}
    for (const f of DOC_EDIT_FIELDS) {
      const before = String(doc?.[f.key] ?? '').trim()
      const after = String(form[f.key] ?? '').trim()
      if (after !== before) patch[f.key] = after
    }
    // 类型只跟**库里的原值**比，不跟收敛后的值比：库里存着 'j' 而下来框显示 'J' 时，
    // 这个差异是真实存在的（一次归一化），该提交；库里存着白名单外的脏值而框里显示
    // 「未设置」时同理 —— 保存即把它清成空，渲染侧本来也按 J 处理，输出不变。
    if (type !== String(doc?.source_type ?? '').trim()) patch.source_type = type
    return patch
  }

  async function save() {
    const patch = buildPatch()
    if (!Object.keys(patch).length) {
      props.onClose()
      return
    }
    setSaving(true)
    setMessage('')
    const res = await props.onSave(doc, patch)
    setSaving(false)
    if (res.ok) props.onClose()
    else setMessage('❌ ' + res.message)
  }

  return (
    <div className="modal-overlay" onClick={props.onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>✏ 编辑文献元数据</h2>
          <button className="btn" onClick={props.onClose}>✕</button>
        </div>

        <div className="modal-body">
          <p className="muted">
            这里填的值会直接进正文角标与文末参考文献列表。
            原文件 <code>{doc.filename}</code> 与它解析出的页码索引不受影响，
            所以改元数据不会让任何一条引用失准。
          </p>

          {DOC_EDIT_FIELDS.map((f) => (
            <div className="field" key={f.key}>
              <label>{f.label}</label>
              <input
                value={form[f.key]}
                placeholder={f.placeholder}
                onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
              />
              {f.hint && <p className="muted">{f.hint}</p>}
            </div>
          ))}

          <div className="field">
            <label>文献类型（题名后方括号里的字母）</label>
            <select value={type} onChange={(e) => setType(e.target.value)}>
              <option value="">未设置（按 J 期刊文章处理）</option>
              {SOURCE_TYPE_LETTERS.map((letter) => (
                <option key={letter} value={letter}>
                  {SOURCE_TYPE_LABELS[letter]
                    ? `${letter} — ${SOURCE_TYPE_LABELS[letter]}`
                    : letter}
                </option>
              ))}
            </select>
            <p className="muted">
              {/* 这一句此前写的是「只影响这一个方括号；非期刊类型还没有各自的完整著录
                  模板（那要出版地、出版者等字段），其余部分与期刊共用同一套兜底形态」
                  —— v1.28 补上 M/D/C/N 的模板之后，前半句就成了假话（出版地 / 版本项 /
                  报纸出版日期三个框就在上面）。改后按本项目口径**并列穷举**：
                  填了专属项 → 走各自的模板；没填 → 与期刊共用兜底形态。两种情形都
                  写出来，并点名是哪几项，不猜、也不含糊成"部分支持"。 */}
              只影响这一个方括号。专著 [M] / 学位论文 [D] / 会议论文 [C] / 报纸 [N]
              在上面填了**它专属的那一项**（出版地 / 版本项 / 报纸出版日期）之后，
              会按各自的完整著录模板著录；那几项留空时与期刊共用同一套兜底形态。
              期刊论文 [J] 没有专属模板，填了那几项也不会被读出来。
            </p>
          </div>

          {message && <div className="config-message">{message}</div>}
        </div>

        <div className="modal-footer">
          <button className="btn" onClick={props.onClose}>取消</button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}


function ResourceStep(props) {
  // 正在编辑的那一篇（null = 没在编辑）。存在这里而不是 App：它是纯粹的界面状态，
  // 关掉就没了，不该让 App 多一个只有本步读的 state。
  const [editingDoc, setEditingDoc] = useState(null)
  const parseRunning =
    props.task?.status === 'running' && props.task.kind === 'documents'
  // 结构由文献决定的类型（文献综述）**不能**一篇不传就走：它的章节主题就是从这里
  // 的文献来的，后端在生成大纲处会 400。这不是「依赖高低」的问题，而是这一步是
  // 该类型的输入源 —— 所以它连「留空跳过」这条退路都没有。
  const needsDocs = props.isReview && props.documents.length === 0
  // 其余类型七类都允许不做引用调度，因此也允许一篇不传直接进入下一步：
  // 它的立身之本是一手内容（技术方案、工程参数、作者自己的资料），
  // 不该被「至少上传一篇文献」挡在生成之外。
  const canSkip = !props.citationsRequired && !needsDocs && props.documents.length === 0
  // analyzing / materialsPending 两条判定都在模块级算好了往下来（见 materialsPendingOf
  // 的注释：它现在有两个消费点，而且引用调度步收不到 task、自己算不了）。

  function handleNext() {
    // 只在真有东西可失去时才打断（与 confirmTopicChange 同一把尺子）：材料为空或
    // 已分析时静默通过。不做成硬闸 —— LLM 没配时分析会走兜底，硬拦会把用户堵死。
    if (props.materialsPending && !window.confirm(
      `有 ${props.materials.length} 份材料还没有分析，继续的话它们不会写进正文。\n\n`
      + '确定继续？',
    )) {
      return
    }
    props.onNext()
  }

  return (
    <div className="card">
      <h2>{props.stepNo}. 下载指南与资源注入</h2>
      <div className="guide-box">
        <h3>文献下载建议清单</h3>
        <p className="muted">
          建议前往 <strong>知网(CNKI)</strong>、<strong>谷歌学术(Google Scholar)</strong> 等平台，
          {props.isReview
            ? '围绕研究主题检索并下载真实文献 PDF，然后批量上传 —— 下一步的大纲将据此生成。'
            : '根据大纲章节关键词下载真实文献 PDF，然后批量上传。文献用于正文引用与文末参考文献列表；'
              + '若本文不需要参考文献，也可以留空直接进入下一步。'}
        </p>
      </div>

      {/* 与材料面板的「研究数据与成果（作者自有材料）」对称的小标题：这一步有两条
          资源线，分界不写出来，用户不知道下面这块和上面那块是两回事 */}
      <h3>参考文献（进正文角标与文末参考文献列表）</h3>

      <div className="actions">
        {/* 上传与删除都按 busy 禁，不是按 parseRunning：后端 /documents 与
            DELETE /documents/{id} 查的是**两套**忙闲闸（正文正在生成时也会 409），
            而 parseRunning 只认得「文献正在解析」这一种 —— 认错的那种情形下按钮是亮的，
            点下去必然 409，失败原因从前还被 api.js 吞掉（现在会如实显示）。 */}
        <label className="upload-zone">
          <input
            type="file" multiple accept=".pdf"
            onChange={props.onUpload}
            disabled={props.loading || props.busy}
          />
          <span>点击上传批量 PDF 文献（支持多选）</span>
        </label>
        {parseRunning && (
          <WorkingBar
            message={props.task.message}
            startedAt={props.task.started_at}
            done={props.task.done}
            total={props.task.total}
          />
        )}
      </div>

      {/* 去重是自动的，但「自动」这件事不写出来，用户会以为重复被静默吞了、
          或者反过来以为靠重传就能补一篇。放在上传动线的正下方，不挪进别处的说明里 */}
      <p className="muted" style={{ margin: '0 0 4px', fontSize: 12 }}>
        文献自动去重：同一篇文献重复上传会自动跳过，并在提示里告知是哪几篇。
      </p>

      {props.documents.length > 0 && (
        <div className="doc-list">
          <h3>已上传文献（{props.documents.length} 篇）</h3>
          {props.documents.map((d) => (
            <div key={d.id} className="doc-item">
              <div className="doc-body">
                <div className="doc-title">{d.title}</div>
                <div className="muted">
                  {!isMissingMeta(d.authors) && `${d.authors} · `}
                  {!isMissingMeta(d.year) && `${d.year} · `}
                  {d.pages?.length || 0} 页
                </div>
                {d.summary && <div className="muted">{d.summary}</div>}
                <div className="muted">{d.filename}</div>
              </div>
              {/* 两个动作竖着排：横排时「编辑 删除」的宽度会把长标题挤成两行。
                  禁用判据与上传/删除同一套（props.loading || props.busy）——
                  后端 PATCH 查的也是那两套忙闲闸，正文正在生成时一样会 409。 */}
              <div className="doc-actions">
                <button
                  className="btn btn-sm"
                  title="编辑这篇文献的题名、作者、卷期页等元数据"
                  disabled={props.loading || props.busy}
                  onClick={() => setEditingDoc(d)}
                >
                  编辑
                </button>
                <button
                  className="btn btn-sm btn-danger"
                  title="删除这篇文献"
                  disabled={props.loading || props.busy}
                  onClick={() => props.onDeleteDoc(d)}
                >
                  删除
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {editingDoc && (
        <DocumentEditModal
          doc={editingDoc}
          onSave={props.onEditDoc}
          onClose={() => setEditingDoc(null)}
        />
      )}

      {/* 文献综述的主体就是文献本身，没有「作者自有数据」这一层，故不显示 */}
      {!props.isReview && (
        <MaterialPanel
          {...props}
          // 这两个是 App 算好、经本步原样转手的（MaterialPanel 也不该自己算）
          analyzing={props.analyzing}
          analyzePromoted={props.materialsPending}
        />
      )}

      {/* 前进按钮在**卡片末尾**（与选题/设计/大纲/引用/生成各步一致）。它原先排在
          材料面板**上方**，于是两条资源线中最该点的那一个反而不在视线尽头 */}
      <div className="actions">
        <button
          className={props.materialsPending ? 'btn' : 'btn btn-primary'}
          onClick={handleNext}
          disabled={
            props.loading || parseRunning || props.analyzing || needsDocs ||
            (props.documents.length === 0 && props.citationsRequired)
          }
        >
          {canSkip ? '不注入文献，进入引用调度' : props.nextLabel}
        </button>
      </div>
      {props.documents.length === 0 && (
        <p className="muted">
          {needsDocs || props.citationsRequired
            ? '至少上传一篇文献后才能继续 —— 本类型的结构建立在这些文献上。'
            : '本文类型可以一篇文献都不传直接继续，跳过后正文不产生角标与参考文献列表。'}
        </p>
      )}
    </div>
  )
}

function MaterialPanel(props) {
  // analyzing / analyzePromoted 都由 App 算好、经 ResourceStep 原样传下来：同一条判定
  // 不留两份实现，而且「谁是主按钮」是**卡片级**的决定（它取决于底部那个前进按钮的
  // 状态），不是材料面板自己能定的
  const { analyzing, analyzePromoted } = props
  const plan = props.materialsPlan
  const placedIds = new Set(
    (plan?.placements || []).flatMap((p) => p.material_ids || []),
  )
  const labelOf = (id) =>
    props.materials.find((m) => m.id === id)?.label || '（已删除）'

  return (
    <div className="material-panel">
      <h3>研究数据与成果（作者自有材料）</h3>
      <p className="muted">
        这里放<strong>你自己的一手材料</strong>：核心论点与写作思路、问卷或实验数据、
        调研结果、访谈记录、图表说明等。系统会让大模型分析这些材料该放进哪一节、
        如何融入，并据此撰写正文。它们<strong>不会</strong>出现在参考文献列表里。
      </p>

      <div className="field">
        <label>直接粘贴一段材料</label>
        <input
          type="text"
          placeholder="给这段材料起个名字，如「研究思路」「访谈要点」"
          value={props.materialDraft.label}
          onChange={(e) =>
            props.setMaterialDraft({ ...props.materialDraft, label: e.target.value })
          }
        />
      </div>
      <textarea
        rows={4}
        placeholder="在此粘贴研究思路、数据说明、结论草稿……"
        value={props.materialDraft.text}
        onChange={(e) =>
          props.setMaterialDraft({ ...props.materialDraft, text: e.target.value })
        }
      />
      <div className="actions">
        <button
          className="btn"
          onClick={props.onAddMaterialText}
          disabled={props.loading || analyzing || !props.materialDraft.text.trim()}
        >
          添加这段材料
        </button>
        <label className="upload-zone upload-zone-sm">
          <input
            type="file" multiple
            accept=".pdf,.docx,.txt,.md,.markdown,.csv,.json,.log,.xlsx"
            onChange={props.onUploadMaterials}
            disabled={props.loading || analyzing}
          />
          <span>或上传文件（PDF / Word / Excel / txt / csv）</span>
        </label>
      </div>

      {props.materials.length > 0 && (
        <div className="doc-list">
          <h4>已添加材料（{props.materials.length} 份）</h4>
          {props.materials.map((m) => (
            <div key={m.id} className="doc-item">
              <div className="doc-body">
                <div className="doc-title">
                  {m.label}
                  {placedIds.has(m.id) && <span className="tag tag-success">已归入章节</span>}
                </div>
                <div className="muted">
                  {m.kind === 'text' ? '粘贴文本' : '上传文件'} · {m.char_count} 字
                </div>
              </div>
              <button
                className="btn btn-sm btn-danger"
                disabled={props.loading || analyzing}
                onClick={() => props.onDeleteMaterial(m)}
              >
                删除
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="actions">
        {/* busy 而不是 analyzing：/materials/analyze 在后端查的是 _busy_task。
            上面那条材料线（添加 / 粘贴 / 上传 / 删除）反过来 —— 那四个入口在后端
            没有任何忙闲闸，所以它们只按 analyzing 禁，不跟着 busy 一起灰。 */}
        <button
          // 有材料还没分析时它是这一步的**主按钮**（见 ResourceStep 的 materialsPending）
          className={analyzePromoted ? 'btn btn-primary' : 'btn'}
          onClick={props.onAnalyzeMaterials}
          disabled={props.loading || props.busy || props.materials.length === 0}
        >
          {plan ? '重新分析材料如何融入正文' : '分析材料如何融入正文'}
        </button>
        {analyzing && (
          <WorkingBar message={props.task.message} startedAt={props.task.started_at} />
        )}
      </div>

      {props.materials.length > 0 && !plan && !analyzing && (
        <p className="muted">
          尚未分析：不点上面的按钮，这些材料<strong>不会</strong>写进正文。
        </p>
      )}

      {plan && (
        <div className="material-plan">
          {plan.notes && <p className="muted">整体思路：{plan.notes}</p>}
          {plan.placements.map((p) => (
            <div key={p.section_title} className="plan-item">
              <div className="plan-section">{p.section_title}</div>
              <div>
                使用材料：
                {p.material_ids.map((id) => (
                  <span key={id} className="tag">{labelOf(id)}</span>
                ))}
              </div>
              {p.usage && <div className="muted">融入方式：{p.usage}</div>}
              {p.key_points?.length > 0 && (
                <div className="muted">必写入正文：{p.key_points.join('；')}</div>
              )}
            </div>
          ))}
          {plan.unused?.length > 0 && (
            <p className="muted">
              未被采用：
              {plan.unused.map((u) => `${u.label}（${u.reason}）`).join('；')}
            </p>
          )}
        </div>
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
function bindingRefs(refs) {
  if (!Array.isArray(refs)) return []
  return refs.filter((r) => r && typeof r === 'object')
}

function CitationStep(props) {
  const noDocs = props.documents.length === 0
  // 已跳过：绑定缺省 + 状态**走到过** citation_confirmed。
  // 原先这里判的是「状态正好等于 citation_confirmed」—— 那只在跳过后的那一瞬间成立：
  // 一旦生成把状态推成 generating/completed，用户再回看本步，这条就变成 false，
  // 那句「已跳过」凭空消失，界面看上去像他从没做过这个决定（连「进入分段生成」的
  // 出口也跟着没了）。状态是**当前位置**，跳过与否是**发生过的事**，两者的判据本来
  // 就该不同（见 STEP_DONE_STATUSES）。
  const skipped = !props.binding && STEP_DONE_STATUSES.has(props.projectStatus)
  // 结构由文献决定的类型（文献综述）没有文献不是「选择」而是退化：它的骨架本就由
  // 文献聚类而来，后端在跳过与生成两处都会 400。这里不给按钮，并说明去哪补。
  const needsDocs = props.isReview && noDocs
  const canSkip = noDocs && !props.citationsRequired && !skipped && !needsDocs
  // 有任务在跑就冻结：三个按钮写的都是同一阶段的状态，后端
  // _require_citation_editable 会以 409 拒绝（两套忙闲登记里的任一在跑、或状态不在
  // 白名单里），前端跟着禁掉，免得用户点了才发现点不动。用 props.busy 而不是
  // props.generating：生成只是那两道闸里的一道，调度正在跑、大纲正在重生成同样会被拒。
  // 下拉也跟着禁 —— 它是「确认」时才落库的本地值，跑着的时候改它只会攒下一个按不下去
  // 的改动。
  const frozen = props.loading || props.busy
  // 本步那条进度条的两个来源，也是本步"我正在等"的**唯一判据**（下面用它决定要不要
  // 渲染进度条、以及要不要说那句"有后台任务在进行"）：
  //
  //   ① 服务端那份：另一个标签页、切走再切回、或状态已是 citation_pending 时刷新 ——
  //      那三种情况的下我们这里没有本地判据，而槽里有（后端 _write_task 写的
  //      kind="citations"），靠 loadProject 种入 + POLLED_TASK_KINDS 里的 citations
  //      轮询到终态。running 的 citations 状态**只可能是真在跑的那一趟**：崩溃残留的
  //      running 由后端启动时 reset_stale_tasks 收成 interrupted。
  //   ② 本地那份：本地点了这一下、回执还没回来。这条路由同步等到一次上限 120 秒的
  //      模型调用结束才返回，所以那一侧没有可轮询的服务端状态（见 scheduleCitations）。
  const serverScheduling = props.task?.status === 'running' && props.task.kind === 'citations'
  const scheduling = props.scheduleInFlight || serverScheduling
  // 绑定落后于文献库（库里有 N 篇不在绑定里）时，**本步该点的是「执行引用调度」**：
  // 用户返回上一步加/删过文献，而绑定还是上一版的，直接确认下去，新加的那几篇在正文
  // 和文末列表里根本不会出现。判据由服务端现算（unbound_documents），前端数不了 ——
  // 那份 documents 列表在解析中途是滞后的，而且它不知道绑定里有什么。
  //
  // 顺带补上一个既有缺口：刚上传完文献、第一次进本步时（binding 假、noDocs 假 ⇒
  // canSkip/skipped 全假），这张卡片**原先一个蓝按钮都没有**，现在这里会亮。
  //
  // `!noDocs` 这一半不是多余的：本步的「执行引用调度」在无文献时是禁用的，而
  // `unbound_documents` 读的是**库里**的行、`documents` 是前端那份列表 —— 两者理论上
  // 可以短暂不一致。少了它，就会出现"提示让你点一个灰按钮"、甚至一个蓝按钮都没有
  // （本卡片的三处蓝按钮全被 schedulePromoted 压成白的）。宁可静默一帧。
  //
  // 与下面材料那半**必须是两个**变量：材料没分析不提升这个按钮 —— 重新调度对材料
  // 一行都不改，点亮它等于指错地方。
  const schedulePromoted = props.unboundDocuments > 0 && !noDocs

  function handleConfirm() {
    // 只在**真有东西会丢**时才打断（与 ResourceStep.handleNext、confirmTopicChange 同一
    // 把尺子）：正常情况下确认绑定是零损失的盖章动作，弹窗会变成用户学会闭眼点掉的东西。
    // 也不做成硬闸：跳过引用、或故意只绑一部分文献，都是用户有权做的选择 —— 告知后果后放行。
    const lost = []
    if (props.unboundDocuments > 0) {
      lost.push(`有 ${props.unboundDocuments} 篇文献不在当前绑定里，正文不会引用它们`)
    }
    if (props.materialsPending) {
      lost.push(`有 ${props.materialsCount} 份材料还没有分析，不会写进正文`)
    }
    if (lost.length && !window.confirm(
      `${lost.join('；')}。\n\n`
      // 承重的一句：照抄材料那句（「继续的话不会写进正文」）在这里是错的 ——
      // 材料是**丢下不管**，绑定是**盖章固化**，两个动作丢的东西不一样。
      + '「确认引用绑定」只是把当前这份绑定盖章，不会把它们补进来。\n\n'
      + '确定继续？',
    )) {
      return
    }
    props.onConfirm()
  }

  return (
    <div className="card">
      <h2>{props.stepNo}. 全局引用调度与配额管理</h2>
      <div className="guide-box">
        <h3>相关性调度（附确定性兜底）</h3>
        <p className="muted">
          系统逐篇判断这 {props.documents.length} 篇文献与各章节主题的相关性，据此把文献绑定到大纲章节，
          并保证无遗漏、无同章重复、每节都有引用，所有引用均可追溯至具体页码。
          判断是一次模型调用（只看文献摘要与章节标题）；模型不可用、超时或输出不可解析时，
          会退回按上传顺序均匀铺开，并在顶部如实说明 —— 无论走哪条路，
          你上传的每一篇文献都会进入正文，系统不会替你判定「这篇不引用」。
          {!props.citationsRequired && !needsDocs &&
            ' 若本文不需要参考文献，也可以跳过（跳过后全文不产生角标与参考文献列表）。'}
        </p>
      </div>

      {needsDocs && (
        <div className="notice-banner">
          本类型论文的结构与论证都建立在文献上，没有文献就没有可调度的引用，
          也不会生成正文。请先回到「文献注入」步上传并解析文献。
        </div>
      )}

      {/* 上一步动过资源、而这一步的东西还是旧的。用 .stale-banner 而不是 .notice-banner：
          它讲的不是一个操作的后果，而是**此刻仍然生效的事实**（下面这两条只在落后时
          出现，重排/重新分析后自己就没了），所以没有关闭按钮。
          措辞刻意**不猜原因**：跳过引用、从没调度过、删掉一篇被绑文献，都会落到这里，
          而「文献有改动」在第一种情况下就是假话 —— 数字自己会说话。
          v1.23 修掉的一处**自相矛盾**：这段注释是对的，可下面那段文案却写着「绑定是在
          上一次调度时定下的，此后加进来的文献不在其中」——跳过引用、从没调度过的项目
          根本没有"上一次调度"，这两句话都是假的，而用户会拿它去理解自己项目的状态。
          现在改成**并列穷举、不指认哪一种**（照下面材料那条的写法），每一句在每一种
          落到这里的情形下都成立；补救动作照旧无条件给出。 */}
      {schedulePromoted && (
        <div className="stale-banner">
          <b>有 {props.unboundDocuments} 篇文献还没有绑定到任何章节。</b>
          {' '}它们不会出现在正文角标和文末参考文献列表里。没调度过、跳过过引用、
          调度之后又增删过文献或改过章节标题，都会落到这里。点下面的「执行引用调度」
          重排一次即可（确定性算法，不到一秒）。
        </div>
      )}

      {/* 材料这一半的补救动作**不在本步**（本卡片没有材料面板），所以必须把目的地和
          按钮名都说出来：「回上一步」对用户不是地址，『文献注入』步的那个按钮名才是。
          也说清重排引用对它无效 —— 否则用户会在这里点「执行引用调度」然后以为修好了。 */}
      {props.materialsPending && (
        <div className="stale-banner">
          <b>有 {props.materialsCount} 份研究材料还没有分析。</b>
          {' '}材料增删、或大纲章节改动，都会让上一次的分析作废（作废的是「哪些材料写进哪一节」
          这份计划，材料本身还在）。分析不是本页能做的事：回到「文献注入」步点一下
          「分析材料如何融入正文」。在这里重排引用对材料没有任何作用。
        </div>
      )}

      {props.generating && (
        <p className="muted">
          正文正在分段生成，期间不能改动引用设置（后端会拒绝），等生成结束后再来。
        </p>
      )}

      {/* 另外那一半 busy：跑的是大纲 / 解析 / 材料 / 提炼 / 聚类里的某一种。
          它们同样让下面所有按钮冻结（后端那两道闸），而本卡片此前说不出到底是哪一种
          —— 所以只说「有后台任务在进行」并指出进度条在哪。
          不写这一句的话，按钮会莫名其妙地灰着而卡片上一个字的解释都没有
          （改前它们反而是亮的，点下去能拿到后端那句确切的 409 文案）。
          `!scheduling` 那一半是 v1.29 补的：引用调度从这一版起也会让 busy 为真，而
          它那条进度条**就在本卡片里**（上面那个 .actions 行）—— 这时再说"它的进度条
          在它自己那一步上"就是当面自相矛盾。此时本卡片已经有那条条在说事了，这半句
          的职责（让灰按钮有个解释）由那条条接管。
          注：「本卡片收不到 task」是这一句当初的理由，v1.29 起不成立（本卡片从此收
          task），但上面这条判断照旧 —— 除了引用调度，别的任务仍然说不出是哪一种。 */}
      {!props.generating && props.busy && !scheduling && (
        <p className="muted">
          有后台任务正在进行，期间不能改动引用设置（后端会拒绝），
          它的进度条在它自己那一步上，等它结束后再来。
        </p>
      )}

      {/* 角标样式、列表格式与「执行引用调度」**不按 skipped 隐藏**。它们是重新调度的
          入口（样式要按「确认」才落库），而「跳过过引用」是**发生过的事**、不是权限：
          一个跳过引用、正文都写完的项目，用户后来改了主意想补上参考文献，路必须还在。
          之前这里写成 !skipped，正好会把这条路一并关掉 —— 状态与历史的混淆在两个方向
          上都咬人：该认出来的没认出来，该留着的出路被顺手封了。 */}
      <div className="field" style={{ maxWidth: 360, marginBottom: 8 }}>
        <label>正文引用角标样式</label>
        <select
          value={props.citeStyle}
          onChange={(e) => props.setCiteStyle(e.target.value)}
          disabled={frozen}
        >
          {/* 选项由后端下发（同下面的格式下拉）。此前这两项是手写的，而
              /citations/confirm 的 400 文案里印的正是**后端那张表**里的名字 ——
              两处靠人抄一致。它还会在加第三种角标样式时静静地漏掉一个新选项。 */}
          {props.citeStyles.map((s) => (
            <option key={s} value={s}>{props.citeStyleLabels[s] || s}</option>
          ))}
        </select>
      </div>

      <div className="field" style={{ maxWidth: 360, marginBottom: 8 }}>
        <label>参考文献列表格式</label>
        {/* 选项**按写作语言派生**，不手写：英文论文里不该出现 GB/T 7714，而手写一份
            清单就是第二份格式白名单 —— 它与后端漂移时，界面上会留着一个后端会 400
            拒掉的选项，且它看起来完全正常。formats / formatLabels 都来自 /api/meta，
            由后端 citation_format 那两张表下发（选项文字与「能选哪些」同一处真相）。
            「（默认）」按 defaultFormat 现判：默认格式随语言变（zh 是 GB/T、en 是 APA），
            写死在标签里就会在另一种语言下印着一个错的默认标记。 */}
        <select
          value={props.citationFormat}
          onChange={(e) => props.setCitationFormat(e.target.value)}
          disabled={frozen}
        >
          {props.formats.map((f) => (
            <option key={f} value={f}>
              {props.formatLabels[f] || f}{f === props.defaultFormat ? '（默认）' : ''}
            </option>
          ))}
        </select>
        {/* 该语言下这个格式的提示（如中文写作选 APA「中文期刊极少使用」）。是提示不是拦：
            不报错、不置灰 —— 三项都留着是用户拍的板。空串表示这个组合没什么要说的。 */}
        {props.formatNotes[props.citationFormat] && (
          <p className="muted">{props.formatNotes[props.citationFormat]}</p>
        )}
        <p className="muted">
          正文仅保留角标，文末参考文献列表按所选格式严格排版。
          {/* 改动此处的后果必须在这里说清：正文里的角标与文末列表是生成时烘死的一整套，
              改格式能把两者拆开（文末列表跟着新格式、正文角标还是旧的），后端会把这件事
              回报成 citations_stale，界面顶部那条常驻提示据此出现。 */}
          {' '}注意：正文已生成后改动这里，不会自动重排已有的正文，只会让两者不一致。
        </p>
      </div>

      {/* 本步的蓝按钮靠**结构**保证「任何时刻恰好一个」，不靠顺序（三处改写见
          schedulePromoted 的注释）：
            schedulePromoted → 这个蓝，下面「确认引用绑定」与前进按钮降为白；
            否则有绑定     → 「确认引用绑定」蓝（今天的行为）；
            否则           → 前进按钮蓝（今天的行为）。
          后两条本就互斥（确认要 binding 真值、前进要假值），加上第一条仍然互斥。 */}
      <div className="actions">
        <button
          className={schedulePromoted ? 'btn btn-primary' : 'btn'}
          onClick={props.onSchedule}
          disabled={frozen || noDocs}
        >
          {skipped ? '重新执行引用调度' : '执行引用调度'}
        </button>
        {/* 最长 120 秒的等待（后端那次相关性判断的上限），此前这里只有一个灰按钮。
            刻意**只走不确定条**：整段等待就是那一次调用，没有任何可数的单位，给百分比
            就是编造 —— 与 WorkingBar 自己那段注释同一条判据（文献解析能确定，是因为
            它按篇做）。startedAt 只在服务端那份可用时给：本地那份没有服务端时刻，
            传 null 让 WorkingBar 按自己的挂载时刻起算（它的既有兜底），秒数照样走。 */}
        {scheduling && (
          <WorkingBar
            message={(serverScheduling && props.task.message) || '正在执行引用调度（最长 120 秒）…'}
            startedAt={serverScheduling ? props.task.started_at : null}
          />
        )}
      </div>

      {props.binding && (
        <>
          {/* 只读展示。它存在的理由是**人工确认点②**：确认按钮按下去就固化了这份绑定，
              而此前界面上只有一串标题 —— 用户看不见「为什么这篇绑在这里」，等于闭着眼盖章。
              那些 reason 是模型给的一句判断依据（兜底铺开时没有），有则显示。 */}
          <h3>系统给出的文献归属（只读）</h3>
          <div className="binding-list">
            {Object.entries(props.binding).map(([sec, refs]) => (
              <div key={sec} className="binding-section">
                <div className="binding-sec-title">{sec}</div>
                {bindingRefs(refs).map((r, i) => (
                  <div key={i} className="binding-item">
                    <span className="doc-ref">📄 {r.doc_title}</span>
                    <span className="tag">第 {r.page} 页</span>
                    {r.reason && <span className="binding-reason">{r.reason}</span>}
                  </div>
                ))}
              </div>
            ))}
          </div>
          <div className="actions">
            <button
              className={schedulePromoted ? 'btn' : 'btn btn-primary'}
              onClick={handleConfirm}
              disabled={frozen}
            >
              ✓ 确认引用绑定（人工确认点②）
            </button>
          </div>
        </>
      )}

      {skipped && (
        <div className="notice-banner">
          已跳过引用调度：本文没有角标与参考文献列表，正文按无引用撰写。
          改主意了的话，上面的「重新执行引用调度」随时可以再来一次
          （已生成的正文不会自动补上角标，要重新生成）。
        </div>
      )}

      {/* 未绑定时本步原本没有任何前进入口（确认按钮包在 props.binding 里），
          跳过引用调度后用户会卡死在这里 —— 这两种情况都必须给出一条出路。 */}
      {(canSkip || skipped) && (
        <div className="actions">
          {canSkip ? (
            <button
              className={schedulePromoted ? 'btn' : 'btn btn-primary'}
              onClick={props.onSkip}
              disabled={frozen}
            >
              跳过引用调度，进入分段生成
            </button>
          ) : (
            <button
              className={schedulePromoted ? 'btn' : 'btn btn-primary'}
              onClick={props.onNext}
              disabled={frozen}
            >
              进入分段生成
            </button>
          )}
        </div>
      )}
      {canSkip && (
        <p className="muted">
          跳过后再生成正文也不会产生角标与文末参考文献列表。
        </p>
      )}
    </div>
  )
}

// 后台任务的实时进度提示，放在触发按钮的同一行。
//
// 两种形态是有意的区分：
// - 知道总量（文献解析）走确定条，done/total 就是真实比例；
// - 不知道总量（大纲两遍 LLM、选题推荐）走不确定条。这里给百分比就是编造 ——
//   耗时取决于模型，谁也说不准，如实表达「正在动」才对。
//
// 「已等待 N 秒」以服务端给的 started_at 为锚点，刷新页面后秒数仍然准确；
// 缺失或格式不对时退回挂载时刻，绝不显示 NaN。
function WorkingBar({ message, startedAt, done, total }) {
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const t0 = Date.parse(startedAt)
    const base = Number.isNaN(t0) ? Date.now() : t0
    const tick = () => setElapsed(Math.max(0, Math.floor((Date.now() - base) / 1000)))
    tick()
    const timer = setInterval(tick, 1000)
    return () => clearInterval(timer)
  }, [startedAt])

  const known = Number.isFinite(done) && Number.isFinite(total) && total > 0
  const percent = known ? Math.round((Math.min(done, total) / total) * 100) : 0

  return (
    <div className="working-bar">
      <div className="working-track">
        {known ? (
          <div className="working-fill" style={{ width: `${percent}%` }} />
        ) : (
          <div className="working-fill working-fill-unknown" />
        )}
      </div>
      <div className="working-meta">
        <span>{message || '正在处理…'}</span>
        <span>已等待 {formatElapsed(elapsed)}</span>
      </div>
    </div>
  )
}

// 秒 -> 「45 秒」/「2 分 10 秒」
function formatElapsed(sec) {
  if (sec < 60) return `${sec} 秒`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`
}

// 秒 -> 「3 分 20 秒」
function formatEta(sec) {
  if (sec == null) return ''
  if (sec < 60) return `${sec} 秒`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`
}

// 正文与当前引用设置不一致时的常驻条。**没有关闭按钮**（理由见 App.css 里那段）：
// 它不是某次操作的失败，而是这份稿子此刻的一个事实，关掉就等于把它重新藏起来。
// 条件消失时它自己就没了 —— 重排一次文末列表、或者作废正文重写一份。
//
// 两版的界线就是后端 _refs_diff 分的那两类，前端不自己判（只渲染服务端给的 kind 与
// summary）：这两种情况的补救差一个量级，混成一句话的代价本项目刚付过 —— 此前只有
// 「回选题重做一遍」一条路，为改一个下拉框赔上大纲、绑定、设计、材料计划。
function RefsStaleBanner({ change, sections }) {
  const kind = change?.kind || ''
  const count = sections?.length || 0
  const summary = change?.summary || ''
  if (kind !== 'render' && kind !== 'numbering') {
    // 服务端老到不给这个字段时的兜底：只讲事实与出路，不猜原因（不写成「回选题」，
    // 那条路已经不是这里的答案了）。正常路径上到不了这里。
    return (
      <div className="stale-banner">
        <b>正文与当前引用设置不一致。</b>
        {' '}请到「分段生成」步处理：那里会给出这次该做哪一个补救动作。
      </div>
    )
  }
  return (
    <div className="stale-banner">
      <b>{kind === 'render' ? '文末列表与当前引用设置不一致。' : '正文与当前引用设置不一致。'}</b>
      {' '}{summary}。
      {kind === 'render' ? (
        '正文里的角标指的仍是原来那篇文献，不必重写正文 —— 到「分段生成」步点一下'
        + '「按当前设置重排文末列表」即可（确定性重排、不调用模型）。'
      ) : (
        ` 这套编号已经烘进正文里了，改回设置也修不好 —— 到「分段生成」步点一下`
        + `「作废正文并重写」（${count > 0 ? `只作废已生成的 ${count} 节正文，` : ''}`
        + '大纲、引用绑定、设计与材料计划都保留）。'
      )}
    </div>
  )
}

function GenerateStep(props) {
  const p = props.progress
  const done = p?.done || 0
  const total = p?.total || 0
  const percent = total > 0 ? Math.round((done / total) * 100) : 0
  const failed = p?.status === 'error'
  const interrupted = p?.status === 'interrupted'
  const finished = p?.status === 'done'
  // 已写了部分章节但没写完 —— 按钮改成「继续生成」，从断点续写
  const resumable = !props.generating && done > 0 && !finished
  // 引用设置改过、而正文还是按旧的一套写成的时候，**蓝按钮先做补救、不做生成**：
  //   render    → 重排文末列表（零模型、正文一个字不动）
  //   numbering → 作废正文并重写（大纲/绑定/设计/材料计划都保留）
  // 这两种情况下原来那个生成按钮**必须收起来**：numbering 时它必然被后端 400，render
  // 时点它等于跳过该做的补救 —— 两种都是「看着能点、点了没反应或者点错」。
  // 有任务在跑时不参与（按钮本来就禁着），免得跑到一半按钮突然换了名字。
  const change = props.refsChange || {}
  const fix = props.busy ? 'none' : (change.kind || 'none')
  const sectionsCount = props.sections?.length || 0
  // 正文已完整落库：有正文，而且进度里也没有「还没写完」这件事。
  //   `!props.generating` 这一条不能省 —— resumable 里带着 !generating，生成中时它恒为
  //   false，光看 `!resumable` 会把「正在写」误判成「已写完」，蓝按钮位就会在写到一半
  //   的时候跳成「查看导出结果」。
  //   这里刻意**不**用「节数够了」来判完整（`sectionsCount >= total`）：error 与
  //   interrupted 是既有的两种「可续写」终态，那时点一次「继续生成」正好把状态修回来；
  //   改按节数判会让这两个状态也跳成「已写完」，与那条设计打架。
  const complete = sectionsCount > 0 && !resumable && !props.generating
  // 蓝按钮位上站谁：引用设置改过时让给补救（上面那两个 fix 分支优先），其余情况正文写完
  // 了就站「查看导出结果」—— 它是这个状态下**该点的那一个**（前进），而不是重做一遍。
  const viewPrimary = fix === 'none' && complete

  return (
    <div className="card">
      <h2>{props.stepNo}. 渐进式分段生成</h2>
      <p className="muted">
        按章节逐段生成，自动继承上文上下文，每段生成后自动校验字数并校准，确保逻辑连贯、字数达标。
        生成在后台进行，可随时刷新页面，进度不会丢失。
      </p>

      {(props.generating || (p && p.status !== 'idle' && total > 0)) && (
        <div className="progress-wrap">
          <div className="progress-track">
            <div
              className={`progress-fill ${failed ? 'progress-fill-error' : ''}`}
              style={{ width: `${percent}%` }}
            />
          </div>
          <div className="progress-meta">
            <span>
              <strong>{percent}%</strong> · 已完成 {done}/{total} 节
            </span>
            <span>
              {props.generating && p?.current && `正在撰写：${p.current}`}
              {!props.generating && finished && '已全部完成'}
              {failed && '生成中断'}
            </span>
          </div>
          <div className="progress-meta">
            <span>
              已写 {p?.words_so_far || 0} / {p?.words_target || 0} {props.wordsUnit}
            </span>
            <span>
              {props.generating && p?.eta_seconds != null && `预计剩余 ${formatEta(p.eta_seconds)}`}
              {props.generating && p?.eta_seconds == null && done > 0 && '正在估算剩余时间…'}
              {props.generating && done === 0 && '正在准备…'}
            </span>
          </div>
        </div>
      )}

      {failed && <div className="error-banner">生成失败：{p?.error}</div>}
      {interrupted && (
        <div className="error-banner">
          {p?.error || '生成任务因服务重启而中断，已完成的章节已保存，可继续生成。'}
        </div>
      )}

      {/* 四个按钮一律按 busy 禁：/references/rerender、/sections/reset、/generate 在后端
          查的都是**两套**忙闲闸（生成中重排/作废会被 409，别的任务在跑时同样会被 409）。 */}
      <div className="actions">
        {/* 正文已经全部在的时候，这一格原来站着「开始分段生成」：点它后端会一路走完循环、
            发现一节都不用写，只把 status 原样写回 COMPLETED（项目已经导出过时还会把
            exported 顶回 completed）——一次纯空转。而真正该做的「重写正文」就在旁边那个
            白按钮上，同一个状态并排两个按钮、其中一个还是空的。所以这一格让给
            「查看导出结果」：它才是这个状态下该点的那一个（前进，不是重做一遍）。 */}
        {fix === 'render' ? (
          <button className="btn btn-primary" onClick={props.onRerender} disabled={props.loading || props.busy}>
            按当前设置重排文末列表
          </button>
        ) : fix === 'numbering' ? (
          <button className="btn btn-primary" onClick={props.onRewrite} disabled={props.loading || props.busy}>
            作废正文并重写
          </button>
        ) : viewPrimary ? (
          <button className="btn btn-primary" onClick={props.onView}>查看导出结果 →</button>
        ) : (
          <button
            className="btn btn-primary"
            onClick={props.onGenerate}
            disabled={props.loading || props.busy}
          >
            {props.generating
              ? '生成中…'
              : resumable ? '继续生成（从断点续写）' : '开始分段生成'}
          </button>
        )}
        {/* 任何时候都能重写正文（此前只能靠回「选题与设定」改一个字段绕路，代价是
            把大纲、绑定、设计、材料计划一起作废）。numbering 时蓝按钮就是这个动作，
            不再重复给一个 —— 两个名字做同一件事，用户会以为它们不一样。 */}
        {sectionsCount > 0 && fix !== 'numbering' && !props.generating && (
          <button className="btn" onClick={props.onRewrite} disabled={props.loading || props.busy}>
            重写正文
          </button>
        )}
        {/* 同一个入口在蓝按钮位上站着的时候就不再给一个（取 viewPrimary 的反）——只有蓝位
            被那两个补救动作占着时，它才退到这里当次要出口。 */}
        {complete && !viewPrimary && (
          <button className="btn" onClick={props.onView}>查看导出结果 →</button>
        )}
      </div>

      {fix !== 'none' && (
        <p className="muted">
          {fix === 'render'
            ? '引用设置改过之后，先按当前设置重排文末列表：正文里的角标指的仍是原来那篇文献，不必重写正文。'
            : '引用编号改过之后不能直接续写（角标会指向错的文献），必须先作废正文再重写；大纲、引用绑定、设计与材料计划都会保留。'}
        </p>
      )}

      {props.generating && (
        <div className="loading-hint">
          正在逐段撰写，请保持页面打开（关闭后已生成的章节不会丢失）。
        </div>
      )}

      {props.sections && props.sections.length > 0 && (
        <div className="done-list">
          <h3>已生成章节（{props.sections.length}）</h3>
          {props.sections.map((s, i) => (
            <div key={i} className="done-item">
              <span className="done-title">{s.section_title}</span>
              <span className="muted">{s.actual_words} {props.wordsUnit}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function ExportStep(props) {
  const { sections, project, references, referenceRuns } = props
  const total = sections.reduce((s, x) => s + x.actual_words, 0)
  // 标题在这里出现三次（预览、导出成品的一级标题、下载文件名），取一次、三处共用，
  // 免得预览上的名字和文件里的名字不是同一个
  const title = projectLabel(project)
  // 这一页上有三样东西跟着写作语言走：字数单位、文末列表那一节的标题词、导出排版。
  // 语言只有**一处**来源 —— 服务端那一列（本组件已经拿到了 project）；单位后端的
  // 那张表由 App 现算后传下来（见 wordsUnit）。各推一份就会漂出「屏幕上写着参考文献、
  // 下载下来是 References」这种不一致，而它只在用户打开文件的那一刻才暴露。
  const lang = project?.writing_lang || FALLBACK_DEFAULT_WRITING_LANG
  const unit = props.wordsUnit
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef(null)

  // 只负责关：点面板外、按 Esc。不做 hover 展开 —— 鼠标路过就弹出一层菜单，
  // 想点旁边的东西时会被它挡住。
  useEffect(() => {
    if (!menuOpen) return undefined
    function onDown(e) {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    function onKey(e) {
      if (e.key === 'Escape') setMenuOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  // 有任务开始跑就把菜单收起来：触发按钮已经禁了，留着这份摊开的菜单等于留着一个
  // 能点的入口（菜单里的每一项都会去写那条导出回执，而后端这时会 409）。
  useEffect(() => { if (props.busy) setMenuOpen(false) }, [props.busy])

  // 一条参考文献的显示文本：**只认后端给的那一份**。
  //
  // 这里原先有第二份实现（后端 citation_format._gb7714 是正身，前端那份是"兜底"：
  // `formatted` 缺失时就地按 GB/T 形态拼一条出来）。它本轮被删掉了，推翻的是上一轮
  // 记档在 test_frontend_mirror.py 里的那个决定，理由如下：
  //
  // ① **它不可能再同步**。上一轮它只是三行 GB/T 拼接，与后端一起改的成本很低。本轮
  //    之后 APA / MLA 要的是姓名倒装 + 缩写带点 + `&` + 20 作者规则 + 方括号英译题名，
  //    等于把后端 names.py 那六十行启发式在 JS 里再实现一遍。做不到同步却号称同步，
  //    比没有更坏：`formatted` 一旦缺失，英文论文的预览会印出 GB/T 形态的姓名 ——
  //    正是本轮要消灭的那个 bug，而且它看起来完全正常。
  // ② **它的存在理由本身不成立**。记档写的理由是"元信息是异步来的，首屏必须有东西
  //    可渲染"，但 `formatted` 与 doc_title / num 来自**同一次响应**（同一个列表、
  //    同一个对象），不存在"有题名却没有 formatted"的时刻。references 为空时这个分支
  //    一行都不渲染，所以它连"首屏空窗"都解释不了。
  //
  // 于是它降级为一句提示：**如实说而不是猜**（项目既有约定），自带补救指引。
  // 后端侧有保证：`tests/test_generation.py` 那条「快照里每条 formatted 非空」的用例，
  // 启动时的 _migrate_reference_snapshots 也会给存量快照补上 —— 所以这条分支在实际
  // 使用中不可达，它是守卫，不是兜底渲染。
  function formatRef(r) {
    if (r.formatted) return r.formatted
    return `[${r.num}]（本条格式未生成，请到引用调度步重排文末列表）`
  }

  // 片段形态的兜底，与 formatRef 那条**必须同进同出**：两者描述的是同一条文献，
  // 一条有 `formatted` 而另一条没有就是自相矛盾。所以没拿到片段时退回的**正是
  // formatRef 的结果**（整条正体）—— 与 `formatted` 缺席时那句提示逐字相同。
  // 否则某条旧快照会让预览崩在 `.map` 上、或让预览与下载下来的 docx 说的不是一回事。
  function formatRefRuns(r, i) {
    const runs = referenceRuns && referenceRuns[i]
    if (Array.isArray(runs) && runs.length > 0) return runs
    return [{ text: formatRef(r), italic: false }]
  }

  // 三种格式共用同一份「导出文档」：标题、章节、参考文献只在这里拼一次，
  // 具体怎么渲染交给 exporters.js（各格式自己再拼一遍标题与列表，就是三份迟早漂移的实现）。
  //
  // 文件交给浏览器之后**再**记回执（把「已导出」写进后端状态机，见 App.markExported）。
  // 顺序不能反：先记后导的话，万一浏览器拦下了下载、用户当场取消了保存，库里就已经
  // 写着「已导出」；反过来，记账失败不影响文件是真的导出成功了（那边按提示处理）。
  function runExport(key) {
    setMenuOpen(false)
    // 排版语言读 `project.writing_lang`（后端下发的那一列），前端不推导、不另立常量：
    // 它是**产物**的属性 —— 一份英文论文导出的 Word 该是 Times New Roman、双倍行距、
    // 四边 1 英寸、文末叫 References；中文那一套一个字都不动（有 sha256 哨兵盯着）。
    exportDocument(
      key,
      {
        title,
        sections: sections.map((s) => ({ title: s.section_title, content: s.content })),
        references: (references || []).map(formatRef),
        // 与上面那一行**同源同序**（都从 references 走一遍），所以第 i 条片段一定
        // 属于第 i 条文献 —— 这正是 exporters.js 在顶部归一化时依赖的前提。
        referenceRuns: (references || []).map(formatRefRuns),
      },
      safeFilename(title),
      lang,
    )
    props.onExported()
  }

  return (
    <div className="card">
      <h2>{props.stepNo}. 预览与导出</h2>
      {/* 标题原先在这一页完全不显示，而它是导出成品的第一行 */}
      <div className="export-title">{title}</div>
      <div className="budget-bar">
        <span>全文总计：<strong>{total}</strong> {unit}（目标 {project.target_words} {unit}）</span>
      </div>
      <div className="actions">
        {/* 一个按钮带出三种格式：格式表在 exporters.js 里，加一种格式只改那一处。
            菜单项按「最可能是用户要的那一个」排 —— Word 在最前是因为它是要交给
            老师/期刊的那一份；Markdown 排最后（原先是唯一的那个按钮）。
            按钮文案是「导出全文」：导出的确实是全文（标题 + 各节正文 + 文末参考文献），
            而这一页上还显示着「全文总计 N 字」这类局部信息，只写「导出」会让人不确定
            导出的是不是当前这一屏看到的东西。 */}
        <div className="export-menu" ref={menuRef}>
          <button
            className="btn btn-primary"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            // 拼文件这件事本身不碰服务端，但这一次点击还带着一条回执（completed →
            // exported），而回执会被后端的忙闲闸 409。与其让用户导出一个文件、再看到
            // 一句「没记上」，不如等那个任务跑完 —— 这也是全仓一致的尺子：按钮不亮着
            // 让用户去点一个必然被拒的动作。
            disabled={props.busy}
            title={props.busy ? '有任务正在进行，等它结束后再导出' : undefined}
            onClick={() => setMenuOpen((v) => !v)}
          >
            导出全文 <span className="caret">▾</span>
          </button>
          {menuOpen && (
            <div className="menu" role="menu">
              {EXPORT_FORMATS.map((f) => (
                <button key={f.key} role="menuitem" onClick={() => runExport(f.key)}>
                  {f.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {sections.map((s, i) => (
        <div key={i} className="section-preview">
          <div className="preview-header">
            <h3>{s.section_title}</h3>
            <span className="muted">{s.actual_words} / {s.word_budget} {unit}</span>
          </div>
          <p className="preview-body">{s.content.slice(0, 500)}{s.content.length > 500 ? '...' : ''}</p>
        </div>
      ))}

      {references && references.length > 0 && (
        <div className="section-preview ref-list">
          <div className="preview-header">
            {/* 这一节的标题词也是**产物文字**（它会印在论文里，也是导出文件里那一节的
                标题），所以跟导出走的是同一个函数 —— 各写一个词，屏幕上与下载下来的
                就会不一样。 */}
            <h3>{referencesHeading(lang)}</h3>
            <span className="muted">{references.length} 条</span>
          </div>
          <ol className="ref-items">
            {references.map((r) => (
              <li key={r.num}>{formatRef(r)}</li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}

// ==================== 设置（API Key 配置） ====================

function SettingsModal(props) {
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('deepseek-chat')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [current, setCurrent] = useState(null)

  useEffect(() => {
    api.getLlmConfig().then((c) => {
      setCurrent(c)
      setBaseUrl(c.llm_base_url || '')
      setModel(c.llm_model || 'deepseek-chat')
    }).catch(() => {})
  }, [])

  async function save() {
    setSaving(true)
    setMessage('')
    try {
      const res = await api.updateLlmConfig({
        api_key: apiKey || undefined,
        base_url: baseUrl || undefined,
        model: model || undefined,
      })
      setMessage(res.llm_configured ? '✅ 配置已保存，LLM 已启用' : '配置已保存（API Key 为空）')
      setCurrent(res)
      setApiKey('')
      props.onSaved && props.onSaved()
    } catch (e) {
      setMessage('❌ 保存失败：' + e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={props.onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>⚙ 设置 — 接入你的 LLM</h2>
          <button className="btn" onClick={props.onClose}>✕</button>
        </div>

        <div className="modal-body">
          <p className="muted">
            填写你自己的大模型 API Key，即可使用本系统生成真实学术内容。
            Key 仅保存在本机（<code>~/.academic_writer/config.json</code>），不会上传。
          </p>

          {current && (
            <div className="config-status">
              <span className="muted">当前状态：</span>
              {current.llm_configured
                ? <span className="tag tag-success">已配置（{current.llm_api_key_masked}）</span>
                : <span className="tag">未配置</span>}
            </div>
          )}

          <div className="field">
            <label>API Key（必填）</label>
            <input
              type="password"
              placeholder="sk-..."
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>

          <div className="field">
            <label>Base URL（可选，留空用服务商默认）</label>
            <input
              placeholder="https://api.deepseek.com/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
            <p className="muted">
              常见：DeepSeek <code>https://api.deepseek.com/v1</code>；
              通义 <code>https://dashscope.aliyuncs.com/compatible-mode/v1</code>
            </p>
          </div>

          <div className="field">
            <label>模型名</label>
            <input
              placeholder="deepseek-chat"
              value={model}
              onChange={(e) => setModel(e.target.value)}
            />
          </div>

          {message && <div className="config-message">{message}</div>}
        </div>

        <div className="modal-footer">
          <button className="btn" onClick={props.onClose}>取消</button>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}
