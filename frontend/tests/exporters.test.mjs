// exporters.js 的 node:test 用例。它是纯 ES 模块（不含 React/DOM），改坏了只在
// 导出那一刻以「文件打不开 / 内容错乱」的形式暴露，pytest 读 App.jsx 文本钉不到。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import {
  buildMarkdown,
  buildTxt,
  buildDocx,
  buildBytes,
  formatOf,
  italicRpr,
  EXPORT_FORMATS,
} from '../src/exporters.js'

const doc = {
  title: '测试论文标题',
  sections: [
    { title: '第一章 引言', content: '第一段内容。\n\n第二段内容。' },
    { title: '第二章 正文', content: '正文内容。' },
  ],
  references: ['[1] 张三. 文献一[J]. 期刊, 2023.'],
}

test('buildMarkdown 输出标题、章节与参考文献', () => {
  const md = buildMarkdown(doc)
  assert.ok(md.startsWith('# 测试论文标题\n'))
  assert.ok(md.includes('## 第一章 引言\n'))
  assert.ok(md.includes('## 参考文献\n'))
  assert.ok(md.includes('[1] 张三. 文献一[J]. 期刊, 2023.'))
})

test('buildTxt 用 CRLF，且正文空行不产生空段落', () => {
  const txt = buildTxt(doc)
  assert.ok(txt.includes('\r\n'), '纯文本要 CRLF 换行（去处是 Windows 记事本/Word）')
  assert.ok(txt.startsWith('测试论文标题\r\n'))
  assert.ok(txt.includes('第一段内容。\r\n'))
  assert.ok(txt.includes('第二段内容。\r\n'))
  // paragraphs() 把空白行过滤掉：不会出现连续两个换行夹出空段
  assert.ok(!txt.includes('\r\n\r\n\r\n'), '不该有空段落')
})

test('buildDocx 返回以 PK 签名为头的合法 zip 字节', () => {
  const bytes = buildDocx(doc)
  assert.ok(bytes instanceof Uint8Array)
  assert.deepEqual([...bytes.slice(0, 4)], [0x50, 0x4b, 0x03, 0x04])
  const xml = new TextDecoder().decode(bytes)
  assert.ok(xml.includes('测试论文标题'))
  assert.ok(xml.includes('word/document.xml'))
})

test('buildDocx 转义 XML 特殊字符', () => {
  const dirty = { title: 'A & B <C>', sections: [], references: [] }
  const xml = new TextDecoder().decode(buildDocx(dirty))
  assert.ok(xml.includes('A &amp; B &lt;C&gt;'))
  assert.ok(!xml.includes('A & B <C>'), '原始 <C> 若留进 XML，Word 会报文件损坏')
})

test('buildBytes 按 key 分发，未知 key 报错', () => {
  assert.ok(buildBytes('md', doc) instanceof Uint8Array)
  assert.ok(buildBytes('txt', doc) instanceof Uint8Array)
  assert.ok(buildBytes('docx', doc) instanceof Uint8Array)
  assert.throws(() => buildBytes('nope', doc), /未知的导出格式/)
})

test('formatOf 找到对应格式', () => {
  assert.equal(formatOf('docx').ext, 'docx')
  assert.equal(formatOf('md').mime, 'text/markdown;charset=utf-8')
  assert.equal(formatOf('unknown'), undefined)
})

test('EXPORT_FORMATS 三种格式齐全且顺序稳定', () => {
  assert.deepEqual(EXPORT_FORMATS.map((f) => f.key), ['docx', 'txt', 'md'])
})

// ---------------------------------------------------------------- 写作语言

const docXml = (bytes) => new TextDecoder().decode(bytes)

test('英文排版与中文排版逐项不同', () => {
  const en = docXml(buildDocx(doc, 'en'))
  // 字体：三个属性全 Times New Roman（中文那套的 eastAsia 是宋体）
  assert.ok(en.includes('w:ascii="Times New Roman" w:hAnsi="Times New Roman"'
    + ' w:eastAsia="Times New Roman"'))
  assert.ok(!en.includes('宋体'), '英文排版里不该留着中文字体')
  // 字号全是 12 磅（24 半点）：标题/节标题/正文同号，靠加粗与居中分层次。
  // **逐个数**而不是只查「没有 32」，因为漏改一处不会报警、只会印出一行大一号的字。
  const sizes = [...en.matchAll(/<w:sz w:val="(\d+)"\/>/g)].map((m) => m[1])
  assert.equal(sizes.length, 8, '这 fixture 一共 8 个段落，每段一个 sz')
  assert.deepEqual([...new Set(sizes)], ['24'], '英文排版不分级，全是 12 磅')
  // 双倍行距
  assert.ok(en.includes('<w:spacing w:line="480" w:lineRule="auto"/>'))
  assert.ok(!en.includes('w:line="360"'), '1.5 倍行距是中文那套的')
  // 段首不缩进
  assert.ok(!en.includes('w:ind'), '英文段落靠段间距分界，没有首行缩进')
  assert.ok(!en.includes('firstLineChars'))
  // 四边 1 英寸
  assert.ok(en.includes('<w:pgMar w:top="1440" w:right="1440" w:bottom="1440"'
    + ' w:left="1440"'))
  // 文末那一节的标题词
  assert.ok(en.includes('References'))
  assert.ok(!en.includes('参考文献'), '产物文字跟着写作语言走')
})

test('中文排版本轮一字未动（sha256 哨兵）', () => {
  // 这三个值是**改动之前**的产物指纹。中文侧已经交付过，不该因为加了英文排版而变样；
  // 这条红了就说明 zh 那一套被顺手动了，此时要问的是「为什么非动不可」，
  // 而不是把新值抄进这里 —— 抄一遍等于把哨兵作废。
  const sha = (bytes) => createHash('sha256').update(bytes).digest('hex')
  assert.equal(sha(buildDocx(doc)), '1d2308f8a044c9455c01a390d1a53a0592c1944702957e1adaa772f996b7bfa8')
  assert.equal(sha(new TextEncoder().encode(buildMarkdown(doc))),
    '02907e80d5fe72358a20c22fa244c4c2348e658abb8b7d6ffee5e3fa48ff49ec')
  assert.equal(sha(new TextEncoder().encode(buildTxt(doc))),
    'a35f26bde9876a9690d0c43e1f2c7751837663d3a59cdc9105dc81e12e136b8e')
})

test('不传语言与显式传 "zh" 逐字节相同', () => {
  // 尾参默认 "zh" 的意义就在这里：既有调用点（以及本文件上面的全部断言）不用改一个字。
  assert.deepEqual(buildDocx(doc), buildDocx(doc, 'zh'))
  assert.equal(buildMarkdown(doc), buildMarkdown(doc, 'zh'))
  assert.equal(buildTxt(doc), buildTxt(doc, 'zh'))
  assert.deepEqual(buildBytes('docx', doc), buildBytes('docx', doc, 'zh'))
})

test('认不出的语言按中文渲染', () => {
  // 与后端 writing_lang.normalize 同一判据（精确匹配，否则中文）：连 en-US 这种
  // 看着像英文的地区标签也两边都落到中文 —— 前端不自己发明一套匹配规则。
  assert.equal(buildMarkdown(doc, 'en-US'), buildMarkdown(doc, 'zh'))
  assert.deepEqual(buildDocx(doc, 'fr'), buildDocx(doc, 'zh'))
})

test('buildMarkdown / buildTxt 的文末标题也按语言切换', () => {
  assert.ok(buildMarkdown(doc, 'en').includes('## References\n'))
  assert.ok(!buildMarkdown(doc, 'en').includes('参考文献'))
  assert.ok(buildTxt(doc, 'en').includes('References\r\n'))
  assert.ok(!buildTxt(doc, 'en').includes('参考文献'))
  // 英文项目里那一条文献（中文条目）内容照旧，只是节标题换了词
  assert.ok(buildMarkdown(doc, 'en').includes('[1] 张三. 文献一[J]. 期刊, 2023.'))
})

test('buildBytes 把语言透传到三种格式', () => {
  // buildBytes 是按 key 分发的那个，漏传语言不会报错、只会悄悄给一份中文排版的英文论文
  assert.ok(docXml(buildBytes('docx', doc, 'en')).includes('References'))
  assert.ok(!docXml(buildBytes('docx', doc, 'en')).includes('宋体'))
  assert.equal(new TextDecoder().decode(buildBytes('txt', doc, 'en')),
    buildTxt(doc, 'en'))
  assert.equal(new TextDecoder().decode(buildBytes('md', doc, 'en')),
    buildMarkdown(doc, 'en'))
})

// ---------------------------------------------------------------- 斜体片段

// 一条带斜体的参考文献：`[1] 张三. (2023). 题名. *教育研究, 32*, 56-75.`
// 斜体范围是后端算好放进片段里的（citation_format.reference_runs），前端只认
// `italic` 这个布尔值 —— 这里就照那个形状造一份。
const runsDoc = {
  title: 'T',
  sections: [],
  references: ['[1] 张三. (2023). 题名. 教育研究, 32, 56-75.'],
  referenceRuns: [[
    { text: '[1] ', italic: false },
    { text: '张三. (2023). 题名. ', italic: false },
    { text: '教育研究, 32', italic: true },
    { text: ', 56-75.', italic: false },
  ]],
}

test('只含正体片段的 referenceRuns 与整段一个 run 逐字节相同', () => {
  // 这条是前端"单一路径"的准入条件：归一化之后**只有一条渲染路径**，所以走片段
  // 与不走片段必须产出同一个字节。它红了就说明 wPara 里的分段/转义与改动前不同，
  // 而 zh 那份 sha256 哨兵也会同时红 —— 那时先看这条（它指得出是哪一段）。
  const viaRuns = buildDocx({
    ...doc,
    referenceRuns: doc.references.map((t) => [{ text: t, italic: false }]),
  })
  assert.deepEqual(viaRuns, buildDocx(doc))
})

test('缺席 / 长度对不上的 referenceRuns 一律退回整条正体，不抛', () => {
  // 兜底的三种形态：整块缺席、比 references 短、某一条是 null / 空数组。任何一种都
  // 不能让导出崩掉 —— 用户那一刻只想拿到文件，`references` 本身就是完整的著录串。
  // **用两条文献**：只有一条时"短一条"与"整块缺席"长得一样，回落那半条测不到。
  const two = { title: 'T', sections: [], references: ['[1] 甲.', '[2] 乙.'] }
  const bytes = (extra) => docXml(buildDocx({ ...two, ...extra }))
  const plain = docXml(buildDocx(two))

  // ① 整块缺席（旧快照、或调用方没给）→ 与"根本没有这个字段"逐字节相同
  assert.equal(bytes({}), plain)
  assert.ok(plain.includes('[1] 甲.') && plain.includes('[2] 乙.'))
  assert.ok(!plain.includes('<w:i/>'), '没拿到片段时不该凭空出现斜体')

  // ② 短一条：第 1 条拿到了就照它渲染，第 2 条**逐条**回落成 references 原文
  const short = bytes({ referenceRuns: [[{ text: 'A', italic: true }]] })
  assert.ok(short.includes('<w:i/>'), '拿到了片段的那一条要真的斜体')
  assert.ok(short.includes('>A</w:t>'))
  assert.ok(short.includes('[2] 乙.'), '没拿到片段的那一条要回落成原文')

  // ③ 某一条是 null / 空数组 → 那一条回落，其余不抛
  assert.equal(bytes({ referenceRuns: [null, []] }), plain)
})

test('斜体片段发成 <w:i/>，正体片段不带', () => {
  const xml = docXml(buildDocx(runsDoc))
  // 斜体那一段：rPr 是 bodyRpr + <w:i/>，且 <w:i/> 在 <w:sz 之前（schema 次序）
  assert.ok(
    xml.includes('<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"'
      + ' w:eastAsia="宋体"/><w:i/><w:sz w:val="24"/></w:rPr>'
      + '<w:t xml:space="preserve">教育研究, 32</w:t>'),
    '斜体那一段的 rPr 形态不对',
  )
  // 正体那几段不带 w:i —— 逐条数，漏掉一段（或整条都斜体）都能看出来
  const refPara = xml.split('<w:p>').find((p) => p.includes('教育研究, 32'))
  assert.equal([...refPara.matchAll(/<w:i\/>/g)].length, 1)
  assert.equal([...refPara.matchAll(/<w:r>/g)].length, 4, '四段各自一个 run')
})

test('斜体片段里的文本照旧转义', () => {
  // 分段之后每一段各自过 escXml：漏掉一遍就会让 `A & B` 原样进 XML，Word 报「文件已损坏」
  const xml = docXml(buildDocx({
    title: 'T',
    sections: [],
    references: ['A & B'],
    referenceRuns: [[{ text: 'A & ', italic: false }, { text: '<B>', italic: true }]],
  }))
  assert.ok(xml.includes('A &amp; '))
  assert.ok(xml.includes('<w:t xml:space="preserve">&lt;B&gt;</w:t>'))
  assert.ok(!xml.includes('A & B'))
})

test('italicRpr 把 <w:i/> 插在 <w:b/> 之后、<w:sz 之前', () => {
  // rPr 的子元素次序是 OOXML 的 schema 约束（rFonts → b → i → sz），错了 Word 可能
  // 报「内容有问题」。三套 rPr 都要验：参考文献那套没有 w:b，走 buildDocx 验不到
  // 「在 w:b 之后」那半条，所以这个函数单独导出来测。
  assert.equal(
    italicRpr('<w:rFonts w:ascii="A"/><w:b/><w:sz w:val="32"/>'),
    '<w:rFonts w:ascii="A"/><w:b/><w:i/><w:sz w:val="32"/>',
  )
  assert.equal(
    italicRpr('<w:rFonts w:ascii="A"/><w:sz w:val="24"/>'),
    '<w:rFonts w:ascii="A"/><w:i/><w:sz w:val="24"/>',
  )
  // 没有 w:sz 时补在末尾（w:b → w:i 本身也合规）
  assert.equal(italicRpr('<w:b/>'), '<w:b/><w:i/>')
})

test('md / txt 完全不读 referenceRuns（纯文本里斜体没有意义）', () => {
  // 对照必须拿**同一份文档去掉片段**来比（不是上面那个 doc —— 它另有一套标题与章节，
  // 那样比出来的差异与 referenceRuns 无关）
  const noRuns = { ...runsDoc, referenceRuns: undefined }
  assert.equal(buildMarkdown(runsDoc, 'zh'), buildMarkdown(noRuns, 'zh'))
  assert.equal(buildTxt(runsDoc, 'zh'), buildTxt(noRuns, 'zh'))
  // 反向：斜体不改字 —— 三种格式拿到的**文字**是同一份
  assert.ok(buildMarkdown(runsDoc).includes('[1] 张三. (2023). 题名. 教育研究, 32, 56-75.'))
})
