"""生成 Agent：渐进式分段生成 + 字数精准校准 + 上下文继承 + 引用转述。

引用机制（学术规范）：
- 正文不直接粘贴文献标题，而是基于文献真实内容进行学术化转述（paraphrase），
  在引用处用角标标注（[1] 或 上标¹）。
- 全文末尾统一生成参考文献列表，正文只保留角标。

**字数有两种口径，别混**：中文按字符（`count_chars`）、英文按词（`count_words`），
由 `count_units` 分发。英文正文按字符计能到按词计的六倍左右，混用会让每一节都判
「字数不足」→ 反复扩写 → 撞满重试上限 → 整节落进占位文本。这个症状长得像模型故障，
实则是口径错配，所以分发点只留 `count_units` 这一个。
"""
from __future__ import annotations

import re
from typing import Any

from app import paper_types
from app.llm import llm
from app.material_parser import excerpt
# 只取函数、不 import 模块本身：本模块有多个形参就叫 `writing_lang`，
# 带着模块进来会被形参遮住（`writing_lang.words_unit` 当场变成 AttributeError）。
from app.writing_lang import DEFAULT_LANG, is_en, output_rule, words_unit

GENERATE_PROMPT = """你是学术论文撰写专家。请撰写论文的以下章节内容。

论文信息：
- 选题：{topic}
- 论文类型：{paper_type}{thread_block}

要求：
1. 严格使用学术化、严谨的语言，避免口语化和模板化表达。
2. 目标字数约 {word_budget} {word_unit}（正文字数）。
{citation_block}4. 结合上文上下文保持逻辑连贯。
{materials}{type_note}
上文摘要（上一章节结尾，如为空说明是第一章）：
{context}

本章节标题：{section_title}

请直接输出正文内容（不要输出章节标题、不要输出参考文献列表、不要任何解释）。
{output_rule}"""


async def generate_section(
    section_title: str,
    word_budget: int,
    refs: list[dict],
    context: str,
    topic: str,
    cite_style: str = "bracket",
    max_retries: int = 3,
    materials: list[dict] | None = None,
    design_context: str = "",
    design_label: str = "",
    paper_type: str = "",
    writing_note: str = "",
    writing_ideas: str = "",
    core_question: str = "",
    writing_lang: str = DEFAULT_LANG,
) -> dict:
    """生成单个章节，并进行字数校准（重试/扩充/精简）。

    refs 中每项需含：num（全局编号）、doc_title、page、snippet（原文片段）。

    materials 是作者自有的研究材料（每项含 label / text / usage / key_points），
    来自 material_agent 的分析计划。design_context 是作者已给出的研究设计 /
    技术方案字段（见 design_agent）。

    paper_type 与 writing_note（该类型的写法要求，见 paper_types.TYPE_CONFIG）
    决定**这一段该用什么腔调写**：七类论文共用一套提示词，只给节标题与字数的话，
    质性访谈、数理推导、工程实现写出来是同一个调子。topic 同理——此前它是形参却
    从未进过提示词，模型只能从节标题反推这篇论文在写什么。

    writing_ideas 是作者在选题那一步写下的写作思路 / 论证思路；core_question 是
    大纲第一遍定下的核心研究问题。**这两条此前都到不了正文**：写每一节的模型看不到
    全篇主线，只能从节标题反推——而节标题本身是被模型改写过的一句话，反推出的「主线」
    与大纲页上显示的那一句未必是同一件事。两个都带上，并在提示词里写明以作者原文为准。

    **加参数必须同步给 tests/test_generation.py 的假生成器加 `**kwargs`**：
    那份替身此前签名是锁死的，上一轮就因为漏了而挂掉 7 个用例。它现在有 `**_ignored`
    兜着（加参数不会再挂），但代价是**不显式扩签名就断不出「参数有没有真的传下来」**，
    所以本轮的串链路用例走 seen_ideas 那条路。

    writing_lang 决定**字数口径**（中文按字、英文按词）与**输出语言宣告**，追加在末尾
    且默认中文 —— 既有调用与断言因此一个字都不用改。
    """
    prompt = GENERATE_PROMPT.format(
        topic=topic or "（未指定）",
        paper_type=paper_types.label_for(paper_type) if paper_type else "（未指定）",
        word_budget=word_budget,
        word_unit=words_unit(writing_lang),
        citation_block=_citation_block(refs, cite_style),
        materials=_format_materials(materials, design_context, design_label),
        type_note=_writing_note_block(writing_note),
        thread_block=_core_question_line(core_question) + _ideas_part(writing_ideas),
        context=context or "（无）",
        section_title=section_title,
        # 正文是产出物的主体：英文项目下必须显式宣告输出语言，否则整段提示词都是
        # 中文指令，模型很可能顺着指令语言用中文交稿（zh 下这是空串，逐字节不变）。
        output_rule=output_rule(writing_lang),
    )

    text = ""
    fallback = False
    if llm.is_configured:
        budget = _token_budget(word_budget)
        for attempt in range(max_retries):
            chunk, finish = await llm.chat_with_finish(
                [{"role": "user", "content": prompt}],
                max_tokens=budget,
            )
            chunk = chunk.strip()
            if finish == "length":
                # 被 max_tokens 截断，加大预算重试（推理模型的思考 token 同样占额度）。
                # 实测推理模型可能把整个额度耗在思考上、正文一字未吐（finish=length
                # 且 chunk 为空）：空返回绝不能覆盖上一轮的可用稿，否则一次抖动就白扔
                # 一整稿，落进占位文本的几率也高得多。
                if chunk:
                    text = chunk
                budget = int(budget * 1.8)
                continue
            if chunk:
                text = chunk
            actual = count_units(text, writing_lang)
            if _within_tolerance(actual, word_budget):
                break
            prompt = _adjust_prompt(
                prompt, text, actual, word_budget, attempt, writing_lang
            )

    if not text.strip():
        # 未配置 LLM，或全部重试都没吐出正文：退回占位文本，避免空内容流入润色环节
        text = _placeholder(
            section_title, word_budget, refs, cite_style,
            configured=llm.is_configured, attempts=max_retries,
            lang=writing_lang,
        )
        fallback = True

    return {
        "section_title": section_title,
        "word_budget": word_budget,
        "actual_words": count_units(text, writing_lang),
        "content": text,
        "references": refs,
        # 记下本节用了哪些自有材料，便于用户回查「我的数据到底进了哪一节」
        "materials_used": [m.get("label", "") for m in (materials or [])],
        # 本节没拿到真实正文。占位文本是失败说明而非正文，下游据此跳过润色
        "fallback": fallback,
    }


def _citation_block(refs: list[dict], cite_style: str) -> str:
    """第 3 条要求 + 文献清单。

    **本节没有绑定文献时，整段引用要求必须消失** —— 不能只把文献清单换成
    「（本章节无指定文献引用）」，因为上面「必须引用下方列出的文献」「每篇文献至少
    被引用一次」还在，模型就会照做，凭空吐出 [1][2]，而文末参考文献列表是空的。
    结果是正文挂着无对应条目的角标，直接击穿「0 幻觉引用」这条立身之本。
    **七类论文都允许不做引用调度**（见 paper_types.TYPE_CONFIG 顶部说明），
    所以这条路径对每种类型都是可达的，不只是技术/工程报告。
    """
    if not refs:
        return (
            "3. 本节**不引用任何文献**：正文中不得出现角标（如 [1]、¹），不得提及"
            "参考文献，也不得编造文献观点；需要佐证时用一般性的学术论述表达。\n"
        )
    cite_format = "方括号角标" if cite_style == "bracket" else "纯数字上标"
    cite_mark = "[1][2]" if cite_style == "bracket" else "¹²"
    return (
        "3. 引用规范：\n"
        "   - 必须引用下方列出的文献，但**不要直接粘贴文献标题或原文**。\n"
        "   - 请先理解每篇文献的核心观点，再用你自己的学术语言**转述（paraphrase）**"
        "其观点并融入正文论证。\n"
        f"   - 每处引用观点后，紧跟角标标注：{cite_mark}（{cite_format}）。\n"
        "   - 每篇文献至少被引用一次。\n"
        "供转述的文献材料（编号 + 标题 + 页码 + 原文片段）：\n"
        f"{_format_refs(refs)}\n\n"
    )


def _format_materials(
    materials: list[dict] | None,
    design: str = "",
    design_label: str = "",
) -> str:
    """把作者自有材料与研究设计渲染成提示词片段。

    两者都为空时返回空串 —— 拼出来的提示词与没有这些功能时逐字节一致，避免为文献综述
    那条已经实测过的路径引入任何行为漂移。
    """
    return _material_blocks(materials) + _design_part(design, design_label)


def _material_blocks(materials: list[dict] | None) -> str:
    """per-节材料片段（按节取用，来自材料融入计划）。无材料时返回空串。"""
    if not materials:
        return ""
    blocks = []
    for m in materials:
        lines = [f"【作者自有材料】{m.get('label') or '未命名材料'}"]
        if m.get("usage"):
            lines.append(f"融入要求：{m['usage']}")
        points = m.get("key_points") or []
        if points:
            lines.append("必须写入正文的事实与数字：" + "；".join(points))
        lines.append("材料内容：")
        lines.append(excerpt(m.get("text") or ""))
        blocks.append("\n".join(lines))
    return (
        "\n作者自有研究材料（作者本人提供的一手数据与成果，**必须融入正文**：其中的"
        "具体数据、结论与案例优于泛泛而谈的一般性论述。这些材料是作者自己的，"
        "**不加角标、不进入文末参考文献列表**）：\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def _design_part(design: str, design_label: str = "") -> str:
    """作者已给出的研究设计 / 技术方案片段。未填写时返回空串。

    内容是**全节通用**的：设计字段是全文共享的已知条件，而按章节标题挑选用不用
    并不可靠 —— 章节标题允许被 LLM 改写，靠关键词匹配随时会失配，失配时正文里
    作者的数据会静默消失。所以改为每节都带上，用使用要求说清「只在相关的地方用、
    不要硬塞」。缺数据的地方要求如实留白，而不是补一个看起来合理的假结果。
    """
    if not design.strip():
        return ""
    label = design_label or "研究设计"
    return (
        f"\n作者已给出的{label}（作者本人提供的一手内容：方法、资料与结论，"
        f"**优于任何文献结论**）：\n"
        f"{design}\n\n"
        f"使用要求：本节凡涉及作者自己的方法、资料或结论，一律以上述内容为准 —— "
        f"不得改动其中的数字、口径与结论，也不得补充这里没有的内容；"
        f"本节不涉及这些内容时不要生硬引入。上面未覆盖、而本节确实需要的部分，"
        f"请如实写成「此处需作者补充」，不要编造。\n"
    )


def _core_question_line(core_question: str) -> str:
    """全篇的核心研究问题（大纲第一遍定下的那一句）；没有值时返回空串。

    边界：它只在生过大纲之后才有值，且改选题会随 outline_json 一起作废 —— 所以
    正文侧必须容忍空，空时这一行整行消失（老项目与新项目的提示词都不受影响）。
    """
    if not core_question or not core_question.strip():
        return ""
    return f"\n- 核心研究问题：{core_question.strip()}"


def _ideas_part(writing_ideas: str) -> str:
    """作者写下的写作思路 / 论证思路片段；没填时返回空串。

    与 outline_agent._ideas_block 是**同一份数据的两份实现**，话不同是刻意的：
    大纲那一步要它决定 core_question 怎么收敛、节标题怎么贴题；正文这一步要的是
    「这一段怎么落笔」。同 _design_block / _design_part 的分法——**阶段不同，指令就不同**。

    优先级必须写明：core_question 是模型把这段思路收敛成的一句话，两者同处一段提示词
    时，模型的摘要不该与作者原文平权；不写这句就是「两句互相矛盾的指令」（这条教训见
    outline_agent._literature_block 的注释：两句同时在场时，模型会挑对自己省事的那个）。
    """
    if not writing_ideas or not writing_ideas.strip():
        return ""
    return (
        f"\n作者本人写下的写作思路与论证思路（**作者本人的意图，优先于你自己的归纳，"
        f"也优先于上面的核心研究问题**）：\n"
        f"{writing_ideas.strip()}\n"
        f"使用要求：本节凡涉及作者点到的论证角度与主题偏向，一律以它为准；"
        f"本节与它无关时不要生硬引入，也不要为了呼应它而添改作者没写过的内容。\n"
    )


def _writing_note_block(note: str) -> str:
    """该论文类型的写法要求片段；未配置该类型时返回空串。

    与 _design_part 分开放，是为了让「没给类型写法」时这一段彻底消失（空串 ⇒
    提示词里不留痕），而不是留下一句没有内容的标题。
    """
    if not note or not note.strip():
        return ""
    return f"\n本类型论文的写法要求：{note.strip()}\n"


# 推理模型（如 deepseek-flash）的思考 token 同样计入 max_tokens，
# 需在正文预算之外预留推理开销，否则正文会被截断甚至返回空。
_REASONING_ALLOWANCE = 3072


def _token_budget(word_budget: int) -> int:
    """按目标字数估算单次生成的 max_tokens（含推理开销预留）。"""
    return max(2048, word_budget * 3) + _REASONING_ALLOWANCE


def _format_refs(refs: list[dict]) -> str:
    if not refs:
        return "（本章节无指定文献引用）"
    lines = []
    for r in refs:
        snippet = (r.get("snippet") or "").strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + "..."
        lines.append(
            f"[{r['num']}] {r['doc_title']}（第 {r['page']} 页）\n"
            f"    原文片段：{snippet or '（无片段，请基于标题合理转述）'}"
        )
    return "\n".join(lines)


def count_chars(text: str) -> int:
    """统计正文字数（含引用的正文字数）。

    口径：正文中所有可见字符（含角标 [1] / ¹）均计入；
    排除空白（空格、换行、制表符）。
    文末参考文献列表与章节标题由调用方保证不纳入统计。

    曾经有一个 cite_style 形参，但两种角标形式在这里都被同等计入（[1] 与 ¹ 都是
    可见字符），它与本函数的结果无关 —— 留着一个用不上的形参，只会让读者以为
    字数口径随角标样式变化，进而在别处照着这个假设写代码。
    """
    return len(re.sub(r"\s+", "", text))


def count_words(text: str) -> int:
    """统计词数（英文正文的字数口径）。

    口径：按空白分词。角标 `[1]` / `¹` 各算一个词 —— 与 Word 的「字数（words）」
    一致（Word 也把角标当一个词）。

    **不做 CJK 特殊处理**：这个函数只服务英文正文。真拿它数中文，一整段中文没有
    空白、会被算成 1 个词。那不是这里的缺陷，是「调用错了函数」——由 `count_units`
    这一层挡住。
    """
    return len(text.split())


def count_units(text: str, lang: Any = DEFAULT_LANG) -> int:
    """字数口径的**唯一分发点**：中文按字（`count_chars`）、英文按词（`count_words`）。

    分两处写会让「这里按字、那里按词」这种错配悄悄存在 —— 症状是英文每节都被判
    「字数不足」而落进占位文本，看着像模型故障。所以除本函数外，不该有第二处
    按语言选口径的代码。
    """
    return count_words(text) if is_en(lang) else count_chars(text)


# 兼容内部原有调用
_count_chars = count_chars


def _within_tolerance(actual: int, target: int, tol: float = 0.05) -> bool:
    if target <= 0:
        return True
    return abs(actual - target) / target <= tol


def _adjust_prompt(
    orig: str,
    text: str,
    actual: int,
    target: int,
    attempt: int,
    lang: Any = DEFAULT_LANG,
) -> str:
    """把「上次差了多少」追加进提示词。单位跟着语言（字 / words）。

    提示词正文仍是中文 —— 给模型看的指令不随产出语言变（见 app.writing_lang 的
    模块说明），变的只是这个数字后面跟的单位。
    """
    unit = words_unit(lang)
    if actual < target:
        return (
            f"{orig}\n\n注意：上次生成字数不足（{actual} {unit}，目标 {target} {unit}）。"
            f"请扩充内容至目标字数，补充论证细节、案例或分析。"
        )
    else:
        return (
            f"{orig}\n\n注意：上次生成字数超出（{actual} {unit}，目标 {target} {unit}）。"
            f"请精简内容至目标字数，删除冗余表述。"
        )


# 占位正文的填充句。**必须两种语言各一条**：这是唯一一段由程序自己写出来的「正文」，
# 它同样会落进论文里。而更要紧的是——英文模式下按空白分词，一整段中文只值 1 个词，
# 下面的 while 会一直追加到撞上预算，产出一坨重复几百遍的中文。
_ZH_FILLER = " 本节围绕研究主题展开系统论述，涵盖相关理论背景、研究方法与核心结论。"
_EN_FILLER = (
    " This section develops a systematic discussion of the research topic, covering the"
    " relevant theoretical background, the research method, and the core conclusions."
)


def _placeholder(
    title: str,
    word_budget: int,
    refs: list[dict],
    cite_style: str,
    configured: bool = False,
    attempts: int = 0,
    lang: Any = DEFAULT_LANG,
) -> str:
    """占位文本。

    措辞必须区分「根本没配 LLM」与「配了但这次没吐出正文」——按前者写会在后者
    情况下骗人：用户明明配好了模型，界面上却说环境未配置大模型，等于把一次生成
    失败伪装成配置问题。

    **措辞与填充句都按语言选**：这段文字也是落在论文正文位置上的，英文论文里夹一段
    中文说明与夹一段中文正文同样破坏观感。字数判定走 `count_units`（英文按词）。
    """
    en = is_en(lang)
    unit = words_unit(lang)
    cite_open, cite_close = ("[", "]") if cite_style == "bracket" else ("<sup>", "</sup>")
    ref_text = ""
    if refs:
        cites = "".join(f"{cite_open}{r['num']}{cite_close}" for r in refs)
        ref_text = (
            f"This section paraphrases the key points of {len(refs)} reference(s){cites}."
            if en
            else f"本节转述了 {len(refs)} 篇文献的核心观点{cites}。"
        )
    if configured:
        body = (
            f"(This section is the body text of \"{title}\". The model returned no body "
            f"text in {attempts} attempts; this is placeholder text. Please check the "
            f"model service and generate this section again.)"
            if en
            else (
                f"（本节为「{title}」的正文内容。模型在 {attempts} 次尝试中均未返回正文，"
                f"此处为占位文本。请检查模型服务后重新生成本节。）"
            )
        )
    elif en:
        body = (
            f"(This section is the body text of \"{title}\". No LLM is configured in the "
            f"current environment, so this is placeholder text. Target length "
            f"{word_budget} {unit}. Set LLM_API_KEY to generate a real academic paraphrase "
            f"based on the reference excerpts.)"
        )
    else:
        body = f"（本节为「{title}」的正文内容。当前环境未配置大模型，此处为占位文本。"
        body += f"目标字数 {word_budget} {unit}。配置 LLM_API_KEY 后将基于文献片段生成真实学术转述。）"
    filler = _EN_FILLER if en else _ZH_FILLER
    while count_units(body, lang) < word_budget:
        body += filler
    return body + ref_text
