// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { api } from '../api.js'
import { WorkingBar } from '../components/WorkingBar'
import { POLLED_TASK_KINDS, STEP_DONE_STATUSES } from '../constants.js'
import { ResourceStep } from './ResourceStep'

export function CitationStep(props) {
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

export function bindingRefs(refs) {
  if (!Array.isArray(refs)) return []
  return refs.filter((r) => r && typeof r === 'object')
}
