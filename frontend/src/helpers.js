// 纯函数：与 React 无关，任何组件都能直接用。
//
// 从 App.jsx 顶部原样搬来（v1.32 拆分），注释一字未改。放在这里而不是某个组件里，
// 是因为它们**本来就有多个消费点**，而每个消费点在原文件里都要靠"往上看 300 行"才能
// 读到契约（比如 wordsNumber 为什么必须是 Number 而不是 parseInt）。
//
// 一概不 import React —— 这也让它们随时可以用 node 直接跑起来验证。

// 「目标总字数」框里那串字符 → 数字。空串、'0'、敲了一半的 '-' 都归一到 0：
// 库里没有「空」这个表示（同 writing_ideas 的空串落 NULL），0 就是「还没填」。
// 归一只有这一处：草稿回灌与提交前的检查都读它，两处各写一遍就迟早不一致。
//
// 用 Number 而不是 parseInt：type="number" 的框交回来的本来就是**浮点字面量**，
// 而 parseInt 是按十进制前缀截的 —— 它会把 "1e3" 读成 1。用户敲 "1e3" 时框里
// 明明写着 1e3，拒绝语却会说「当前 1 字」，那是对用户刚敲的东西的一句假话。
// 小数截断（1000.5 → 1000）是刻意的：字数没有半个。
export const wordsNumber = (v) => Math.trunc(Number(v)) || 0
export const isMissingMeta = (v) =>
  !v || MISSING_META_VALUES.includes(String(v).trim().toLowerCase())
// 类型下拉框的初值：库里那份标识**收敛之后**的形态。抽出来的值可能不在白名单里
// （模型写了别的字母），那种值在下拉框里没有对应选项、React 会把选中项显示成空白；
// 小写字母（如 'j'）同理，下拉框里只有大写。两种都收敛成空 —— 界面上明确显示为
// 「未设置」，而不是一个看起来像空白、实际存着脏值的框。
export const initialSourceType = (doc) => {
  const upper = String(doc?.source_type || '').trim().toUpperCase()
  return SOURCE_TYPE_LETTERS.includes(upper) ? upper : ''
}
// 这里原先有一个 normalizePages（页码归一化），它只服务于前端那份兜底的参考文献
// 拼接实现。那一份已删，理由写在 ExportStep 的 formatRef 上；页码归一化现在只有
// 后端 citation_format.normalize_pages 一处。
// 项目显示名（同时也是论文标题：用户起的名字优先，其次是他填的研究核心方向）。
// **推导只有后端一份**（projects._display_title，跟着每个项目接口下发 display_title），
// 这里纯粹是「前后端版本不一致时别整屏空白」的最后一手，不是第二份推导 —— 原先的
// 5 个消费点各写一遍 `|| '未命名论文'`（其中导出两处写的是 `|| '论文'`）就是漂移现场。
export const projectLabel = (p) => p?.display_title || '未命名论文'
// 下载文件名。Windows 上 \ / : * ? " < > | 都是非法字符，结尾的点和空格也存不下去，
// 清洗后可能变空（标题整串都是 `???`）→ 回退到兜底词，免得下载出一个 `.md` 隐藏文件。
// 只用于 a.download：md 正文里的一级标题要用原标题，冒号在 markdown 里完全合法。
export const safeFilename = (name, fallback = '论文') =>
  String(name || '')
    .replace(/[\\/:*?"<>|]/g, '_')
    .trim()
    .replace(/[.\s]+$/, '')
    .slice(0, 80) || fallback
// 未知类型（含已下线的「毕业论文」）退化为课程论文 —— 与后端 config_for 的兜底一致。
export function configFor(paperType, typeConfig) {
  const table = typeConfig && Object.keys(typeConfig).length
    ? typeConfig : FALLBACK_TYPE_CONFIG
  return table[paperType] || table[DEFAULT_TYPE_KEY] || FALLBACK_TYPE_CONFIG[DEFAULT_TYPE_KEY]
}

export function buildSteps(paperType, typeConfig) {
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
export function statusToStepKey(status, steps) {
  const anchor = STATUS_ANCHOR[status] || 'topic'
  const i = steps.findIndex((s) => s.key === anchor)
  if (i < 0) return steps[0]?.key || 'topic'
  if (ADVANCE_AFTER.has(status) && i + 1 < steps.length) return steps[i + 1].key
  return steps[i].key
}
// 材料分析是否正在跑。判定只此一份：ResourceStep 自己要用（禁按钮、渲染进度条），
// 「有没有材料还没分析」也要用它。
export function materialsAnalyzing(task) {
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
export function materialsPendingOf({ isReview, materials, materialsPlan, task }) {
  return !isReview && materials.length > 0 && !materialsPlan && !materialsAnalyzing(task)
}
