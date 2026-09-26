// 等待本地后端就绪：轮询 /api/health，直到首次成功响应或超时。
//
// 从 main.cjs 里抽出来，纯粹为了能测 —— main.cjs 顶层 `require('electron')` 在普通
// node 下根本 import 不进来（electron 的 app/BrowserWindow 只在 Electron 运行时存在），
// 抽成不含 electron 的纯 CommonJS 模块后，node:test 才能直接驱动 waitForBackend 本身。
// http / port / 间隔都做成可注入，测试里塞一个假 http.get 就能覆盖「立刻成功 /
// 先失败后成功 / 一直失败到超时」三条路径。
'use strict'

function waitForBackend({
  port = 8000,
  timeoutMs = 30000,
  pollIntervalMs = 500,
  http = require('http'),
} = {}) {
  const start = Date.now()
  return (async () => {
    while (Date.now() - start < timeoutMs) {
      try {
        await new Promise((resolve, reject) => {
          const req = http.get(`http://127.0.0.1:${port}/api/health`, (res) => {
            res.resume()
            resolve()
          })
          req.on('error', reject)
          req.setTimeout(1000, () => { req.destroy(); reject(new Error('timeout')) })
        })
        return true
      } catch (e) {
        await new Promise((r) => setTimeout(r, pollIntervalMs))
      }
    }
    return false
  })()
}

module.exports = { waitForBackend }
