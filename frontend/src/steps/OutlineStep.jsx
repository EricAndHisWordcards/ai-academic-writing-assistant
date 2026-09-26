// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { useEffect, useState } from 'react'
import { WorkingBar } from '../components/WorkingBar'
import { STATUS_ANCHOR } from '../constants.js'
import { statusToStepKey, wordsNumber } from '../helpers.js'

export function OutlineStep(props) {
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
export function Reason({ text, indent = 0 }) {
  if (!text) return null
  return (
    <div className="budget-reason" style={indent ? { paddingLeft: indent } : undefined}>
      {text}
    </div>
  )
}

// 字数预算只落在最细粒度节点：有子节时累计子节，否则累计节本身。
// 需与后端 routers/projects.py 的 _sum_budget 保持一致。
export function outlineTotal(outline) {
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

export function cloneOutline(outline) {
  return JSON.parse(JSON.stringify(outline))
}

export function updateBudget(props, ci, si, val) {
  const next = cloneOutline(props.outline)
  const sec = next.chapters[ci].sections[si]
  sec.word_budget = val
  clearReason(sec)
  props.setOutline(next)
}

export function updateSubBudget(props, ci, si, bi, val) {
  const next = cloneOutline(props.outline)
  const sub = next.chapters[ci].sections[si].subsections[bi]
  sub.word_budget = val
  clearReason(sub)
  props.setOutline(next)
}

// 依据是模型为某个具体数字给出的论证，数字被手动改掉之后那句话就不再成立，
// 留着它等于在界面上摆一句假话。
export function clearReason(node) {
  delete node.rationale
}

// 文献综述的「主题聚类」面板。放在大纲步内、生成按钮之前 —— 聚类本就是大纲的
// 上游产物，把它做成独立步骤会牵动 STATUS_ANCHOR / statusToStepKey 的锚点体系，
// 而它并不对应任何项目状态。
//
// **作者能改的只有簇标题**：文献归属（doc_ids）只读，因为它由模型按全文判断，
// 手改一个 id 只会让「这篇文献讲什么」与簇标题真的对不上，而这种错配在大纲里
// 看不出来——章节照样生成，只是主题是错的。
export function ClusterPanel(props) {
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
