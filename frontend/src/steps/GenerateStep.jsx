// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { formatEta } from '../components/WorkingBar'

export function GenerateStep(props) {
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
