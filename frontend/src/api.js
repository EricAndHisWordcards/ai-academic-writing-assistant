// API 客户端封装
// 开发模式（Vite）用相对路径走 proxy；打包模式（file://）直连本地后端
const BASE = window.location.protocol === 'file:'
  ? 'http://127.0.0.1:8000/api'
  : '/api'

// 非 2xx 时后端一律给 {detail: "中文原因"}（400 的校验、404 的不存在、409 的
// 「正在生成正文」都走这条路），所以那句 detail 是**唯一**能说明白「为什么失败」
// 的东西，取不到才退回一句通用文案。
//
// 单独抽出来是因为两个上传入口原先各自把整个响应体丢掉了，只抛一句「上传失败」/
// 「材料上传失败」—— 而它们恰恰是最容易撞上 409 的两个入口（正文生成中不许改文献）。
// 三个入口共用这一份，消息口径就不会再各写各的。
async function errorDetail(res, fallback) {
  const err = await res.json().catch(() => ({ detail: res.statusText }))
  return err.detail || fallback
}

// timeoutMs 是**可选**的客户端上限，目前只有「执行引用调度」这一个调用传它。
// 为什么只有它：那条路由是同步的，界面在等它回执的整段时间里进度条一直在转 —— 而
// 服务端那次调用本来就有 120 秒上限（relevance_agent.REQUEST_TIMEOUT），所以正常
// 情况下 150 秒之内必然有回执；反过来，一条已经死掉的 TCP 路径（拔网线、代理半死）
// 不会自己报错，没有这个上限那条进度条会**永远转下去**，比没有进度条更假。
// 其余调用不传：它们要么本来就是后台任务（进度由轮询给，与这条请求无关），要么
// 快到不值得为一个假想的死连接加一层。（`fetch` 自己没有超时参数，只能这样罩。）
//
// 中止时抛的必须是**中文**：AbortError 的原文（"The operation was aborted"）会原样
// 进 run() 的错误横幅（见 App.jsx 的 run），那是给用户看的位置。
// timeoutMs 必须从 options 里摘出来（下面 ...options 是要交给 fetch 的），
// 否则它会作为一个未知字段进 init；清了定时器再抛，避免留下一个空转的 timer。
async function request(path, options = {}) {
  const { timeoutMs, ...init } = options
  const controller = timeoutMs ? new AbortController() : null
  const timer = controller
    ? setTimeout(() => controller.abort(), timeoutMs)
    : null
  try {
    const res = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
      ...(controller ? { signal: controller.signal } : {}),
    })
    if (!res.ok) throw new Error(await errorDetail(res, '请求失败'))
    return res.json()
  } catch (exc) {
    if (controller && controller.signal.aborted) {
      throw new Error(`请求超过 ${Math.round(timeoutMs / 1000)} 秒仍未返回，已中止，请重试`)
    }
    throw exc
  } finally {
    if (timer) clearTimeout(timer)
  }
}

export const api = {
  // 项目
  createProject: (title) =>
    request('/projects', { method: 'POST', body: JSON.stringify({ title }) }),
  listProjects: () => request('/projects'),
  getProject: (id) => request(`/projects/${id}`),
  deleteProject: (id) => request(`/projects/${id}`, { method: 'DELETE' }),
  // 改名（标题 = 论文标题）。空串是合法输入：含义是「跟随研究核心方向」
  renameProject: (id, title) =>
    request(`/projects/${id}/title`, { method: 'POST', body: JSON.stringify({ title }) }),

  // 选题
  recommendTopics: (id, body) =>
    request(`/projects/${id}/topics/recommend`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  setTopic: (id, body) =>
    request(`/projects/${id}/topic`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // 大纲
  generateOutline: (id) =>
    request(`/projects/${id}/outline/generate`, { method: 'POST' }),
  confirmOutline: (id, outline) =>
    request(`/projects/${id}/outline/confirm`, {
      method: 'POST',
      body: JSON.stringify({ outline }),
    }),

  // 文献主题聚类（仅文献综述：结构来自文献，故先聚类、人工确认后再出大纲）
  generateClusters: (id) =>
    request(`/projects/${id}/clusters/generate`, { method: 'POST' }),
  confirmClusters: (id, clusters, notes = '') =>
    request(`/projects/${id}/clusters/confirm`, {
      method: 'POST',
      body: JSON.stringify({ clusters, notes }),
    }),

  // 文献
  uploadDocuments: (id, files) => {
    const form = new FormData()
    for (const f of files) form.append('files', f)
    return fetch(`${BASE}/projects/${id}/documents`, {
      method: 'POST',
      body: form,
    }).then(async (r) => {
      if (!r.ok) throw new Error(await errorDetail(r, '上传失败'))
      return r.json()
    })
  },
  listDocuments: (id) => request(`/projects/${id}/documents`),
  // 删除必须说清「从哪个项目删」：后端拿路径里的项目 id 与文献行上的 project_id
  // 比对，不一致按 404 拒掉。这不是多余的装饰 —— 这个 doc id 来自界面上那份列表，
  // 而列表有可能还是上一个项目的（换项目时不是无条件重拉），只报 doc id 的话
  // 后端只能从文献行反推项目，于是「在 Y 的界面上点删除」会真删掉 X 的文献。
  deleteDocument: (projectId, docId) =>
    request(`/projects/${projectId}/documents/${docId}`, { method: 'DELETE' }),
  // 编辑一篇文献的元数据。**只提交改动过的字段**：后端的语义是「字段缺席 = 不修改、
  // 空串 = 清空」，提交整份表单会把没碰过的字段也一并写一遍 —— 那不仅多余，还会把
  // 界面上那份可能已经过期的副本盖回库里。
  updateDocument: (projectId, docId, fields) =>
    request(`/projects/${projectId}/documents/${docId}`, {
      method: 'PATCH',
      body: JSON.stringify(fields),
    }),

  // 作者自有研究材料（数据 / 成果 / 核心思路）。解析是纯本地文本提取，
  // 毫秒级完成，所以与文献不同：不走后台任务，直接同步返回。
  listMaterials: (id) => request(`/projects/${id}/materials`),
  uploadMaterials: (id, files) => {
    const form = new FormData()
    for (const f of files) form.append('files', f)
    return fetch(`${BASE}/projects/${id}/materials/files`, {
      method: 'POST',
      body: form,
    }).then(async (r) => {
      if (!r.ok) throw new Error(await errorDetail(r, '材料上传失败'))
      return r.json()
    })
  },
  addMaterialText: (id, body) =>
    request(`/projects/${id}/materials/text`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // 与 deleteDocument 同一道守卫：材料列表也可能是上一个项目的，路径带项目 id，
  // 后端按「不属于这个项目就 404」拦住跨项目删除（D4）。
  deleteMaterial: (projectId, materialId) =>
    request(`/projects/${projectId}/materials/${materialId}`, { method: 'DELETE' }),
  analyzeMaterials: (id) =>
    request(`/projects/${id}/materials/analyze`, { method: 'POST' }),

  // 一手内容录入：研究设计 / 质性设计 / 工程设计 / 理论模型 / 技术方案（按类型定字段名）
  saveDesign: (id, fields) =>
    request(`/projects/${id}/design`, {
      method: 'POST',
      body: JSON.stringify({ fields }),
    }),
  extractDesign: (id) =>
    request(`/projects/${id}/design/extract`, { method: 'POST' }),

  // 引用调度。**唯一**带客户端超时的调用，理由见 request 上面的注释：这条路由是
  // 同步的（最长 120 秒），而界面在整段时间里显示一条进度条 —— 150 秒的上限比服务端
  // 那个 120 秒留了 30 秒余量（含那次瞬时兜底），不可能误伤正常路径。
  scheduleCitations: (id) =>
    request(`/projects/${id}/citations/schedule`, { method: 'POST', timeoutMs: 150000 }),
  confirmCitations: (id, binding, citeStyle = 'bracket', citationFormat = 'gb7714') =>
    request(`/projects/${id}/citations/confirm`, {
      method: 'POST',
      body: JSON.stringify({ binding, cite_style: citeStyle, citation_format: citationFormat }),
    }),
  // 跳过引用调度：本文不产生角标与参考文献列表
  skipCitations: (id) =>
    request(`/projects/${id}/citations/skip`, { method: 'POST' }),
  // 按当前设置重排文末参考文献列表。零模型调用、不碰正文；后端只接受「编号与指向
  // 都没变、只有排版变了」这一种情形，其余一律 400（重排会把角标与列表错配出来）。
  rerenderReferences: (id) =>
    request(`/projects/${id}/references/rerender`, { method: 'POST' }),
  // 作废已生成的正文（连带文末列表基线与生成进度），保留大纲、引用绑定、设计与材料计划
  resetSections: (id) =>
    request(`/projects/${id}/sections/reset`, { method: 'POST' }),

  // 生成（触发生成后立即返回，正文由 progress 轮询取回）
  generate: (id) => request(`/projects/${id}/generate`, { method: 'POST' }),
  generateProgress: (id) => request(`/projects/${id}/generate/progress`),

  // 导出回执：Word/txt/Markdown 三种格式都在浏览器里拼（exporters.js），服务端一个
  // 字节都不参与 —— 这个接口只把「走完了终点」记进状态机（completed → exported）。
  // 幂等：导第二次原样返回成功。生成中会被 409 拒（那时该记的不是这一版正文）。
  markExported: (id) => request(`/projects/${id}/export`, { method: 'POST' }),

  // 通用后台任务进度（大纲生成 / 文献解析）
  taskProgress: (id) => request(`/projects/${id}/task/progress`),

  // 元信息
  meta: () => request('/meta'),

  // LLM 配置
  getLlmConfig: () => request('/config/llm'),
  updateLlmConfig: (body) =>
    request('/config/llm', { method: 'POST', body: JSON.stringify(body) }),
}
