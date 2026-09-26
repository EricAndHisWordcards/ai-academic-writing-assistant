// wait-for-backend.cjs 的 node:test 用例。桌面版启动时靠它等后端就绪，坏掉的表现是
// 「后端起不来也照开窗口」或「起来得很慢也超时」，都是没人看日志才发现的静默缺陷。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const { waitForBackend } = require('../electron/wait-for-backend.cjs')

// 假 http：每调一次 get 就按脚本数组取一个动作，'ok' 模拟健康检查通过、'err' 模拟
// 连接失败。动作排空后固定复用最后一个。
function makeHttp(script) {
  let n = 0
  return {
    get(url, cb) {
      const action = script[Math.min(n, script.length - 1)]
      n++
      const req = {
        _err: null,
        on(ev, h) { if (ev === 'error') req._err = h },
        setTimeout() {},
        destroy() {},
      }
      if (action === 'ok') {
        queueMicrotask(() => cb({ resume() {} }))
      } else {
        queueMicrotask(() => req._err(new Error('ECONNREFUSED')))
      }
      return req
    },
  }
}

test('后端立刻可用时返回 true', async () => {
  const http = makeHttp(['ok'])
  const ok = await waitForBackend({ port: 8000, timeoutMs: 200, pollIntervalMs: 1, http })
  assert.equal(ok, true)
})

test('先失败后成功则重试并返回 true', async () => {
  const http = makeHttp(['err', 'ok'])
  const ok = await waitForBackend({ port: 8000, timeoutMs: 500, pollIntervalMs: 1, http })
  assert.equal(ok, true)
})

test('一直失败到超时返回 false', async () => {
  const http = makeHttp(['err'])
  const ok = await waitForBackend({ port: 8000, timeoutMs: 30, pollIntervalMs: 1, http })
  assert.equal(ok, false)
})
