// 导出成品的生成逻辑集中在这里（纯 JS，不含 React）。
//
// 三种格式共用同一份「导出文档」形状，由调用方拼一次：
//   { title: string, sections: [{ title, content }], references: [string],
//     referenceRuns?: [[{ text, italic }]] }
// 各格式自己再拼一遍标题与参考文献列表，就是三份迟早漂移的实现。
//
// `referenceRuns` 与 `references` **并排、逐条对应**，只有 docx 读它：txt / md 是
// 纯文本，斜体在那里没有意义（后端也仍然只按纯文本产出 `references`）。它缺席时
// docx 退回整条正体，三种格式都照旧能导出。
//
// **每个入口的尾参是写作语言**（`lang`，默认 `"zh"`）：中文论文与英文论文的排版惯例
// 不同（字体、行距、首行缩进、页边距、文末那一节叫「参考文献」还是 References）。
// 语言由调用方从 `project.writing_lang` 取（后端下发），这里不推导、不猜 ——
// 认不出的语言一律按中文渲染，与后端 `writing_lang.normalize` 同一判据。
// 尾参默认 `"zh"` 还保证了不传语言的老调用**逐字节不变**。
//
// 为什么 Word 是手写 OOXML 打包、而不是引一个库：这个前端只依赖 react/react-dom，
// 项目一贯不引额外依赖（后端解析 .docx/.xlsx 也是标准库直读 OOXML）。而 .docx 本身
// 就是「一个 zip 里放几份 XML」，用不压缩（store）方式写出来 Word / WPS 都能打开，
// 比 HTML 改名成 .doc 那种做法干净——后者新版 Word 会弹「文件格式与扩展名不匹配」。

export const EXPORT_FORMATS = [
  { key: 'docx', label: 'Word 文档（.docx）', ext: 'docx',
    mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
  { key: 'txt', label: '纯文本（.txt）', ext: 'txt', mime: 'text/plain;charset=utf-8' },
  { key: 'md', label: 'Markdown（.md）', ext: 'md', mime: 'text/markdown;charset=utf-8' },
]

// 文末那一节的标题词。三种格式共用一份：它是**产物文字**（会印在论文里），所以跟着
// 写作语言走 —— 一份英文论文的文末列表顶着「参考文献」四个汉字是不对的。
const REFERENCES_HEADING = { zh: '参考文献', en: 'References' }

/** 导出成品里文末那一节的标题词。**导出与界面预览共用它**：各写一个词，屏幕上就会
 *  出现「屏幕上写着参考文献、下载下来是 References」这种不一致。 */
export const referencesHeading = (lang) => REFERENCES_HEADING[lang] || REFERENCES_HEADING.zh

// ---------------------------------------------------------------- 公共零件

function paragraphs(text) {
  // 正文里的换行是段落分界；空行只是排版，不生成空段落
  return String(text ?? '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

function escXml(s) {
  // 先剔掉 XML 1.0 里非法的控制字符（模型偶发吐出 U+000B 这类），
  // 留着的话 Word 会直接报「文件已损坏」而不是显示得难看一点。
  //
  // 字符类里用 `\x..` 转义写、**不写原字符**：原先这里就是原样的控制字符，运行结果
  // 一模一样，但整个文件因此被当成二进制 —— ripgrep 直接跳过它（报 `No matches
  // found` 而不是内容），按文本读它的工具会半路截断。一行转义换回一份能检索的源码。
  return String(s ?? '')
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

// ---------------------------------------------------------------- Markdown

export function buildMarkdown(doc, lang = 'zh') {
  let md = `# ${doc.title}\n\n`
  for (const s of doc.sections) {
    md += `## ${s.title}\n\n${s.content}\n\n`
  }
  if (doc.references.length > 0) {
    md += `## ${referencesHeading(lang)}\n\n`
    for (const r of doc.references) md += `${r}\n`
    md += '\n'
  }
  return md
}

// ---------------------------------------------------------------- 纯文本

export function buildTxt(doc, lang = 'zh') {
  // 纯文本没有层级可用，靠空行分段；不用 `#`（那在 txt 里就是噪声）。
  // 换行一律 CRLF：这份文件的主要去处是 Windows 记事本与 Word。
  const lines = [doc.title, '']
  for (const s of doc.sections) {
    lines.push(s.title, '')
    for (const p of paragraphs(s.content)) lines.push(p, '')
  }
  if (doc.references.length > 0) {
    lines.push(referencesHeading(lang), '')
    for (const r of doc.references) lines.push(r)
    lines.push('')
  }
  return lines.join('\r\n')
}

// ---------------------------------------------------------------- Word (.docx)

const W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

// 排版按写作语言分两套。**zh 那一套是本轮之前逐字节的原文**，一个字都不许动 ——
// 它已经交付过，中文用户拿到的 .docx 不该因为加了英文排版而变样
// （`frontend/tests/exporters.test.mjs` 里有 sha256 哨兵盯着）。
//
// en 那一套按英文论文（APA 第 7 版）的惯例：正文 Times New Roman 12 磅、双倍行距、
// 段首不缩进、四边 1 英寸页边距。字号**只有 24**（12 磅）—— APA 的标题与节标题同样
// 是 12 磅（靠加粗与居中区分层级），不照搬中文那套 16/14 磅的落差。
//
// rPr / pPr 里的子元素顺序在 OOXML 里是有 schema 约束的（rFonts→b→sz、spacing→ind→jc），
// 顺序错了 Word 可能报「内容有问题」而不是静默忽略——两套都照 schema 的次序写。
const LAYOUT = {
  zh: {
    // 中文字体走 eastAsia、西文走 ascii/hAnsi：只设一边的话数字与英文会掉进另一种字体。
    font: '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/>',
    titleRpr: '<w:b/><w:sz w:val="32"/>',
    headRpr: '<w:b/><w:sz w:val="28"/>',
    bodyRpr: '<w:sz w:val="24"/>',
    titlePpr: '<w:spacing w:before="240" w:after="240"/><w:jc w:val="center"/>',
    headPpr: '<w:spacing w:before="240" w:after="120"/>',
    // 中文正文惯例：首行缩进 2 字符 + 1.5 倍行距。firstLineChars 是 Word 自己的「字符」
    // 单位（WPS 等渲染器不认），所以同时给一个等值的 firstLine（480 = 24 磅 = 两个字宽）。
    bodyPpr: '<w:spacing w:line="360" w:lineRule="auto"/>'
      + '<w:ind w:firstLineChars="200" w:firstLine="480"/>',
    // 参考文献条目不缩进（每条以 [1] 开头，缩进反而看不出层次）
    refPpr: '<w:spacing w:line="360" w:lineRule="auto"/>',
    // A4 纵向（11906×16838 缇）+ 中文论文常用的页边距（上下 2.54cm、左右 3.17cm）
    sect: '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
      + '<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800"'
      + ' w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>',
  },
  en: {
    font: '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"'
      + ' w:eastAsia="Times New Roman"/>',
    titleRpr: '<w:b/><w:sz w:val="24"/>',
    headRpr: '<w:b/><w:sz w:val="24"/>',
    bodyRpr: '<w:sz w:val="24"/>',
    titlePpr: '<w:spacing w:before="240" w:after="240"/><w:jc w:val="center"/>',
    headPpr: '<w:spacing w:before="240" w:after="120"/>',
    // 双倍行距（480 = 24 磅 × 2，lineRule=auto 表示按倍数解释），**没有 w:ind** ——
    // 英文段落靠段间距分界，段首缩进是中文（与部分中文学术刊物）的惯例。
    bodyPpr: '<w:spacing w:line="480" w:lineRule="auto"/>',
    refPpr: '<w:spacing w:line="480" w:lineRule="auto"/>',
    // 四边 1 英寸（1440 缇）；页眉页脚距用 Word 在 1 英寸页边距下的默认值（0.5 英寸）。
    sect: '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
      + '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
      + ' w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>',
  },
}

const layoutOf = (lang) => LAYOUT[lang] || LAYOUT.zh

// 一段正体文字（`wPara` 只认片段列表，这条是它的"整段正体"写法）。
const plain = (text) => [{ text, italic: false }]

// 把 `doc.referenceRuns` 归一化成"每条文献一串片段"，与 `doc.references` 逐条对应。
//
// **只在顶部归一化一次，下面只有一条渲染路径**：`doc.references` 缺席片段时（旧快照、
// 或调用方没传），这里退回"整条正体"—— 判据是**逐条**看 `runs[i]` 是不是一个非空
// 数组，而不是看整个 `doc.referenceRuns` 真假：后者在"两条文献只给了一条片段"时
// 会放行，那一条的长度对不上，`wPara` 就会把整条文献渲染成空段落。
function refRunsOf(doc) {
  const refs = doc.references || []
  const runs = doc.referenceRuns
  return refs.map((text, i) => (
    Array.isArray(runs?.[i]) && runs[i].length ? runs[i] : [{ text, italic: false }]
  ))
}

// rPr 里的子元素顺序在 OOXML 里是 schema 约束（rFonts → b → i → sz，见 LAYOUT 的注释），
// 顺序错了 Word 可能报「内容有问题」而不是静默忽略。所以斜体那份**不是**往 rPr 末尾
// 补一个 `<w:i/>`，而是插在 `<w:b/>` 之后、`<w:sz` 之前 —— 这两处相对次序才是 schema
// 要的那个。本文件里每一套 rPr 都以 `<w:sz .../>` 收尾（LAYOUT 的三对 *_Rpr 都是），
// 所以"插在 `<w:sz` 之前"就是正确位置。
// 导出**是给测试用的**：它是"斜体该插在哪儿"的唯一实现点，而 OOXML 的次序约束
// 只有对着三套 rPr 逐一套试才验得出来 —— 走 buildDocx 只能验到参考文献那一套
// （bodyRpr，没有 `w:b`），带 `w:b` 的那半条规则就永远没人碰。
export function italicRpr(rPr) {
  const at = rPr.indexOf('<w:sz')
  return at < 0 ? rPr + '<w:i/>' : rPr.slice(0, at) + '<w:i/>' + rPr.slice(at)
}

// runs: [{ text, italic }]。只含正体片段时，产出的 XML 与"整段一个 run"逐字节相同 ——
// 这是 docs/tests 里那条「单 run 片段 == 纯字符串路径」断言钉住的性质，也是 zh 那份
// sha256 哨兵能继续绿的原因。
function wPara(runs, rPr, pPr) {
  const inner = runs.map((r) => {
    const pr = r.italic ? italicRpr(rPr) : rPr
    return `<w:r><w:rPr>${pr}</w:rPr>`
      + `<w:t xml:space="preserve">${escXml(r.text)}</w:t></w:r>`
  }).join('')
  return `<w:p><w:pPr>${pPr}</w:pPr>${inner}</w:p>`
}

function documentXml(doc, lang) {
  const l = layoutOf(lang)
  const titleRpr = l.font + l.titleRpr
  const headRpr = l.font + l.headRpr
  const bodyRpr = l.font + l.bodyRpr
  const body = [wPara(plain(doc.title), titleRpr, l.titlePpr)]
  for (const s of doc.sections) {
    body.push(wPara(plain(s.title), headRpr, l.headPpr))
    for (const p of paragraphs(s.content)) body.push(wPara(plain(p), bodyRpr, l.bodyPpr))
  }
  if (doc.references.length > 0) {
    body.push(wPara(plain(referencesHeading(lang)), headRpr, l.headPpr))
    // 参考文献是**唯一**逐段判斜体的地方：APA 的刊名+卷号、MLA 的容器名由后端放在
    // 片段里（见 citation_format.reference_runs），前端只按 italic 布尔值发 run。
    for (const runs of refRunsOf(doc)) body.push(wPara(runs, bodyRpr, l.refPpr))
  }
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + `<w:document xmlns:w="${W_NS}"><w:body>${body.join('')}${l.sect}</w:body></w:document>`
}

const CONTENT_TYPES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
  + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
  + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
  + '<Default Extension="xml" ContentType="application/xml"/>'
  + '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
  + '</Types>'

const ROOT_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
  + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
  + '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
  + '</Relationships>'

export function buildDocx(doc, lang = 'zh') {
  return zipStore([
    { name: '[Content_Types].xml', text: CONTENT_TYPES },
    { name: '_rels/.rels', text: ROOT_RELS },
    { name: 'word/document.xml', text: documentXml(doc, lang) },
  ])
}

// —— 极简 zip：只写「不压缩」（store）条目，够 .docx 用。
// 手写而不用库，是因为只需要这一个功能；CRC32 与两处头部格式都不复杂，
// 但**顺序与长度必须严格正确**，所以下面每写一个字段就推进一次游标。

const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let i = 0; i < 256; i++) {
    let c = i
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[i] = c >>> 0
  }
  return table
})()

function crc32(bytes) {
  let c = 0xffffffff
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function zipStore(files) {
  const encoder = new TextEncoder()
  const chunks = []
  let offset = 0
  const put = (bytes) => { chunks.push(bytes); offset += bytes.length }
  const u16 = (v) => { const b = new Uint8Array(2); new DataView(b.buffer).setUint16(0, v, true); return b }
  const u32 = (v) => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, v, true); return b }

  // 固定时间戳（2020-01-01 00:00）而不是「现在」：同一份内容每次导出字节一致，
  // 出问题时可以直接比字节。Word 不关心这个字段。
  const dosTime = 0
  const dosDate = ((2020 - 1980) << 9) | (1 << 5) | 1

  const entries = files.map((f) => {
    const name = encoder.encode(f.name)
    const data = encoder.encode(f.text)
    return { name, data, crc: crc32(data), localOffset: 0 }
  })

  for (const e of entries) {
    e.localOffset = offset
    put(u32(0x04034b50))                 // 本地文件头签名
    put(u16(20))                         // 解压所需版本
    put(u16(0))                          // 标志位
    put(u16(0))                          // 压缩方法：0 = store
    put(u16(dosTime))
    put(u16(dosDate))
    put(u32(e.crc))
    put(u32(e.data.length))              // 压缩后大小 = 原始大小
    put(u32(e.data.length))
    put(u16(e.name.length))
    put(u16(0))                          // 扩展字段长度
    put(e.name)
    put(e.data)
  }

  const cdStart = offset
  for (const e of entries) {
    put(u32(0x02014b50))                 // 中央目录签名
    put(u16(20))                         // 创建者版本
    put(u16(20))                         // 解压所需版本
    put(u16(0))
    put(u16(0))
    put(u16(dosTime))
    put(u16(dosDate))
    put(u32(e.crc))
    put(u32(e.data.length))
    put(u32(e.data.length))
    put(u16(e.name.length))
    put(u16(0))                          // 扩展字段
    put(u16(0))                          // 注释
    put(u16(0))                          // 起始磁盘号
    put(u16(0))                          // 内部属性
    put(u32(0))                          // 外部属性
    put(u32(e.localOffset))
    put(e.name)
  }
  const cdSize = offset - cdStart

  put(u32(0x06054b50))                   // 中央目录结束记录
  put(u16(0))                            // 本磁盘号
  put(u16(0))                            // 中央目录起始磁盘号
  put(u16(entries.length))
  put(u16(entries.length))
  put(u32(cdSize))
  put(u32(cdStart))
  put(u16(0))                            // 注释长度

  const out = new Uint8Array(offset)
  let pos = 0
  for (const c of chunks) { out.set(c, pos); pos += c.length }
  return out
}

// ---------------------------------------------------------------- 下载

export function buildBytes(key, doc, lang = 'zh') {
  if (key === 'docx') return buildDocx(doc, lang)
  if (key === 'txt') return new TextEncoder().encode(buildTxt(doc, lang))
  if (key === 'md') return new TextEncoder().encode(buildMarkdown(doc, lang))
  throw new Error(`未知的导出格式：${key}`)
}

export function formatOf(key) {
  return EXPORT_FORMATS.find((f) => f.key === key)
}

/** 生成并下载。baseName 由调用方清洗好（见 App.jsx 的 safeFilename）。 */
export function exportDocument(key, doc, baseName, lang = 'zh') {
  const format = formatOf(key)
  const bytes = buildBytes(key, doc, lang)
  const url = URL.createObjectURL(new Blob([bytes], { type: format.mime }))
  const a = document.createElement('a')
  a.href = url
  a.download = `${baseName}.${format.ext}`
  a.click()
  URL.revokeObjectURL(url)
}
