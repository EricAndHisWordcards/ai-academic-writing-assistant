// 界面用到的**数据表**（兜底镜像、状态锚点、字段清单……）。
//
// 从 App.jsx 顶部原样搬来（v1.32 拆分），注释一字未改 —— 这个项目的注释本身就是这类
// 常量为什么长成这样的唯一说明。**位置不参与任何断言**：test_frontend_mirror.py 与
// test_frontend_conventions.py 读的是整个 src/ 树，不关心它在哪个文件里。
//
// 真身几乎都在**后端**（paper_types / citation_format / writing_lang / metadata），
// 这里的 FALLBACK_* 只是 /api/meta 到达前的那一帧。

// 各阶段的显示名（key 是稳定标识，序号随工序变化，见 buildSteps）
export const STEP_LABELS = {
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
export const FALLBACK_PAPER_TYPES = [
  '定量/实证研究', '质性研究/案例分析', '工程设计/系统实现', '理论推导/数理建模',
  '文献综述', '课程论文/小论文', '技术/工程报告',
]
// 短名 → 下拉框里显示的长名（含适用范围），正常由 /api/meta 的 paper_type_labels 下发。
export const FALLBACK_TYPE_LABELS = {
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
export const FALLBACK_MIN_TARGET_WORDS = 1000

// 写作语言与参考文献格式的兜底镜像。真身在**后端**（app/writing_lang.py 与
// app/citation_format.py），正常由 /api/meta 下发；这一份只在元信息到达前、或它
// 失败时用，处境与 FALLBACK_PAPER_TYPES 相同（test_frontend_mirror.py 与后端对账）。
//
// **为什么非镜像不可**：格式下拉框必须按语言派生，而它派生不出东西来的时候是**空的**
// 下拉框（`[].map()` 一个选项都没有），比给错了还糟。这几张表加起来的体积骗不了人，
// 但它们不是「第二份格式清单」—— 清单只有后端一份，这里只是它到达之前的那一帧。
export const FALLBACK_WRITING_LANGS = ['zh', 'en']
export const FALLBACK_WRITING_LANG_LABELS = { zh: '中文', en: '英文' }
export const FALLBACK_DEFAULT_WRITING_LANG = 'zh'
// 语言 → 该语言下符合习惯的著录格式（顺序即下拉框顺序）。英文写作里没有 GB/T 7714 ——
// 这条判据在后端也只有一份（citation_format._FORMATS_BY_LANG），后端那道 400 读的
// 就是它。
export const FALLBACK_CITATION_FORMATS_BY_LANG = {
  zh: ['gb7714', 'apa', 'mla'],
  en: ['apa', 'mla'],
}
export const FALLBACK_DEFAULT_CITATION_FORMAT_BY_LANG = { zh: 'gb7714', en: 'apa' }
// 后端认识的全部著录格式（不分语言）。用途只有一个：算出**本语言下被排除掉**的那几个，
// 好把「它们为什么不在这儿」说清楚 —— 那句话本身也来自后端（见 formatNotes），
// 前端只管挑哪几个该说。真身是 citation_format.SUPPORTED_FORMATS。
export const FALLBACK_SUPPORTED_FORMATS = ['gb7714', 'apa', 'mla']
// 格式标识 → 选项文字。「（默认）」**不在标签里**：默认格式随语言变，写进标签就会在
// 另一种语言下印着一个错的默认标记 —— 它由前端按上面那张 default 表现判。
export const FALLBACK_CITATION_FORMAT_LABELS = {
  gb7714: 'GB/T 7714', apa: 'APA（第 7 版）', mla: 'MLA（第 9 版）',
}
// 目标字数的计数单位：中文按字、英文按词。它同时是**显示单位**与后端 count_units 的
// 计数口径，所以英文项目里显示「目标 5000 字」不只是文案不对 —— 用户会照着填 5000，
// 而产出的是 5000 个词（约等于中文七千多字的体量）。
export const FALLBACK_WORDS_UNIT_BY_LANG = { zh: '字', en: 'words' }
// 角标样式（不随语言变，所以只有一份）。与 FALLBACK_CITATION_FORMAT_LABELS 同款：
// 名字的真身在后端（citation_format.CITE_STYLE_LABELS），这里只是它到达前的那一帧。
// 它不是可有可无的装饰 —— 后端那道 400 用的是同一张表里的名字，两边同源才不至于出现
// 「报错说你选的 [1] 方括号角标不支持」这种用户对不上号的句子。
export const FALLBACK_CITE_STYLES = ['bracket', 'superscript']
export const FALLBACK_CITE_STYLE_LABELS = {
  bracket: '[1] 方括号角标', superscript: '纯数字上标（¹ ² 风格）',
}
// 必须是 FALLBACK_TYPE_CONFIG 的既有键，否则 configFor 兜不回来
export const DEFAULT_TYPE_KEY = '课程论文/小论文'
export const DESIGN_STEPS = ['topic', 'design', 'outline', 'resources', 'citation', 'generate', 'export']
// 与后端 app/metadata.py 的 MISSING_META_VALUES **同源**：模型在文献里找不到作者/年份时
// 不会留空，而是写「未提及」「Not specified」这类占位词——它们是普通字符串，不加判断
// 就会被当成真实作者名显示或导出。test_frontend_mirror.py 会断言两边逐字一致。
export const MISSING_META_VALUES = [
  '未提及', '未提供', '未说明', '未标注', '未给出', '未注明', '未知', '不详',
  '无', '空', '暂无', '缺失',
  'not specified', 'not given', 'not stated', 'not provided', 'unspecified',
  'unknown', 'n/a', 'na', 'none', 'null', 'nil',
  '-', '--', '—', '－',
]
// 文献类型标识白名单，**与后端 app/citation_format.py 的 SOURCE_TYPES 同源**
// （test_frontend_mirror.py 对账）。它现在只有一个消费点：文献编辑界面的类型下拉框
// （选项由它派生，见 DocumentEditModal）。越界值回落 J 那一半在**后端**
// `clean_source_type` —— 前端不再自己拼参考文献，也就没有第二处需要收敛。
export const SOURCE_TYPE_LETTERS = ['J', 'M', 'D', 'C', 'N', 'R', 'S', 'P', 'G', 'Z']
// 类型标识的中文名，只给编辑界面的下拉项当注解。**不是第二份白名单**：下拉项的字母
// 由 SOURCE_TYPE_LETTERS 派生（见 DocumentEditModal），这里漏一个键只会让那一项少了
// 注解文字，不会少一个可选项 —— 两份清单因此不可能漂移。
export const SOURCE_TYPE_LABELS = {
  J: '期刊文章', M: '专著', D: '学位论文', C: '会议录', N: '报纸',
  R: '报告', S: '标准', P: '专利', G: '汇编', Z: '其他',
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
export const DOC_EDIT_FIELDS = [
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
export const FALLBACK_TYPE_CONFIG = {
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
export const POLLED_TASK_KINDS = new Set([
  'outline', 'documents', 'material_analysis', 'design_extract', 'clusters', 'citations',
])
export const TASK_POLL_MS = 1200
// 轮询**连续失败**时的退避上限：失败一次就慢一倍，最多这么慢。
// 为什么不是「失败几次就放弃」：这里的进度条是用户唯一的反馈，放弃等于把一个还要
// 跑几分钟的任务变成「永远转圈、按钮永远禁用」，自救手段只剩刷新页面。退避到 30 秒
// 已经不会给服务造成任何压力，而服务一旦回来它自己就恢复了。
export const TASK_POLL_MAX_MS = 30000
// 状态 -> 语义锚点(key)。所有取值都同时存在于两套工序里，因此无论顺序如何，
// steps.findIndex 都能命中，不会出现 -1。
export const STATUS_ANCHOR = {
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
export const ADVANCE_AFTER = new Set([
  'topic_set', 'outline_confirmed', 'citation_confirmed', 'completed',
])
// 「这一步已经做过了」的状态。与 STATUS_ANCHOR 回答的问题不同：那个答「现在停在哪」，
// 这个答「走到过这里没有」—— 回看前面某一步时（生成中、生成完）仍然要能认出那里
// 做过什么，否则用户看到的是一句「请先上传文献」，像是自己做过的事被撤销了。
export const STEP_DONE_STATUSES = new Set([
  'citation_confirmed', 'generating', 'completed', 'exported',
])
