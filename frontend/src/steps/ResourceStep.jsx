// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { useState } from 'react'
import { api } from '../api.js'
import { WorkingBar } from '../components/WorkingBar'
import { DOC_EDIT_FIELDS, SOURCE_TYPE_LABELS, SOURCE_TYPE_LETTERS } from '../constants.js'
import { initialSourceType, isMissingMeta, materialsPendingOf } from '../helpers.js'

export function ResourceStep(props) {
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
            ? '围绕研究主题检索并下载真实文献 PDF（或知网 CAJ），然后批量上传 —— 下一步的大纲将据此生成。'
            : '根据大纲章节关键词下载真实文献 PDF（或知网 CAJ），然后批量上传。文献用于正文引用与文末参考文献列表；'
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
            type="file" multiple accept=".pdf,.caj,.kdh"
            onChange={props.onUpload}
            disabled={props.loading || props.busy}
          />
          <span>点击上传批量 PDF/CAJ 文献（支持多选）</span>
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

export function MaterialPanel(props) {
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
            accept=".pdf,.caj,.kdh,.docx,.txt,.md,.markdown,.csv,.json,.log,.xlsx"
            onChange={props.onUploadMaterials}
            disabled={props.loading || analyzing}
          />
          <span>或上传文件（PDF / CAJ / Word / Excel / txt / csv）</span>
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

// 文献元数据编辑框。
//
// **为什么得有它**：元数据全部由一次 LLM 调用抽取，抽错是常态；而此前改正的唯一
// 办法是删掉重传 —— 那条路走不通，同一份 PDF 会被内容指纹判重静默跳过（正文一个字
// 没变）。也就是说抽错的作者其实改不掉，这个框是那条断路的接口。
//
// 提交走 PATCH，只发**改动过的**字段：后端把「字段缺席」解释成「不修改这一项」、
// 把空串解释成「清空」。逐字段比对同时让「什么都没改就点保存」变成一次纯粹的关闭。
export function DocumentEditModal(props) {
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
