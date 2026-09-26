// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。
//
// ==================== 设置（API Key 配置） ====================

import { useEffect, useState } from 'react'
import { api } from '../api.js'

export function SettingsModal(props) {
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('deepseek-chat')
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [current, setCurrent] = useState(null)

  useEffect(() => {
    api.getLlmConfig().then((c) => {
      setCurrent(c)
      setBaseUrl(c.llm_base_url || '')
      setModel(c.llm_model || 'deepseek-chat')
    }).catch(() => {})
  }, [])

  async function save() {
    setSaving(true)
    setMessage('')
    try {
      const res = await api.updateLlmConfig({
        api_key: apiKey || undefined,
        base_url: baseUrl || undefined,
        model: model || undefined,
      })
      setMessage(res.llm_configured ? '✅ 配置已保存，LLM 已启用' : '配置已保存（API Key 为空）')
      setCurrent(res)
      setApiKey('')
      props.onSaved && props.onSaved()
    } catch (e) {
      setMessage('❌ 保存失败：' + e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={props.onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>⚙ 设置 — 接入你的 LLM</h2>
          <button className="btn" onClick={props.onClose}>✕</button>
        </div>

        <div className="modal-body">
          <p className="muted">
            填写你自己的大模型 API Key，即可使用本系统生成真实学术内容。
            Key 仅保存在本机（<code>~/.academic_writer/config.json</code>），不会上传。
          </p>

          {current && (
            <div className="config-status">
              <span className="muted">当前状态：</span>
              {current.llm_configured
                ? <span className="tag tag-success">已配置（{current.llm_api_key_masked}）</span>
                : <span className="tag">未配置</span>}
            </div>
          )}

          <div className="field">
            <label>API Key（必填）</label>
            <input
              type="password"
              placeholder="sk-..."
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>

          <div className="field">
            <label>Base URL（可选，留空用服务商默认）</label>
            <input
              placeholder="https://api.deepseek.com/v1"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
            <p className="muted">
              常见：DeepSeek <code>https://api.deepseek.com/v1</code>；
              通义 <code>https://dashscope.aliyuncs.com/compatible-mode/v1</code>
            </p>
          </div>

          <div className="field">
            <label>模型名</label>
            <input
              placeholder="deepseek-chat"
              value={model}
              onChange={(e) => setModel(e.target.value)}
            />
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
