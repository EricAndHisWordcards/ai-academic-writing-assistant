// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { useEffect, useRef, useState } from 'react'
import { FALLBACK_DEFAULT_WRITING_LANG } from '../constants.js'
import { EXPORT_FORMATS, exportDocument, referencesHeading } from '../exporters.js'
import { projectLabel, safeFilename } from '../helpers.js'

export function ExportStep(props) {
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
