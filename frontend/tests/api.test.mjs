// api.js 的 node:test 用例。它在模块加载时读 window.location.protocol 定 BASE，又用
// fetch 发请求，所以要在 import 前先铺好 window、再注入假 fetch。它把「非 2xx 时抛
// 后端的 detail」这条唯一能把失败说清的口径钉住，pytest 读源码文本钉不到。
import { test } from 'node:test'
import assert from 'node:assert/strict'

// http: 协议 → BASE='/api'（走 Vite 代理）。必须在 import 之前就位。
globalThis.window = { location: { protocol: 'http:' } }
const { api } = await import('../src/api.js')

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

function mockFetch(handler) {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return handler(url, options)
  }
  return calls
}

test('request 把路径拼到 BASE 上并透传 method/body', async () => {
  const calls = mockFetch(() => jsonResponse(200, { id: 'abc' }))
  const p = await api.getProject('abc')
  assert.deepEqual(p, { id: 'abc' })
  assert.equal(calls[0].url, '/api/projects/abc')
  assert.deepEqual(calls[0].options.headers, { 'Content-Type': 'application/json' })
})

test('POST 会把 body 序列化成 JSON 并带 method', async () => {
  const calls = mockFetch(() => jsonResponse(200, { ok: true }))
  await api.setTopic('id', { paper_type: 'x', topic: 't' })
  assert.equal(calls[0].url, '/api/projects/id/topic')
  assert.equal(calls[0].options.method, 'POST')
  assert.equal(calls[0].options.body, JSON.stringify({ paper_type: 'x', topic: 't' }))
})

test('非 2xx 时把后端的 detail 当成错误消息抛出', async () => {
  mockFetch(() => jsonResponse(409, { detail: '正文生成中，不能改文献' }))
  await assert.rejects(api.deleteDocument('p1', 'd1'), /正文生成中，不能改文献/)
})

test('响应没有 detail 时退回一句通用文案', async () => {
  mockFetch(() => jsonResponse(500, {}))
  await assert.rejects(api.scheduleCitations('p1'), /请求失败/)
})

test('updateDocument 用 PATCH，且只发传进去的那几个字段', async () => {
  const calls = mockFetch(() => jsonResponse(200, { ok: true }))
  await api.updateDocument('p1', 'd1', { volume: '32', issue: '1' })
  assert.equal(calls[0].url, '/api/projects/p1/documents/d1')
  assert.equal(calls[0].options.method, 'PATCH')
  // 后端的语义是「字段缺席 = 不修改这一项、空串 = 清空」。发整份表单会把没碰过的
  // 字段也写一遍 —— 那些值来自界面上那份副本，可能是旧的。
  assert.equal(calls[0].options.body, JSON.stringify({ volume: '32', issue: '1' }))
})

test('updateDocument 的 409 要把后端的 detail 抛出来（编辑框要显示原因）', async () => {
  mockFetch(() => jsonResponse(409, { detail: '正文生成中，无法编辑文献' }))
  await assert.rejects(
    api.updateDocument('p1', 'd1', { title: 'x' }),
    /正文生成中，无法编辑文献/,
  )
})

test('上传走 FormData，不手工设 Content-Type', async () => {
  const calls = mockFetch(() => jsonResponse(200, { ok: true }))
  const file = new Blob(['content'], { type: 'application/pdf' })
  await api.uploadDocuments('p1', [file])
  assert.equal(calls[0].url, '/api/projects/p1/documents')
  assert.equal(calls[0].options.method, 'POST')
  assert.ok(calls[0].options.body instanceof FormData)
  assert.equal(calls[0].options.headers, undefined, '边界由浏览器按 FormData 拼，不能手设')
})

test('打包模式（file://）直连本地后端 127.0.0.1:8000', async () => {
  const prev = globalThis.window.location.protocol
  globalThis.window.location.protocol = 'file:'
  const mod = await import(`../src/api.js?file-${Date.now()}`)
  globalThis.window.location.protocol = prev

  const calls = mockFetch(() => jsonResponse(200, { ok: true }))
  await mod.api.meta()
  assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/meta')
})
