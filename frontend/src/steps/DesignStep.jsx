// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { WorkingBar } from '../components/WorkingBar'

// 研究设计 / 技术方案录入（实证研究、技术报告）
//
// 为什么排在大纲之前：这一类论文的核心章节写的正是作者本人给出的一手内容 ——
// 实证的数据与结果、质性的访谈与编码、工程的需求与实现、数理的假设与推导。
// 不先把这些交给模型，它只能编——编变量、编显著性、编结论，而这份虚构在确认点①上
// 看起来完全合理，且会一路带进正文。字段名随类型而定，渲染逻辑完全通用。
export function DesignStep(props) {
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
