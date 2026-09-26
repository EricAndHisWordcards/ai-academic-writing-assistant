// 从 App.jsx 拆分而来（v1.32）。函数体与注释逐字节保留。

import { useEffect, useState } from 'react'
import { WorkingBar } from '../components/WorkingBar'
import { wordsNumber } from '../helpers.js'

export function TopicStep(props) {
  // 「目标总字数」这个框此前是**直接绑在数字状态上**的受控输入：
  //   value={props.targetWords} onChange={(e) => props.setTargetWords(parseInt(e.target.value) || 0)}
  // parseInt('') 是 NaN，`|| 0` 又把它落成 0 —— 于是框里永远至少挂着一个 "0"。
  // 用户全选删掉，框里立刻回到 "0"、光标停在它旁边：接着敲的数字要么接在 0 后面
  // （框里显示 "03000"），要么插在 0 前面变成 "30000" —— 后者是个**静默的错值**，
  // 目标字数从 3000 变成 3 万，用户看不出来。想改这个数就只有先删、再想办法把光标
  // 挪回去这一条别扭的路。
  //
  // 修法是把「用户正在敲的那串字符」与「库里那个数字」各归各位：框里放草稿文本，
  // 数字仍由父层持有。**不能顺手把父层的 targetWords 改成字符串** —— 大纲步那句
  // `total === props.targetWords` 会因此恒假（数字永远不等于字符串），确认大纲的
  // 按钮就此永久禁用，而界面上不会有任何报错。草稿留在这一层即可：它是纯粹的
  // 输入中间态，父层那个数字才是唯一真相。
  const [wordsDraft, setWordsDraft] = useState(
    props.targetWords ? String(props.targetWords) : '',
  )
  // 回灌只在「两边归一到不同的数字」时发生，正常是「切换项目」那一路（8000 → 3000）。
  // 判据必须与 onChange 用同一个 wordsNumber，否则 "1000.5" 这类中间态会被判成不同、
  // 在用户手底下把草稿改掉；而清空时草稿是 ''、父层是 0，两边都归一到 0 —— 于是
  // 刚被删掉的 "0" 不会被塞回来，那次回灌正是原来那个毛病本身。
  useEffect(() => {
    if (wordsNumber(props.targetWords) !== wordsNumber(wordsDraft)) {
      setWordsDraft(props.targetWords ? String(props.targetWords) : '')
    }
  }, [props.targetWords])
  // 按过按钮之后的拒绝语，就显示在按钮正上方。挂在这一层而不是走 App 级那条
  // error-banner：那条横幅在**步骤卡片之上**，而这张卡片很高（选题列表 + 两个大
  // 文本框），用户按的按钮在卡片最下面、横幅多半在视野之外 —— 那就成了「按了没反应」，
  // 正是这个项目一路在消灭的那类静默。生成步的 `failed && <div className="error-banner">`
  // 就是这个位置、这个写法。
  const [wordsRefused, setWordsRefused] = useState(false)
  const wordsTooFew = wordsNumber(props.targetWords) < props.minWords

  function submitTopic() {
    // 这道检查的职责是**把话说在按钮旁边**，不是闸门：真正的闸门是后端 set_topic 里
    // 那道同值的 400（前端这份若过期，用户也只是多等一次往返、消息落到顶部横幅里）。
    // 所以这里**不**复制一份阈值，只读下发下来的 props.minWords。
    if (wordsTooFew) {
      setWordsRefused(true)
      return
    }
    setWordsRefused(false)
    props.onConfirm()
  }

  return (
    <div className="card">
      <h2>{props.stepNo}. 选题与设定</h2>
      <div className="form-grid">
        <div className="field">
          <label>论文类型</label>
          {/* value 是短名（库里存的那一个），显示的是带适用范围的长名 ——
              用户靠括号里的「社科、经管、医学」判断该选哪个，靠短名落库。 */}
          <select value={props.paperType} onChange={(e) => props.setPaperType(e.target.value)}>
            {props.paperTypes.map((t) => (
              <option key={t} value={t}>{props.typeLabels[t] || t}</option>
            ))}
          </select>
          <p className="muted">
            大纲结构随类型变化。
            {props.typeConfig[props.paperType]?.flow_hint
              ? `本类型的写作主线：${props.typeConfig[props.paperType].flow_hint}。`
              : ''}
          </p>
        </div>
        <div className="field">
          <label>写作语言</label>
          {/* 它决定产出物用哪种语言（论文标题/摘要/正文/文末列表），界面文字仍是中文。
              选项由 meta 下发的表派生，与后端 writing_lang.SUPPORTED_LANGS 同源。 */}
          <select
            value={props.writingLang}
            onChange={(e) => props.setWritingLang(e.target.value)}
          >
            {props.writingLangs.map((l) => (
              <option key={l} value={l}>{props.langLabels[l] || l}</option>
            ))}
          </select>
          {/* 「能选哪些」与「排除掉的为什么不在」两句都必须由**同一个数据源**现算：
              前者是 formats（= 后端 citation_formats_by_lang[该语言]），后者是
              allFormats 减掉 formats、再逐个取后端下发的 formatNotes。原先后半句是前端
              手写的一句 `writingLang === 'en' ? 'GB/T 7714 是中文期刊的著录标准…'`——
              同一个事实判了两遍，而且手写的那句在后端改表之后就会变成假话，
              看起来却完全正常。 */}
          <p className="muted">
            论文标题、摘要、关键词、正文与文末参考文献列表按它撰写（界面文字仍是中文）。
            参考文献格式可选：
            {props.formats.map((f) => props.formatLabels[f] || f).join('、')}。
            {props.allFormats
              .filter((f) => !props.formats.includes(f))
              .map((f) => props.formatNotes[f])
              .filter(Boolean)
              .join('')}
          </p>
        </div>
        <div className="field">
          <label>目标总字数</label>
          <input
            type="number"
            min={props.minWords}
            value={wordsDraft}
            onChange={(e) => {
              const raw = e.target.value
              // 草稿存原样的字符（含空串 —— 这正是要修的那件事），数字仍交给父层。
              setWordsDraft(raw)
              props.setTargetWords(wordsNumber(raw))
              // 改过了就把上一句拒绝收回去：它说的是改之前那个值，挂在按钮上方会
              // 让用户以为改完还是不行。仍不够就再按一次按钮，话会照原样再说一遍。
              setWordsRefused(false)
            }}
          />
          {/* 单位不是文案问题：英文论文的字数口径是**词**，后端 count_units 也按词数。
              不显示的后果很具体 —— 用户给英文论文填「5000 字」，产出的是 5000 个词
              （约等于中文七千多字的体量），他拿到手才发现差了两倍。 */}
          <p className="muted">
            最少 {props.minWords} {props.wordsUnit}，少于这个数不能进入下一步。
            它是全篇总量，到「大纲与字数」步按节分配。
          </p>
        </div>
        <div className="field field-full">
          <label>研究领域（用于选题推荐）</label>
          <input
            placeholder="例如：人工智能、气候变化、教育公平..."
            value={props.domain}
            onChange={(e) => props.setDomain(e.target.value)}
          />
        </div>
      </div>

      <div className="actions">
        {/* 有服务端任务在跑时禁掉：推荐把进度写进同一个 task 槽位，跑完还会
            setTask(null)，会把正在轮询的服务端任务状态一起清掉（进度条凭空消失）。
            字数不足时也禁掉：这个框就是「输入先校验再消费」——推荐的 match 评分
            是拿目标字数当基准评的，把一个刚被提示「不许用」的数字喂进去，只会
            换回一份按 0 字评的推荐，而界面上看不出来。两个理由都要说给用户听，
            所以 title 按顺序取第一条成立的。 */}
        <button
          className="btn"
          onClick={props.onRecommend}
          disabled={props.loading || !props.domain || props.taskRunning || wordsTooFew}
          title={
            props.taskRunning ? '有任务正在进行，等它结束后再试'
              : wordsTooFew ? `先把目标总字数填到 ${props.minWords} ${props.wordsUnit} 以上：推荐的匹配度是拿这个字数评的`
                : undefined
          }
        >
          AI 推荐选题
        </button>
        {props.task?.status === 'running' && props.task.kind === 'topics' && (
          <WorkingBar
            message={props.task.message}
            startedAt={props.task.started_at}
          />
        )}
      </div>

      {props.topics.length > 0 && (
        <div className="topic-list">
          {props.topics.map((t, i) => (
            <div
              key={i}
              className={`topic-card ${props.topic === t.title ? 'selected' : ''}`}
              onClick={() => props.setTopic(t.title)}
            >
              <div className="topic-title">{t.title}</div>
              <div className="muted">研究问题：{t.question}</div>
              <div className="muted">可行性：{t.feasibility}</div>
              {/* 匹配度是模型自己给的，可能没有；兜底选题（未配置 LLM）也不给 ——
                  它没跟任何东西比对过。有才显示，否则会印出「匹配度 undefined」。 */}
              {t.match != null && <span className="tag">匹配度 {t.match}</span>}
            </div>
          ))}
        </div>
      )}

      <div className="field field-full">
        <label>确定研究核心方向</label>
        <textarea
          rows={3}
          placeholder="可手动填写或从上方推荐选题中选择"
          value={props.topic}
          onChange={(e) => props.setTopic(e.target.value)}
        />
      </div>

      {/* 这一步此前只有一个标题输入框，而一个标题装不下「打算怎么论证」。这个框喂两处：
          大纲第一遍（模型据此收敛 core_question，而不是自己替作者定主线）与每一节正文。
          上面那句「确定研究核心方向」是**标题**，模型只能从它反推主线；下面这段是作者的
          原话，两者冲突时以它为准（优先级写在后端块里，不在这里）。
          必须与「研究材料」区分开：材料在结构上**永远进不了大纲**（材料分析的前置条件
          就是大纲已存在，它只经 materials_plan_json 进正文），而粘贴材料的默认标签恰好
          就叫「粘贴的研究思路」——用户把论证思路写进那里，大纲会无声地忽略它。 */}
      <div className="field field-full">
        <label>写作思路与论证思路（选填）</label>
        <textarea
          rows={4}
          placeholder="例：先辨析 A 与 B 两个概念，再从机制层面论证二者是互补而非替代，最后用近五年的行业数据说明这种互补在什么条件下失效"
          value={props.writingIdeas}
          onChange={(e) => props.setWritingIdeas(e.target.value)}
        />
        <p className="muted">
          写你打算怎么论证，不是论文正文。它会进大纲，也会进每一节的正文；
          与模型自己的推断有出入时以你写的为准。
          想交数据、成果或成段的素材，请到「文献注入」步用「研究材料」——
          那一类只进正文，不会影响大纲。
        </p>
      </div>

      {/* 这个 busy 与上面「AI 推荐选题」的 taskRunning 是两把尺子，刻意不合并：
          那个管的是**本地进度槽位**（推荐把进度写进同一个 task 槽、跑完 setTask(null)，
          会把正在轮询的服务端任务状态一起清掉），这个管的是后端那两道忙闲闸 ——
          set_topic 在任一登记在跑时都 409（「改了也白改」：在飞的任务收尾时会把产物
          写回库，盖掉这次作废）。前者是后者的子集，但理由不同，各留各的。 */}
      {wordsRefused && (
        <div className="error-banner">
          目标总字数最少 {props.minWords} {props.wordsUnit}，当前
          {' '}{wordsNumber(props.targetWords)} {props.wordsUnit}。
          请先在上面的「目标总字数」里补足再进入下一步。
        </div>
      )}
      <div className="actions">
        {/* 字数不足时**不禁用**这个按钮，而是让它按下去把原因说出来：禁用一个主按钮
            等于什么都不说，用户只会看到一个点不动的按钮（这道工序里已经因为「按钮
            该亮不亮、该说不说」修过一轮）。所以判据只体现在 onClick 与文案上。 */}
        <button
          className="btn btn-primary"
          onClick={submitTopic}
          disabled={props.loading || !props.topic || props.busy}
        >
          确认选题，进入下一步
        </button>
      </div>
    </div>
  )
}
