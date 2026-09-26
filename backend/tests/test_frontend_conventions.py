"""测试：前端界面上的三类约定（主按钮唯一 / 横幅不猜原因 / 数字输入的中间态）。

这里的对象既不是后端数据也不是 API 契约，而是**界面上的话、样式与输入行为**——它们没有
别的地方能钉住：前端进不了 pytest（没有 JS 测试运行器），而这些约定的失效方式恰恰都是
**静默**的：多一个蓝按钮不会报错、多一句假原因也不会报错、框里多一个删不掉的 "0" 也不会
报错，只会让用户按错顺序点、按一句假的原因去理解自己的项目状态、或者把一个数字敲成
十倍。所以照 test_frontend_mirror.py 的既有做法，把 App.jsx 当文本读。

前两条来自已记档的决策（PRD 决策 22 / 4.5 的界面安排），不是本轮新发明的：
主按钮 = 你现在该点的那一个，任何时刻恰好一个；横幅讲的是"此刻仍然生效的事实"，
措辞不猜原因。第三条是 v1.24 的：受控的数字输入框里，"用户正在敲的那串字符"与
"父层持有的那个数字"必须分开存——合在一起就没法表达"空"。

后面陆续添进来的几组同样是"界面上的约定"：选项不许手写（派生自后端下发的表）、
导出排版语言只读项目那一列、以及文件末尾那组**渲染期异常的兜底**（挂载点各在哪、
复位判据只有一处、全树只有一个文件允许定义兜底钩子）。
"""
import re
from pathlib import Path

import pytest

APP_JSX = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx"
APP_CSS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.css"


def _code_only(src: str) -> str:
    """去掉注释后的源码。

    断言必须只看**代码**。这个项目的注释里有大量"这里曾经说过一句假话"式的记档 ——
    修掉的那句原文往往被逐字留在注释里，好让下一个人知道它为什么被改掉。把它也算进
    断言范围，等于让"记录历史"和"复现缺陷"在测试里长得一模一样。
    """
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.S)   # JSX 注释 {/* ... */}
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)        # 块注释
    src = re.sub(r"^[ \t]*//.*$", "", src, flags=re.M)     # 整行的行注释
    return src


@pytest.fixture(scope="module")
def jsx() -> str:
    assert APP_JSX.exists(), f"找不到前端源码：{APP_JSX}"
    return _code_only(APP_JSX.read_text(encoding="utf-8"))


def _function_body(jsx: str, name: str) -> str:
    """取出一个顶层函数的函数体（从 `function name(` 到第一个顶格的 `}`)。

    不解析 JS：这两条断言要的是"这些字出没出现在这段代码里"，为此引一个 JS 解析器
    不划算，也不该让测试跟着前端语法变。
    """
    start = jsx.index(f"function {name}(")
    end = jsx.index("\n}\n", start)
    return jsx[start:end]


def _app_method(jsx: str, name: str) -> str:
    """取出 App 组件里一个方法的函数体（从 `function name(` 到下一个同级方法为止）。

    不能用 _function_body：那些方法缩进在 App 内部（两格），函数体里全是同样缩进的
    `}`，靠"顶格的 }"找不到它们的结尾 —— 而**切到下一个方法为止**是可判的：下一个
    两格缩进的 function 定义就是边界。此前有几处手写 `jsx.index("...(下一个方法名)")`
    当右边界，那种写法在两者之间插进一个新方法时会**静默**把新方法的代码也圈进来
    （test_delete_document_notices_do_not_overwrite 就这么红过一次）。
    """
    start = jsx.index(f"function {name}(")
    nxt = re.search(r"\n  (?:async )?function \w+\(", jsx[start:])
    return jsx[start:start + nxt.start()] if nxt else jsx[start:]


def test_outline_card_has_no_hardcoded_primary_button(jsx):
    """大纲步的卡片里不许有写死的主按钮样式。

    这张卡片上并排着**前后两个阶段的出口**——文献综述多出的「确认主题聚类」，以及
    「确认大纲（人工确认点①）」——外加一个「生成大纲」。三者谁是主按钮必须由**一处**
    判据给出（`clustersNeedConfirm` 分出的三个互斥布尔）：各写各的，综述的卡片上就会
    并排出现两个蓝按钮（此前正是如此），而这两个动作的后果差一个量级——聚类确认只是
    盖章（非破坏性），大纲确认会作废引用绑定与材料计划并把流程推进一步，用户无法从
    界面判断先后。
    """
    bodies = {name: _function_body(jsx, name) for name in ("OutlineStep", "ClusterPanel")}

    # 写死的样子是 `className="btn btn-primary"`。条件式里那个 `'btn btn-primary'`
    # 是**该出现**的——它就是"按判据取样式"本身——所以盯的是前者（多了 `="` 那两个字符）。
    for name, body in bodies.items():
        assert 'className="btn btn-primary"' not in body, (
            f"{name} 里有一个写死的主按钮样式：大纲卡片的三个按钮必须各按同一条判据取样式"
        )

    # 反过来对一次账：有条件的必须恰好三个（聚类确认 / 生成大纲 / 确认大纲）。少一个
    # 说明某个按钮退回了写死，多一个说明有人新加了一个按钮却没进这条判据。
    joined = "\n".join(bodies.values())
    assert joined.count("? 'btn btn-primary' : 'btn'") == 3


def test_clusters_need_confirm_is_the_single_judgement(jsx):
    """判据只有一处：1 处定义 + 3 处使用（聚类确认 / 生成大纲 / 确认大纲）。

    数量本身就是断言。判据被抄成第二份，就意味着有人在自己那一格里重新推一遍"现在该
    点哪一个"——这正是"同一张卡片两把尺子"的起点。多一处少一处都该在这里被看见。
    """
    assert jsx.count("clustersNeedConfirm") == 4, (
        "clustersNeedConfirm 应为 4 处（1 处定义 + 3 处使用），"
        "数量变了说明判据被拆开或漏用"
    )


def test_citation_banner_does_not_assert_a_cause(jsx):
    """引用调度步那条琥珀条不得再断言「绑定是在上一次调度时定下的…」。

    那句话只在"调度之后又加了文献"这一种情形下成立。会落到这条横幅上的至少还有三种
    情形——从没调度过、跳过过引用、调度之后删过文献或改过章节标题——它们**根本没有
    "上一次调度"**，两句话都是假的，而横幅的措辞会被用户当成自己项目状态的解释。
    修法照同卡片里材料那条的写法：并列穷举、不指认是哪一种。
    """
    for stale in ("绑定是在上一次调度时定下的", "此后加进来的文献不在其中"):
        assert stale not in jsx, f"这句断言只在一种情形下成立，不该出现在横幅里：{stale}"

    # 正面的一半：横幅必须仍然无条件给出补救动作（条件消失时它自己会消失，所以
    # "不猜原因"不等于"什么都不说"）。
    banner = jsx[jsx.index("有 {props.unboundDocuments} 篇文献还没有绑定到任何章节"):]
    assert "执行引用调度" in banner[:400]


def test_stale_banner_keeps_its_remedy_line(jsx):
    """两条横幅都必须点名补救动作所在的那一步与按钮名（v1.13 起的口径）。

    没有地址的"回上一步"对用户不是地址。材料那条的补救不在本页，所以它的措辞更要紧；
    这条断言把它盯住，免得某次改写把目的地写没了。
    """
    materials = jsx[jsx.index("有 {props.materialsCount} 份研究材料还没有分析"):]
    assert "「文献注入」步" in materials[:400]
    assert "「分析材料如何融入正文」" in materials[:400]
    assert re.search(r"重排引用对材料(没有任何作用|毫无作用)", materials[:400])


def test_target_words_input_keeps_a_text_draft(jsx):
    """「目标总字数」框绑的必须是草稿文本，不能直接绑那个数字。

    直接绑数字时这个受控框**永远至少挂着一个 "0"**：onChange 里的 parseInt('') 是
    NaN，`|| 0` 又把它落成 0。用户全选删掉，框里立刻回到 "0"、光标停在它旁边，接着
    敲的数字要么接在 0 后面（显示成 "03000"），要么插在 0 前面变成 "30000" —— 后者
    是个**静默的错值**：目标字数从 3000 变成 3 万，界面上没有任何东西会报错。
    """
    body = _function_body(jsx, "TopicStep")
    assert "value={wordsDraft}" in body
    assert "value={props.targetWords}" not in body, "框里要放草稿，数字由父层持有"
    # 阈值只从 props 来。前端写死一个数字，就等于让界面能说出一个后端并不认的下限
    # （用户照着它填，照样被 400 挡回来）—— 那个数字只该由 /api/meta 下发。
    assert "1000" not in body, "下限必须读 props.minWords，不能在前端写死"


def test_topic_confirm_button_explains_instead_of_disabling(jsx):
    """字数不足时按按钮要**给出原因**，而不是把按钮禁掉。

    禁用一个主按钮等于什么都不说：用户只看到一个点不动的按钮，不知道该改哪个框。
    所以判据只落在 onClick 与按钮正上方那条横幅上，disabled 里不许出现 wordsTooFew。
    横幅的位置也是断言的一部分 —— App 级那条 error-banner 在**步骤卡片之上**，而这张
    卡片很高（选题列表 + 两个大文本框），用户按的按钮在卡片最下面，横幅多半在视野之外，
    那就成了"按了没反应"。
    """
    body = _function_body(jsx, "TopicStep")
    assert "onClick={submitTopic}" in body
    assert "disabled={props.loading || !props.topic || props.busy}" in body, \
        "disabled 里不许混进字数判据"
    assert body.index("wordsRefused &&") < body.index("onClick={submitTopic}"), \
        "拒绝语必须出现在确认按钮之前"


def test_target_words_is_always_sent_as_a_number(jsx):
    """两个发送点发的都是 `wordsNumber(...)` 归一后的数字。

    草稿可以是空串（那正是要修的东西），数字不行：后端的 target_words 是 int，
    空串会被 pydantic 判成 422，而前端 api.js 的 `new Error(err.detail)` 会把 detail
    数组渲染成 [object Object] —— 用户看到的是一个没有内容的错误。
    """
    for name in ("recommendTopics", "confirmTopic"):
        body = _function_body(jsx, name)
        assert "target_words: wordsNumber(targetWords)" in body, f"{name} 发出去的不是数字"


def test_generation_interrupted_restores_sections_instead_of_clearing(jsx):
    """生成被服务重启打断（interrupted）时，不许清空界面上的章节，要从库里恢复。

    D5：轮询收到 interrupted 时，旧代码走 `setSections(res.sections || null)` —— 后端
    只在 done/completed 时才回传正文，interrupted 不带 sections，于是这一行把界面上
    已生成的章节整块清空，导出步随即改口说「尚无正文」，而库里其实还有逐节落盘的章节。
    修法是让 interrupted 分支用库里的 sections_json / references_json 恢复。
    """
    start = jsx.index("res.status === 'interrupted'")
    end = jsx.index("} else {", start)
    branch = jsx[start:end]
    assert "setSections(p.sections_json)" in branch, "interrupted 分支要从库里恢复章节"
    assert "setReferences(p.references_json)" in branch, "interrupted 分支要一并恢复文末列表"
    assert "res.sections || null" not in branch, "interrupted 分支不得清空章节"


def test_rewrite_sections_does_not_generate_when_reset_fails(jsx):
    """「作废并重写」作废那步失败时，不许继续发起生成。

    D6：run() 曾吞掉异常且不返回成败，于是 rewriteSections 里 api.resetSections 抛错
    （409 忙 / 网络）后，下面的 generate() 照样跑 —— 生成会在一份没作废的旧正文上继续，
    与「作废并重写」的承诺相反。修法：run 返回成败，rewriteSections 据此拦截。
    """
    # run 必须把成败交给调用方，而不是吞掉不吱声
    run = jsx[jsx.index("function run(fn)"): jsx.index("function recommendTopics()")]
    assert "return true" in run, "run 成功时要返回 true"
    assert "return false" in run, "run 失败时要返回 false"

    # rewriteSections 必须接住 run 的成败，且「作废成功」排在「发起生成」之前
    rewrite = jsx[
        jsx.index("function rewriteSections"): jsx.index("function confirmCitations")
    ]
    assert "const resetOk = await run(" in rewrite, "作废结果要被接住"
    assert "if (!resetOk) return" in rewrite, "作废失败要就地返回"
    assert rewrite.index("if (!resetOk) return") < rewrite.index("await generate()"), \
        "生成必须发生在作废成功之后"


def test_delete_document_notices_do_not_overwrite(jsx):
    """删文献时的两条「已作废」提示要累进到同一句，不能各写一句互相覆盖。

    D8：删掉一篇既被引用绑定、又进了某簇的文献时，binding_invalidated 与
    clusters_invalidated 同时为真。旧代码两句 setNotice 顺序执行，第二句把第一句整个
    盖掉 —— 用户只看到「聚类已作废」，漏掉「绑定已作废、请重新调度」，照着一句不完整
    的提示去操作，生成出来的正文缺了重新调度这一环。
    """
    body = _app_method(jsx, "deleteDocument")
    assert body.count("invalidationNotes.push(") == 2, "两条已作废提示都要被收进数组"
    assert "setNotice(invalidationNotes.join(' '))" in body, "最终要合成一句 setNotice"
    assert body.count("setNotice(") == 1, "deleteDocument 只该有一处 setNotice（合成后那句）"


def test_switching_project_clears_topics_and_domain(jsx):
    """换项目时要把上一个项目的「推荐选题」与「研究领域」一并清掉。

    D9：topics 与 domain 都是本地瞬态（不进库、loadProject 从服务端也拿不到），旧的
    loadProject 只在 documents 上算了 projectSwitched 并清空。于是从 A 切到 B 后，A 的
    推荐卡片还留在 B 的选题步，点一张就把 B 的「确定研究核心方向」填成 A 的建议；A 的
    研究领域也照样回填进 B 的输入框。
    """
    start = jsx.index("const projectSwitched = project?.id !== p.id")
    window = jsx[start:start + 400]
    assert "setTopics([])" in window, "换项目要清掉上一个项目的推荐选题"
    assert "setDomain('')" in window, "换项目要清掉上一个项目的研究领域"
    assert "setDocuments([])" in window, "文献列表也要同一条判据、同进同退"


def test_word_budget_input_preserves_empty(jsx):
    """字数预算框删空要显示「空」，不能落成 0（0 本身是合法值）。

    D10：旧值 value={sec.word_budget || 0} 配合 onChange 里的 parseInt(...) || 0，
    用户全选删掉预算时，空串立刻被落成 0、框里回到 "0"，与本步「可手动调整各节字数
    预算」的预期相悖（0 字也是一节，不该和「没分配」混为一谈）。修法用 ?? 与
    === '' 两处一起改：空落成 undefined（经 cloneOutline 的 JSON 往返被丢掉，
    outlineTotal 与后端 _sum_budget 都按 0 计），0 仍显示 0。
    """
    body = _function_body(jsx, "OutlineStep")
    assert "value={sub.word_budget ?? ''}" in body, "子节预算删空要显示空"
    assert "value={sec.word_budget ?? ''}" in body, "节预算删空要显示空"
    assert "e.target.value === '' ? undefined" in body, "删空要落成未分配，不能归零"
    assert "word_budget || 0" not in body, "预算输入不得再用 || 0 把空落成 0"


def test_outline_primary_buttons_are_mutually_exclusive(jsx):
    """大纲卡片三个主按钮必须互斥，且都由 clustersNeedConfirm 这一处判据推出（T6）。

    旧断言只数 `? 'btn btn-primary' : 'btn'` 出现 3 次，不判互斥 —— 把三个按钮的条件
    抄成同一个布尔，计数还是 3，卡片却会并排两个蓝按钮（或一个都不蓝）。这里把三处
    判据的形状钉住：聚类确认由 clustersNeedConfirm 驱动，生成/确认两个在
    !clustersNeedConfirm 下按 outline 有无互补二选一 —— 三者两两互斥、恰好一个为真。
    """
    # ClusterPanel 的确认按钮由父层那一个布尔驱动
    assert "confirmClustersPrimary={clustersNeedConfirm}" in jsx
    # OutlineStep 的两个按钮：!clustersNeedConfirm 下按 outline 有无二选一
    assert "const generatePrimary = !clustersNeedConfirm && !props.outline" in jsx
    assert "const confirmPrimary = !clustersNeedConfirm && !!props.outline" in jsx


def test_generate_step_view_button_takes_over_the_primary_slot(jsx):
    """正文写完之后，蓝按钮位必须是「查看导出结果 →」，不是那个空转的生成按钮。

    三层背景都可查：① `POST /projects/{id}/generate` 是**可续写**的，正文写完时
    `_run_generation` 的循环按标题跳过每一节、然后照旧把 status 写回 COMPLETED ——
    那个按钮在这种状态下是**纯空转**（项目已经导出过时还会把 exported 顶回
    completed），而它是界面上去往那次空转的唯一一条路；② 真正该点的「重写正文」就在
    它旁边，于是同一个状态并排两个按钮、其中一个是空的（用户报的"重复"）；③ 本步的
    蓝按钮唯一性靠 `fix` 的三元互斥**结构**保证（各支硬写一次主按钮样式，与大纲步的
    "条件式取样式"是两种都成立的写法），而此前**没有任何断言盯过本步** —— 改 `fix`
    或往链里加一条出口，卡片上会静默出现两个蓝按钮。这条就是那个钉子。
    """
    body = _function_body(jsx, "GenerateStep")

    # ① 判据各一处。按**形状**数而不是按裸标识符数：_code_only 只剥整行的 `//`，
    #    行尾随手加一句 `// 说明` 就能把裸计数抬高一。
    assert body.count("const complete = ") == 1, "complete 只能定义一次"
    assert body.count("const viewPrimary = ") == 1, "viewPrimary 只能定义一次"
    assert body.count(") : viewPrimary ? (") == 1, "蓝按钮位必须取用判据本身"
    assert body.count("{complete && !viewPrimary && (") == 1, \
        "次级那一支必须取同一个判据的取反，否则同一状态会并排两个「查看导出结果」"
    assert "const viewPrimary = fix === 'none' && complete" in body, \
        "让不让给补救由 fix 这一处判，写成裸 complete 会让两个补救状态里「查看导出结果」整个消失"
    # 「正在写」不能算「已写完」：resumable 里带着 !generating，生成中时它恒为 false，
    # 少了这一条，写到一半蓝按钮就会跳成「查看导出结果」。
    definition = body[body.index("const complete = "): body.index("const viewPrimary = ")]
    assert "!props.generating" in definition, \
        "生成中不许算「已写完」——resumable 在生成时恒为 false，只取它的反会把「正在写」放进去"

    # ② 蓝按钮位四段互斥穷尽（这段最容易被加一条出口而静默出两个蓝按钮）
    for anchor in ("{fix === 'render' ? (", "{complete && !viewPrimary && ("):
        assert anchor in body, f"切片边界不见了：{anchor}"
    slot = body[body.index("{fix === 'render' ? ("): body.index("{complete && !viewPrimary && (")]
    assert slot.count('className="btn btn-primary"') == 4, \
        "四段（render / numbering / 写完 / 兜底）各硬写一次主按钮样式"
    assert slot.count(") : ") == 3, "三元链恰好三个接头（这条依赖单行排版）"
    assert ") : viewPrimary ? (" in slot
    # 写完那一段必须排在兜底之前 —— 否则写完时的蓝按钮还是「开始分段生成」
    assert slot.index(") : viewPrimary ? (") < slot.index("'开始分段生成'")

    # ③ 同一个入口的两处渲染点互相取反，且次级那个不许也是蓝的
    assert body.count("查看导出结果 →") == 2
    tail = body[body.index("{complete && !viewPrimary && ("):]
    secondary = tail[: tail.index(")}")]
    assert 'className="btn"' in secondary and "btn-primary" not in secondary


def test_min_target_words_is_not_hardcoded_outside_the_fallback(jsx):
    """下限数字只能作为 FALLBACK_MIN_TARGET_WORDS 出现，不许再另立一个模块级常量（T10）。

    `1000 not in TopicStep` 只能管住函数体内；把下限提成 `const MIN = 1000` 放到函数外，
    那条断言照旧绿，界面却会说出一个后端并不认的下限。FALLBACK_MIN_TARGET_WORDS 自己
    又被 test_frontend_mirror 锁住 == 后端 MIN_TARGET_WORDS，所以这里补上
    「模块级不得再写死第二份」这一环。
    """
    hardcoded = re.findall(r"const\s+([A-Za-z_]\w*)\s*=\s*1000\b", jsx)
    assert hardcoded == ["FALLBACK_MIN_TARGET_WORDS"], hardcoded


def test_reference_list_preview_turns_off_the_ol_numbering():
    """文末列表预览必须关掉 `<ol>` 的默认序号。

    每一条的文本本身就以 `[1]` 开头（后端 formatted 里带着编号，导出文件里也是它），
    再叠一层 `<ol>` 的 `1.` 就印成「1. [1] 张三. 题名[J]. …」—— 同一条参考文献，
    屏幕上比成品多一层。这不是样式偏好，而是「预览与导出的成品不一致」这个真 bug 的
    修法；它失效起来完全无声（屏幕上多一个序号，没有任何东西会报错，而用户是按
    屏幕上的样子校对他投稿的那份稿子的）。
    """
    css = _code_only(APP_CSS.read_text(encoding="utf-8"))
    rules = re.search(r"\.ref-items\s*\{(.*?)\n\}", css, re.S)
    assert rules, "App.css 里找不到 .ref-items"
    assert "list-style: none" in rules.group(1), "文末列表预览会印出双重编号"


def test_document_edit_type_options_are_derived_not_hand_written(jsx):
    """文献类型下拉框的选项必须由 SOURCE_TYPE_LETTERS 派生，不许手写字母。

    手写一份字母表就是**第二份白名单**：它与后端 citation_format.SOURCE_TYPES 漂移时，
    界面能选到一个后端 400 拒掉的类型（或者选不到某个合法类型），而两边看上去都正常。
    派生出来的选项不可能漂移 —— SOURCE_TYPE_LABELS 只是注解文字，漏一个键只会让那一项
    少一句话，不会多/少一个可选项。所以这里盯的是「没有手写的字母选项」。
    """
    body = _function_body(jsx, "DocumentEditModal")
    assert "SOURCE_TYPE_LETTERS.map(" in body, "选项要按白名单渲染"
    hard = re.findall(r'<option value="([A-Za-z])"', body)
    assert hard == [], f"下拉框里出现了手写的字母选项：{hard}"


def test_citation_format_options_are_derived_not_hand_written(jsx):
    """引用调度步的格式下拉框必须按**该语言的格式清单**派生，不许手写选项。

    与文献类型那个下拉框同一条理由，只是这次的第二份白名单更毒：手写一份三项清单，
    用户在英文写作下会看到一个后端会 400 拒掉的 GB/T 7714 选项 —— 而这正是这轮要消灭
    的那个 bug，且它看起来完全正常（一个多出来的、能选中、点确认时才被后端挡回来的
    选项）。选项文字与提示语同样只有后端一份：手写一句"中文期刊极少使用"，等后端那张
    表改了，界面上这句话就是假的。
    """
    body = _function_body(jsx, "CitationStep")
    # 只切「参考文献列表格式」那一个下拉。同一步里还有一个角标样式的下拉，它**也**是
    # 派生出来的，但判据是另一张表（见下一个用例）—— 两个下拉都靠 `.map()` 渲染，
    # 不切开会互相误伤：拿格式清单去判角标样式，那条断言会在正确的代码上报红。
    select = body[body.index("<label>参考文献列表格式</label>"):]
    select = select[: select.index("</select>")]

    assert "props.formats.map(" in select, "选项要按该语言的格式清单渲染"
    hard = re.findall(r'<option value="([A-Za-z0-9]+)"', select)
    assert hard == [], f"格式下拉框里出现了手写的选项：{hard}"

    # 「（默认）」这个标记也不许写进选项文字：默认格式随语言变（zh 是 GB/T、en 是 APA），
    # 写死一个就会在另一种语言下印着一个错的默认标记。它由 props 现判。
    assert "props.defaultFormat" in select, "默认格式要读 props（它随语言变）"
    assert "props.formatNotes[props.citationFormat]" in body, "提示语要读后端下发的那张表"


def test_cite_style_options_are_derived_not_hand_written(jsx):
    """角标样式下拉框的选项也必须派生，不许手写。

    这道与上面那个格式下拉**不是**同一条理由：格式清单随语言变，所以非派生不可；角标
    样式两种语言都一样，手写一份在功能上永远不会错。它的理由是**报错文案**——
    `/citations/confirm` 的 400 里印的是 `citation_format.CITE_STYLE_LABELS` 里的名字，
    而先前前端那份手写的 `<option>` 恰好也这么写，两边是靠人抄一致的。抄一致的东西迟早
    会不一致，那时用户拿着「可选：[1] 方括号角标、纯数字上标（¹ ² 风格）」找不到自己
    选过的那一项。派生之后，这句话与下拉框里的字是同一个来源。
    """
    body = _function_body(jsx, "CitationStep")
    # 从后往前切：角标样式的下拉在前、格式的在后，所以取到「参考文献列表格式」之前的
    # 那一段才切出它自己。
    select = body[: body.index("<label>参考文献列表格式</label>")]
    select = select[select.rindex("<label>正文引用角标样式</label>"):]
    select = select[: select.index("</select>")]

    assert "props.citeStyles.map(" in select, "选项要按后端下发的白名单渲染"
    hard = re.findall(r'<option value="([A-Za-z0-9]+)"', select)
    assert hard == [], f"角标样式下拉框里出现了手写的选项：{hard}"
    assert "props.citeStyleLabels[" in select, "选项文字要读后端那张表（报错文案用的是同一张）"


def test_export_layout_language_comes_from_the_project(jsx):
    """导出排版的语言只读 `project.writing_lang`，且屏幕预览与导出文件共用同一个标题词。

    这份语言决定的是**产物**：一份英文论文的 Word 该是 Times New Roman、双倍行距、
    四边 1 英寸、文末那一节叫 References。前端若另立一个常量（或干脆写死 'zh'），中文
    用户看不出任何异常，英文用户下载到的是一份中文排版的稿子 —— 而那个文件是**能打开**
    的，没有任何东西会报错，用户要到投稿前才会发现。

    共用同一个标题词的代价是这里多了一个**渲染期依赖**：referencesHeading 少写一行
    import 就是一个渲染期 ReferenceError（后果见下面那段注释），所以这条连 import 行
    一起断言。
    """
    body = _function_body(jsx, "ExportStep")
    assert "const lang = project?.writing_lang || FALLBACK_DEFAULT_WRITING_LANG" in body, \
        "排版语言要读服务端那一列，不推导、不另立常量"

    # 预览与导出必须是**同一个词**：各写一个，就会出现"屏幕上写着参考文献、下载下来是
    # References"这种不一致，而它只在用户打开文件的那一刻才暴露。
    assert "<h3>{referencesHeading(lang)}</h3>" in body, "预览的标题词要跟导出走同一个函数"
    assert "<h3>参考文献</h3>" not in body, "预览的标题词不许手写"

    # 光断言**使用点**是不够的：上面那行断言的是「这里调用了 referencesHeading」，
    # 而少一行 import 照样能让它绿 —— 这正是缺陷溜过去的原因。referencesHeading 由
    # exporters.js 导出，App.jsx 里没有本地声明，所以漏 import 不是退回中文，是渲染期
    # ReferenceError。触发条件是「已生成正文 **且** 文献列表非空」，也就是正常走完
    # 一轮的常态 —— 所以这条必须连 import 行一起钉住。
    #
    # 后果分两个阶段，都如实记在这里：本轮之前 frontend/src 全树没有 error boundary，
    # React 会卸载整棵 root，用户看到一片空白；本轮起正文区与整个 App 各有一层
    # ErrorBoundary（见本文件末尾那三条），同一个缺陷变成「正文区换成一张错误卡片、
    # 顶栏与项目列表还在」。**降级不等于无害**，所以这条断言照旧。
    imported = re.search(r"import\s*\{([^}]*)\}\s*from\s*'\./exporters'", jsx)
    assert imported, "App.jsx 里找不到从 './exporters' 的那行 import"
    assert "referencesHeading" in imported.group(1), \
        "referencesHeading 必须在这行 import 里 —— 少了它不是退回中文，是整页白屏"

    # 语言要一路传到 exportDocument 的最后一个实参。漏传**不会报错**（尾参默认 'zh'），
    # 只会安静地给一份中文排版的英文论文 —— 所以这里盯的是"紧跟着文件名那个实参"。
    call = body[body.index("exportDocument("):]
    assert re.search(r"safeFilename\(title\),\s*\n\s*lang,", call), \
        "exportDocument 的最后一个实参必须是 lang"


def test_document_edit_shows_the_failure_inside_the_modal(jsx):
    """编辑文献保存失败时，原因必须显示在**框里**，不能只丢给顶部横幅。

    顶部那条 error-banner 在模态框遮罩（z-index 1000）**后面** —— 用户点完保存什么也
    看不见，只会以为按钮坏了。本项目已经吃过一次「失败原因被吃掉」（A7），所以这条
    路径刻意不走 run()（它只写横幅），而是把 {ok, message} 退回来由框自己渲染。
    """
    body = _function_body(jsx, "DocumentEditModal")
    assert "props.onSave(" in body, "提交要经 onSave 拿回成败"
    assert "setMessage('❌ ' + res.message)" in body, "失败原因要渲染在框里"

    save = _app_method(jsx, "saveDocument")
    assert "return { ok: false, message: e.message }" in save, "失败要如实退回给调用方"
    assert "run(" not in save, "走 run() 就是把原因丢进遮罩层后面那条横幅"


def test_numbers_are_read_through_wordsNumber_only(jsx):
    """数字输入框一律经 `wordsNumber` 读，不许就地 `parseInt`。

    这不是风格问题。`parseInt("1e3", 10)` 是 **1** —— 它只吃开头的数字串；而
    `Number("1e3")` 是 1000。目标总字数与分节字数都是 number 输入框，浏览器允许敲
    "1e3"，于是同一个字符在界面上显示 1e3、提交上去是 1，用户拿到的是一篇一字的论文，
    全程没有任何报错。`wordsNumber`（见它的定义处那段注释）就是这个唯一入口。

    这条能存在的理由正是它刚刚失效过一次：`wordsNumber` 定义好了、注释也写明了，
    两个预算输入框却各自写了 `parseInt(e.target.value, 10)` —— 有唯一入口但没有
    "只许走这个入口"的钉子，后来的代码就会绕过它。
    """
    assert "const wordsNumber" in jsx or "function wordsNumber" in jsx, \
        "找不到 wordsNumber，这条断言就失去意义了"
    assert "parseInt(" not in jsx, \
        "App.jsx 里出现了 parseInt —— 读数字要走 wordsNumber（它会把 '1e3' 读成 1000）"

    # 两个字数预算输入框（分节 / 子节）是这条缺陷的原发地，逐处钉住：读数字走
    # wordsNumber，且「删空」必须原样落成 undefined —— 落成 0 就是「未分配」被当成
    # 「分配了 0 字」，用户删掉那个数字之后那一节当场变成不分配。
    # 锚在 onChange 上：两个同名函数各自的**定义**也长成 `updateBudget(props, …)`，
    # 不锚就会把定义一并数进来，于是这条断言在正确的代码上报一个数不符。
    budgets = re.findall(r"onChange=\{\(e\) => update(?:Sub)?Budget\(props,[^\n]*", jsx)
    assert len(budgets) == 2, f"预算输入框的 onChange 应为两处，实际 {len(budgets)} 处"
    for line in budgets:
        assert "wordsNumber(e.target.value)" in line, f"这一处没走 wordsNumber：{line}"
        assert "e.target.value === ''" in line, f"这一处把「删空」弄丢了：{line}"


def test_effective_citation_format_has_a_single_derivation_point(jsx):
    """「库里那份格式在当前语言下合不合法」只许判一次，判在 `effectiveFormatFor` 里。

    这个项目为它吃过一次苦头：`loadProject` 播种格式时写的是 `p.citation_format ||
    'gb7714'`（**没归一化**），而 `confirmTopicChange` 又另写了一遍
    `formatsFor(lang).includes(fmt) ? fmt : defaultFormatFor(lang)`。同一件事两把尺子
    的后果很具体：切换到英文项目时界面显示 APA、state 里还是 gb7714（或反过来），
    用户是**按着一个假值**在决定要不要提交选题。

    后端同一个判断只有 `citation_format.effective_format` 一处（它自己的 docstring
    就写着"格式解析的唯一入口"），前端这处与它同名同义，所以也必须是唯一一处。
    """
    body = re.search(
        r"const effectiveFormatFor = \([^)]*\) =>\s*\n?\s*"
        r"formatsFor\(([^)]*)\)\.includes\(([^)]*)\)\s*\?\s*\2\s*:\s*defaultFormatFor\(\1\)",
        jsx,
    )
    assert body, \
        "effectiveFormatFor 的判据要正好是「在本语言的清单里 ? 现值 : 默认格式」"

    hits = re.findall(r"formatsFor\([^)]*\)\.includes\(", jsx)
    assert len(hits) == 1, \
        f"formatsFor(...).includes(...) 出现了 {len(hits)} 次，判据要收敛到 effectiveFormatFor 一处"

    # loadProject 播种时也必须走它 —— 直接读库里的裸值（或配一个写死的默认格式）就是
    # 那个缺陷的原样复现。
    load = _app_method(jsx, "loadProject")
    assert "effectiveFormatFor(" in load, "loadProject 播种格式要走归一化那一处"
    assert "p.citation_format ||" not in load, "别在播种处另配一个默认格式"


def test_topic_step_language_note_comes_from_the_backend_table(jsx):
    """选题步那句"某格式为什么不在这儿"由后端那张表给，不在前端按语言另判一次。

    原先这里是 `props.writingLang === 'en' ? 'GB/T 7714 是中文期刊的著录标准…'`：
    同一个事实判了两遍（可选项由 `props.formats` 派生、说明语按语言手写），而且那句
    文案是**抄过来的**——后端 `format_note` 改一个字，界面上这句话就变成假话，而它
    看起来完全正常。现在改成：本语言的清单（props.formats）决定"能选哪些"，全部格式
    减掉它（props.allFormats）拿到"被排除的"，再逐个取后端下发的 props.formatNotes。
    """
    body = _function_body(jsx, "TopicStep")
    assert "props.allFormats" in body, "被排除的格式要由全部格式减掉本语言的清单现算"
    assert "props.formatNotes[" in body, "说明语要读后端下发的那张表"
    assert "writingLang ===" not in body, \
        "语言说明不许在前端按语言另判一次 —— 那是同一个事实的第二把尺子"
    assert "中文期刊" not in body, "后端那张表里已有的句子不许在前端手写一份"


# ---------------------------------------------------------------
# 渲染期异常的兜底（ErrorBoundary）
# ---------------------------------------------------------------
SRC_DIR = APP_JSX.parent
MAIN_JSX = SRC_DIR / "main.jsx"
BOUNDARY_JSX = SRC_DIR / "ErrorBoundary.jsx"


def _src_files() -> list[Path]:
    """`frontend/src` 下全部 JS/JSX 源码（用来断言「某样东西全树只有一处」）。"""
    return sorted(p for p in SRC_DIR.rglob("*") if p.suffix in (".js", ".jsx"))


def test_app_is_wrapped_in_an_error_boundary_at_the_root():
    """`main.jsx` 里 `<App />` 必须被 `<ErrorBoundary>` 包着。

    这是渲染期异常的最后一道。本项目已经吃过一次：少一行 import → 渲染期
    ReferenceError → React 卸载整棵 root → **整页空白**。开发机上还能看控制台，而
    打包版用户没有控制台可看，他看到的只有一个白窗口，唯一的结论是「这软件坏了」。
    """
    src = _code_only(MAIN_JSX.read_text(encoding="utf-8"))
    assert re.search(r"<ErrorBoundary[^>]*>\s*<App\s*/>\s*</ErrorBoundary>", src, re.S), \
        "main.jsx 里 <App /> 没有被 <ErrorBoundary> 包住"


def test_content_region_has_its_own_error_boundary(jsx):
    """正文区**另有一层**，且复位判据取项目 id。

    为什么外层那一层不够：只有它的话，正文里一个异常会把整页换成错误卡片 ——
    顶栏与左侧项目列表跟着一起没了，用户连「换一个项目试试」这个动作都做不了。
    所以这一层保住的是**出路本身**，不是外层那条的重复。

    复位判据同样只有一处（`key` 取项目 id）：没有它，一次异常之后切到别的项目仍停在
    错误卡片上，看起来像「这软件坏了」，而坏的其实只是上一个项目的那一次渲染。
    """
    m = re.search(
        r'<main className="content">\s*<ErrorBoundary([^>]*)>\s*\{renderStep\(\)\}\s*'
        r"</ErrorBoundary>\s*</main>",
        jsx, re.S,
    )
    assert m, ('App.jsx 的正文区（renderStep 那一块）没有单独的 ErrorBoundary —— '
               '顶栏与左侧项目列表必须在它外面，否则异常时用户连「换个项目」都做不了')
    assert "key={project?.id" in m.group(1), \
        "正文区那层 boundary 的 key 要取项目 id，否则切项目不会复位"


def test_error_boundary_hooks_exist_in_exactly_one_file():
    """全树只有 `ErrorBoundary.jsx` 里有兜底钩子，且两个钩子各恰好一处。

    这条拦的是「顺手在别处再加一层兜底」：两个地方各自决定「出错了怎么办」，用户看到
    哪一种取决于异常恰好落在谁的子树里 —— 而两张卡片的样子与出路一旦不同，同一台
    机器上的两次事故会给出两种说法。数量本身就是断言（与
    `test_clusters_need_confirm_is_the_single_judgement` 同一个道理）。

    判据含 `getDerivedStateFromError`：只钉 `componentDidCatch` 的话，一处只实现了
    「渲染出错后记一笔」却没给出兜底界面的代码能溜过去（那样 React 会继续卸载子树）。
    """
    hits = [
        (path.name, hook)
        for path in _src_files()
        for hook in ("componentDidCatch", "getDerivedStateFromError")
        if hook in _code_only(path.read_text(encoding="utf-8"))
    ]
    files = sorted({name for name, _ in hits})
    assert files == ["ErrorBoundary.jsx"], f"这些文件里出现了兜底钩子：{files}"
    assert len(hits) == 2, f"两个钩子应当各恰好一处，实际：{hits}"


# ---------------------------------------------------------------
# 文末列表两份状态必须成对写（references / referenceRuns）
# ---------------------------------------------------------------


def test_every_references_write_has_a_twin_referenceRuns_write(jsx):
    """`setReferences` 与 `setReferenceRuns` 必须成对，而且是**挨着**写的。

    `references`（纯文本著录串）与 `referenceRuns`（带斜体的片段）是两份并排的状态：
    前者给屏幕上那段预览与 txt / md，后者只给 docx 排版。写前者有四条路径 —— 启动
    恢复、生成完成、换项目播种，外加「作废正文」那一处清空。

    漏配的症状不会报错，只会**印错**：片段那半留在上一版上，于是 docx 在新的正文、
    新的预览旁边，照上一份文末列表的斜体范围排版 —— 正文角标、预览文字、连 txt 导出
    都是对的，只有打开 Word 才看出那几个词斜错了地方。两边都是合法数组，任何行为
    断言都抓不到，所以只能从"写入点必须配对"这一侧看住它。

    只数个数不够：把四处 `setReferenceRuns` 全堆到文件末尾也能过。所以再加一条邻近性
    —— 每个 `setReferences(...)` 后面三行内必须有一个 `setReferenceRuns(...)`。清空
    那处同样要孪生：只清掉字符串那半，片段那半就成了无主的旧数据。

    （`setReferenceRuns] = useState(null)` 那种声明不算写入 —— 判据带左括号，认的是
    调用形状，不是变量名。）
    """
    lines = _code_only(jsx).splitlines()
    writes = [i for i, ln in enumerate(lines) if "setReferences(" in ln]
    twins = [i for i, ln in enumerate(lines) if "setReferenceRuns(" in ln]
    assert writes, "找不到 setReferences，判据本身失效了"
    assert len(writes) == len(twins), (
        f"setReferences 有 {len(writes)} 处、setReferenceRuns 有 {len(twins)} 处 —— "
        "每一处都要配对；漏配的那条路径导出的 docx 会用上一份文末列表的斜体范围"
    )
    for i in writes:
        assert [j for j in twins if 0 <= j - i <= 3], (
            f"第 {i + 1} 行的 setReferences 后面三行内没有 setReferenceRuns："
            f"{lines[i].strip()}"
        )


# ---------------------------------------------------------------
# 引用调度的进度条（v1.29）
# ---------------------------------------------------------------
API_JS = APP_JSX.parent / "api.js"


def test_citations_is_a_polled_task_kind(jsx):
    """`'citations'` 必须在 POLLED_TASK_KINDS 里 —— 缺了它那条进度条永远不结束。

    这条同时管三件事：另一侧（另一个标签页、切走再切回、或状态已是 citation_pending
    时刷新）靠它把槽里的那条进度一路轮询到终态；`serverBusy` 在那 120 秒里为真（此前
    按钮是亮的、点下去才被 409 拒 —— PRD §8.1 记过的那条盲区）；`CitationStep` 里
    `frozen` 那句"调度正在跑时也会被拒"才第一次名副其实。

    反过来也要看住：那张表**没人读**时它就是空的，所以两处判据（serverTaskRunning
    与轮询 effect）都在，也是断言的一部分。
    """
    kinds = jsx[jsx.index("const POLLED_TASK_KINDS = new Set(["):]
    kinds = kinds[: kinds.index("])")]
    assert "'citations'" in kinds, "引用调度不在轮询白名单里，那一侧的进度条永远不会结束"
    assert "'material_analysis'" in kinds, "取值切片跑偏了，这条断言就失去意义"
    assert jsx.count("POLLED_TASK_KINDS.has(") == 2, \
        "这张表的使用点应恰好两处（serverTaskRunning 与轮询 effect）"


def test_citation_step_renders_its_progress_bar(jsx):
    """本步要收 task 与 scheduleInFlight，并据此渲染那条**不确定**进度条。

    两个来源缺一不可，因为这条路由是同步的：发起的那一侧没有可轮询的服务端状态（本地
    判据 scheduleInFlight 在管），另一侧则拿不到本地判据（槽里的 task 在管）。少了本地
    那一半，点下去只有按钮变灰；少了服务端那一半，切走再切回时那条条会一直转。

    `done`/`total` 一个都不许传：整段等待就是一次模型调用，没有任何可数的单位，给百分比
    就是编造 —— 判据与 WorkingBar 自己那段注释同一条（文献解析能确定，是因为它按篇做）。
    """
    body = _function_body(jsx, "CitationStep")
    assert (
        "const serverScheduling = props.task?.status === 'running' "
        "&& props.task.kind === 'citations'" in body
    ), "服务端那份要按 kind 认，不能只看 running（别种任务在跑也会是 running）"
    assert "const scheduling = props.scheduleInFlight || serverScheduling" in body, \
        "本步「我正在等」只能有这一个判据（两个来源，一处合成）"
    assert body.count("{scheduling && (") == 1, "进度条只该有一个渲染点"

    bar = body[body.index("{scheduling && ("):]
    bar = bar[: bar.index(")}")]
    assert "props.task.message" in bar, "服务端那份要说清现在在等什么"
    assert "props.task.started_at" in bar, "服务端那份的「已等待 N 秒」要接着服务端的时刻"
    assert "done=" not in bar and "total=" not in bar, \
        "整段等待没有可数的单位，给百分比就是编造"

    # 本步自己跑起来时，那句"它的进度条在它自己那一步上"就是当面自相矛盾
    assert "{!props.generating && props.busy && !scheduling && (" in body, \
        "busy 提示必须让开本步自己的进度条"

    # 传参那一半在 renderStep 里（CitationStep 拿不到外面这些）
    assert "task={task}" in jsx and \
        "scheduleInFlight={scheduleInFlight === project?.id}" in jsx, \
        "两个判据都要传进来；本地那个按项目 id 比，否则切项目时它会跟着跑过去"


def test_finish_task_reconciles_the_binding_for_citations(jsx):
    """轮询到 citations 终态时，本地那份 binding 必须跟着换成库里的。

    「确认引用绑定」提交的是**本地**这份 binding（confirmCitations 里就是它）。少了
    这一句，另一侧那条恢复路径会停在"库里有新绑定、界面手里还是旧的"，用户下一次盖章
    就把刚调度好的结果顶回去 —— 全程没有任何迹象能让他看出来。这是把 citations 加进
    POLLED_TASK_KINDS 的**配套**，不是可选的收尾。
    """
    body = _app_method(jsx, "finishTask")
    assert "next.kind === 'citations'" in body, "finishTask 没有 citations 分支"
    branch = body[body.index("next.kind === 'citations'"):]
    branch = branch[: branch.index("} catch")]
    assert "setBinding(p.citation_binding_json)" in branch, \
        "要把库里那一份换成手上的这一份，否则下一次确认会盖掉调度结果"


def test_schedule_citations_has_a_client_side_timeout():
    """「执行引用调度」这一个调用必须带客户端上限 —— 否则进度条可能永远转。

    那条路由是同步的，界面在等回执的整段时间里显示进度条；而 `fetch` 自己没有超时，
    一条已经死掉的 TCP 路径不会报错。没有这个上限，用户看到的是一条**永远转下去**的
    进度条，比没有进度条更假（后端那次调用自带 120 秒上限，所以 150 秒不可能误伤）。

    中止时抛的必须是中文：AbortError 的英文原文会原样进 run() 的错误横幅。
    """
    src = _code_only(API_JS.read_text(encoding="utf-8"))
    assert re.findall(r"timeoutMs:\s*(\d+)", src) == ["150000"], \
        "只有引用调度这一个调用传客户端上限，值 150000（= 服务端 120 秒 + 余量）"
    assert "new AbortController()" in src, "上限要靠 AbortController 才落得下来"
    assert "controller.abort()" in src and "signal.aborted" in src, \
        "中止与判定两个动作缺一不可"
    assert "已中止" in src, "中止要抛一句中文，英文原文不能直接进界面"
