// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { useEffect, useState } from 'react'

// 后台任务的实时进度提示，放在触发按钮的同一行。
//
// 两种形态是有意的区分：
// - 知道总量（文献解析）走确定条，done/total 就是真实比例；
// - 不知道总量（大纲两遍 LLM、选题推荐）走不确定条。这里给百分比就是编造 ——
//   耗时取决于模型，谁也说不准，如实表达「正在动」才对。
//
// 「已等待 N 秒」以服务端给的 started_at 为锚点，刷新页面后秒数仍然准确；
// 缺失或格式不对时退回挂载时刻，绝不显示 NaN。
export function WorkingBar({ message, startedAt, done, total }) {
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
export function formatElapsed(sec) {
  if (sec < 60) return `${sec} 秒`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`
}

// 秒 -> 「3 分 20 秒」
export function formatEta(sec) {
  if (sec == null) return ''
  if (sec < 60) return `${sec} 秒`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分钟`
}
