// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { useRef, useState } from 'react'

// 就地改名：默认渲染成文本 + ✎，点文本本身或 ✎ 进入输入态，回车保存、失焦保存、
// Esc 取消。清空输入 = 提交空串 = 回到「跟随研究核心方向」（后端的跟随条件就是
// title 为空 —— 清空是这条路的回车键，不是「把名字删掉」）。
//
// 两个调用点都在**可点击的容器**里（侧栏卡片整块是「打开项目」，页头 h1 也在
// 顶栏），所以这里自己把 click / mousedown / keydown 一律 stopPropagation：
// 否则点一下 ✎ 会顺带把项目打开、或者输入时触发外层快捷键。
export function EditableTitle({ value, topic, onRename, onEditingChange, className = '' }) {
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
