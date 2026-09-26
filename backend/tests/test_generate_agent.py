"""测试：生成 Agent 字数统计与校准（generate_agent）。"""
import asyncio

import pytest

from app.agents import generate_agent
from app.agents.generate_agent import count_chars


def test_count_chars_counts_bracket_citations():
    """字数口径：含引用的正文字数（角标计入）。"""
    text = "这是正文内容[1]，继续论述[2]。"
    # 去掉空白后的全部字符（含角标 [1]、[2]）
    expected = len("这是正文内容[1]，继续论述[2]。".replace(" ", ""))
    assert count_chars(text) == expected


def test_count_chars_counts_superscript():
    """上标角标同样计入字数 —— 与方括号完全一致，所以本函数不需要角标样式参数。"""
    text = "这是正文内容¹²。"
    assert count_chars(text) == len("这是正文内容¹²。")


def test_count_chars_ignores_whitespace():
    """空白与换行不计入字数。"""
    text = "正文 内容\n换行"
    assert count_chars(text) == len("正文内容换行")


# ---------------------------------------------------------------
# 两种字数口径（本轮新增：英文论文按词计）
#
# 混用这两种口径的症状是「英文每一节都被判字数不足 → 扩写 → 撞满重试上限 →
# 整节落进占位文本」，看着像模型故障。所以分发点只有 count_units 一处。
# ---------------------------------------------------------------
def test_count_words_counts_citations_too():
    """英文口径按空白分词；角标各算一个词 —— 与 Word 的「字数（words）」一致。"""
    plain = "The study finds a positive effect, while others disagree."
    with_cites = "The study finds a positive effect [1], while others disagree [2]."

    assert generate_agent.count_words(plain) == 9
    # 两个角标各算一个词（Word 也这么数）
    assert generate_agent.count_words(with_cites) == 11


def test_count_words_is_several_times_smaller_than_chars_in_english():
    """同一段英文，按字符计与按词计差着好几倍 —— 这就是两种口径不能混的理由。

    精确倍数不重要，重要的是它**远大于容差**（±5%）：混用一次就必然判错，
    不会偶尔蒙对。
    """
    text = "This section develops a systematic discussion of the research topic. " * 3
    assert count_chars(text) > 5 * generate_agent.count_words(text)


def test_count_units_dispatches_by_language():
    """分发点按语言选口径；未知语言收敛到中文（与 writing_lang.normalize 同源）。"""
    text = "This is a short sentence with eight words."
    assert generate_agent.count_units(text, "en") == len(text.split())
    assert generate_agent.count_units(text, "zh") == count_chars(text)
    assert generate_agent.count_units(text) == count_chars(text)
    assert generate_agent.count_units(text, "fr") == count_chars(text)
    assert generate_agent.count_units(text, None) == count_chars(text)


def test_english_placeholder_word_count_lands_within_tolerance():
    """英文占位正文的词数必须落在容差内 —— 这一条挡的是「填充串没跟着换语言」。

    英文模式下按空白分词，一整段中文只值 1 个词：若填充串仍是中文那一条，
    `while` 循环会一直追加到撞上预算（次数上不封顶地重复同一句中文），
    产出的东西既不是英文、也不是正常长度。
    """
    refs = [{"num": 1, "doc_title": "文献A", "page": 1}]
    body = generate_agent._placeholder("1.1 Background", 300, refs, "bracket", lang="en")

    words = generate_agent.count_words(body)
    assert generate_agent._within_tolerance(words, 300), words
    assert "本节围绕研究主题" not in body, "英文占位正文里不该有中文填充句"
    # 参考文献那句交代也要是英文的：它同样印在正文位置上
    assert "paraphrases the key points" in body
    assert "[1]" in body, "占位文本仍要交代引用了哪些文献"


def test_english_placeholder_blames_config_only_when_unconfigured():
    """英文占位文本同样要区分「没配 LLM」与「配了但没返回」—— 措辞两套都得有。"""
    refs = [{"num": 1, "doc_title": "Ref A", "page": 1}]
    unconfigured = generate_agent._placeholder(
        "1.1", 50, refs, "bracket", lang="en"
    )
    failed = generate_agent._placeholder(
        "1.1", 50, refs, "bracket", configured=True, attempts=3, lang="en"
    )

    assert "No LLM is configured" in unconfigured
    assert "No LLM is configured" not in failed
    assert "3 attempts" in failed


def test_language_does_not_change_the_zh_placeholder_bytes():
    """不传 lang 与显式传 "zh" 必须逐字节相同 —— 尾参默认值不改既有输出。"""
    refs = [{"num": 1, "doc_title": "文献A", "page": 1}]
    assert generate_agent._placeholder("1.1", 300, refs, "bracket") == (
        generate_agent._placeholder("1.1", 300, refs, "bracket", lang="zh")
    )
    assert generate_agent._adjust_prompt("P", "T", 100, 300, 0) == (
        generate_agent._adjust_prompt("P", "T", 100, 300, 0, lang="zh")
    )


def test_adjust_prompt_uses_the_language_unit():
    """扩写/精简提示里的单位跟着语言走（字 / words）。"""
    zh = generate_agent._adjust_prompt("P", "T", 100, 300, 0)
    en = generate_agent._adjust_prompt("P", "T", 100, 300, 0, lang="en")

    assert "（100 字，目标 300 字）" in zh
    assert "（100 words，目标 300 words）" in en
    assert "请扩充内容至目标字数" in en, "指令语言仍是中文（见 writing_lang 模块说明）"


def test_within_tolerance_5percent():
    """字数容差判断（±5%）。"""
    assert generate_agent._within_tolerance(95, 100)   # -5% 边界内
    assert generate_agent._within_tolerance(105, 100)  # +5% 边界内
    assert not generate_agent._within_tolerance(94, 100)   # -6% 超阈值
    assert not generate_agent._within_tolerance(106, 100)  # +6% 超阈值
    assert generate_agent._within_tolerance(0, 0)       # 目标为0时恒为True


def test_generate_section_placeholder_no_llm():
    """未配置 LLM 时，生成占位文本且字数接近预算。"""
    # 确保 llm 未配置（测试环境无 .env）
    refs = [{"num": 1, "doc_title": "文献A", "page": 1, "snippet": "片段"}]
    result = asyncio.run(generate_agent.generate_section(
        section_title="1.1 测试",
        word_budget=300,
        refs=refs,
        context="",
        topic="测试",
        cite_style="bracket",
    ))
    assert result["section_title"] == "1.1 测试"
    assert result["actual_words"] >= 250  # 占位文本应接近目标
    assert "[1]" in result["content"] or result["references"]


def test_format_refs_contains_num_and_title():
    """引用格式化包含编号与标题。"""
    refs = [{"num": 3, "doc_title": "文献X", "page": 5, "snippet": "内容"}]
    text = generate_agent._format_refs(refs)
    assert "[3]" in text
    assert "文献X" in text
    assert "5" in text


# ---------------------------------------------------------------
# 推理模型空返回的防护
# ---------------------------------------------------------------
def _configure_llm(monkeypatch, responses: list[tuple[str, str]]):
    """把 llm 配成「已配置」，并按顺序吐出 responses 里的 (正文, finish)。"""
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()

    seen: list[int] = []

    async def fake(messages, **kw):
        seen.append(kw.get("max_tokens"))
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(llm, "chat_with_finish", fake)
    return seen


def _section(**kw):
    kw.setdefault("section_title", "5.2 研究局限与展望")
    kw.setdefault("word_budget", 71)
    kw.setdefault("refs", [{"num": 1, "doc_title": "文献A", "page": 1, "snippet": "片段"}])
    kw.setdefault("context", "")
    kw.setdefault("topic", "测试选题")
    return asyncio.run(generate_agent.generate_section(**kw))


def test_empty_truncated_retry_does_not_discard_usable_draft(monkeypatch):
    """think token 吃满额度导致空返回时，不能覆盖上一轮的可用稿。

    这是实测缺陷的回归测试：推理模型第 2 次尝试 finish=length 且正文 0 字，
    旧代码用这次空返回覆盖了第 1 轮的正文，最终掉进占位文本 —— 一次抖动白扔一整稿。
    """
    draft = "本研究受横截面设计所限，难以确证因果" * 5  # 明显超预算，触发重试
    seen = _configure_llm(monkeypatch, [
        (draft, "stop"),
        ("", "length"),   # 思考吃满额度，正文为空
        ("", "length"),
    ])

    result = _section()

    assert result["content"] == draft, "应保留上一轮的可用稿，而不是退回占位文本"
    assert result["fallback"] is False
    assert len(seen) == 3
    # 第 2 次是 finish=length，额度要涨了再试第 3 次
    assert seen[2] > seen[1], "截断后应加大 max_tokens 再试"


def test_all_attempts_empty_falls_back_to_placeholder(monkeypatch):
    """三次都没吐出正文才退占位文本，并打上 fallback 标记。"""
    _configure_llm(monkeypatch, [("", "stop")])

    result = _section()

    assert result["fallback"] is True
    assert "未返回正文" in result["content"]
    # LLM 是配好的，措辞就不该说「未配置」——那是把生成失败伪装成配置问题
    assert "未配置大模型" not in result["content"]


def test_placeholder_blames_config_only_when_unconfigured():
    """占位文本的措辞必须区分「没配 LLM」与「配了但没返回」。"""
    refs = [{"num": 1, "doc_title": "文献A", "page": 1}]
    unconfigured = generate_agent._placeholder("1.1", 300, refs, "bracket")
    failed = generate_agent._placeholder(
        "1.1", 300, refs, "bracket", configured=True, attempts=3
    )

    assert "未配置大模型" in unconfigured
    assert "未配置大模型" not in failed
    assert "3 次" in failed
    assert "[1]" in failed, "占位文本仍要交代引用了哪些文献"


# ---------------------------------------------------------------
# 无引用章节：引用要求必须整段消失
# ---------------------------------------------------------------
def test_citation_block_absent_when_section_has_no_refs():
    """本节没有绑定文献时，不得留任何「必须引用」的指令。

    这是实测缺陷的回归测试：旧提示词里「必须引用下方列出的文献」「每篇文献至少被
    引用一次」是无条件的，而文献清单只是换成了一句「（本章节无指定文献引用）」。
    模型照着指令走，凭空吐出 [1][2]，文末参考文献列表却是空的 —— 正文挂着无对应
    条目的角标，直接击穿「0 幻觉引用」。技术报告这类允许跳过引用调度的类型正是
    走这条路。
    """
    block = generate_agent._citation_block([], "bracket")

    assert "必须引用" not in block
    assert "至少被引用一次" not in block
    assert "不得出现角标" in block


def test_citation_block_carries_refs_and_marks():
    """有文献时照旧：列出编号与标题，并给出角标样式。"""
    block = generate_agent._citation_block(
        [{"num": 3, "doc_title": "文献X", "page": 5, "snippet": "片段"}], "superscript"
    )

    assert "[3]" in block and "文献X" in block
    assert "至少被引用一次" in block
    assert "纯数字上标" in block


# ---------------------------------------------------------------
# 研究设计进正文（不只是进大纲）
# ---------------------------------------------------------------
def _design_section(**kw):
    kw.setdefault("section_title", "4.2 假设检验")
    kw.setdefault("word_budget", 300)
    kw.setdefault("refs", [{"num": 1, "doc_title": "文献A", "page": 1, "snippet": "片段"}])
    kw.setdefault("context", "")
    kw.setdefault("topic", "测试选题")
    return asyncio.run(generate_agent.generate_section(**kw))


def _capture_prompts(monkeypatch) -> list[str]:
    """打开 LLM 并打桩正文生成，返回收到的提示词列表。"""
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()
    prompts: list[str] = []

    async def fake(messages, **kw):
        prompts.append(messages[0]["content"])
        return ("正文" * 200, "stop")

    monkeypatch.setattr(llm, "chat_with_finish", fake)
    return prompts


def test_design_reaches_body_prompt(monkeypatch):
    """设计字段必须进正文提示词，并要求以作者的结果为准、缺的留白。

    只把设计喂给大纲是不够的：大纲只定骨架，第四章的每一句话是在生成这里写的。
    """
    prompts = _capture_prompts(monkeypatch)
    _design_section(design_context="主要结果：β=0.42，p<0.01", design_label="实证设计")

    assert prompts, "应当发起了一次生成调用"
    assert "作者已给出的实证设计" in prompts[0]
    assert "β=0.42" in prompts[0]
    assert "此处需作者补充" in prompts[0]


def test_body_prompt_carries_topic_type_and_writing_note(monkeypatch):
    """正文提示词必须带上选题、类型名与该类型的写法要求。

    这三样此前一个都没有：topic 是死形参（routers 传了却从未用上）、类型从未传入、
    写法要求不存在 —— 七类论文拿到的都是同一句「学术化、严谨」，于是同一份骨架下
    写出来的质性访谈、数理推导、工程实现是一个腔调。
    """
    from app import paper_types

    note = paper_types.writing_note_for("质性研究/案例分析")
    prompts = _capture_prompts(monkeypatch)
    _design_section(
        topic="城市社区养老服务的供需错配",
        paper_type="质性研究/案例分析",
        writing_note=note,
    )

    prompt = prompts[0]
    assert "城市社区养老服务的供需错配" in prompt
    assert "质性研究/案例分析" in prompt, "类型名要发出去，不是只在库里躺着"
    assert note.strip() in prompt


def test_body_prompt_falls_back_when_topic_and_type_are_missing(monkeypatch):
    """没给选题/类型时不留空槽：留空比写「（未指定）」更容易让模型自己编一个。"""
    prompts = _capture_prompts(monkeypatch)
    _design_section(topic="", paper_type="")

    assert "- 选题：（未指定）" in prompts[0]
    assert "- 论文类型：（未指定）" in prompts[0]


def test_writing_note_block_is_empty_without_a_note():
    """没给类型写法时这一段整块消失（与设计、材料同一条纪律）。"""
    assert generate_agent._writing_note_block("") == ""
    assert generate_agent._writing_note_block("   \n ") == ""
    assert "写法要求" in generate_agent._writing_note_block("用研究者口吻")
    assert "用研究者口吻" in generate_agent._writing_note_block("  用研究者口吻  ")


def test_prompt_unchanged_without_materials_and_design():
    """材料与设计都为空时，提示词与没有这两个功能时逐字节一致。

    文献综述那条已经实测过的路径不能被这次改动搅动。
    """
    nothing = generate_agent._format_materials(None)
    empty = generate_agent._format_materials([], "", "")

    assert nothing == ""
    assert empty == ""
    assert generate_agent._format_materials(
        None, "主要结果：β=0.42", "实证设计"
    ).startswith("\n作者已给出的实证设计")


def test_core_question_and_ideas_reach_body_prompt(monkeypatch):
    """核心研究问题与作者写的思路都要进正文提示词。

    这两条此前一条都到不了正文：写每一节的模型看不到全篇主线，只能从节标题反推 ——
    而节标题本身是被模型改写过的一句话，反推出的「主线」与大纲页上显示的那一句
    未必是同一件事。
    """
    prompts = _capture_prompts(monkeypatch)
    _design_section(
        core_question="企业社交媒体的使用如何影响隐性知识共享？",
        writing_ideas="先辨析 A 与 B 两个概念，再论证二者互补而非替代",
    )

    prompt = prompts[0]
    assert "- 核心研究问题：企业社交媒体的使用如何影响隐性知识共享？" in prompt, \
        "整体一行进去，不留一个空标题"
    assert "先辨析 A 与 B 两个概念" in prompt
    # 优先级必须写明：core_question 是模型把这段思路收敛成的摘要，不该与作者原文平权
    assert "也优先于上面的核心研究问题" in prompt


def test_thread_lines_are_empty_without_ideas_and_core_question():
    """两者都没有时这两段彻底消失（与设计、材料同一条纪律）。"""
    assert generate_agent._core_question_line("") == ""
    assert generate_agent._core_question_line("   \n ") == ""
    assert generate_agent._ideas_part("") == ""
    assert generate_agent._ideas_part("   \n ") == ""
    assert "核心问题" in generate_agent._core_question_line("  核心问题  ")
    assert "先辨析" in generate_agent._ideas_part("  先辨析  ")


def test_body_prompt_is_byte_identical_without_thread_block(monkeypatch):
    """没给核心研究问题与思路时，正文提示词逐字节不变。

    存量项目全部走这条路（core_question 要生过大纲才有值，思路是这一步新加的），
    而正文是调用最密的路径。
    """
    with_thread = _capture_prompts(monkeypatch)
    _design_section(core_question="核心问题", writing_ideas="思路")
    without = _capture_prompts(monkeypatch)
    _design_section()

    assert without[0] != with_thread[0]
    assert "- 核心研究问题" not in without[0]
    assert "作者本人的意图" not in without[0]
    # 空串与不传参必须等价
    again = _capture_prompts(monkeypatch)
    _design_section(core_question="", writing_ideas="")
    assert again[0] == without[0]


def test_materials_and_design_are_both_included():
    """两条线同时存在时都在提示词里，且材料那句「不加角标」的话不被顶掉。"""
    text = generate_agent._format_materials(
        [{"label": "回归结果.xlsx", "text": "样本 312", "usage": "放 4.1", "key_points": ["β=0.42"]}],
        "数据源与样本：某平台 312 份问卷",
        "实证设计",
    )

    assert "作者自有研究材料" in text
    assert "不加角标" in text
    assert "回归结果.xlsx" in text
    assert "作者已给出的实证设计" in text
    assert "某平台 312 份问卷" in text


# ---------------------------------------------------------------
# 写作语言进正文提示词
# ---------------------------------------------------------------
def test_english_body_prompt_carries_the_output_rule(monkeypatch):
    """英文项目的正文提示词要带输出语言宣告，字数单位也要跟着换。

    正文是产出物的主体，也是**调用最密**的一处：提示词整段是中文指令，不显式宣告
    输出语言的话，模型顺着指令语言用中文交稿是最自然的反应 —— 而它交回来的是一整节
    「正文」，下游只看到「这一节写好了」，一个字都不会报错。
    """
    prompts = _capture_prompts(monkeypatch)
    _design_section(writing_lang="en")

    assert "**输出语言**" in prompts[0]
    assert "英文" in prompts[0]
    assert "目标字数约 300 words" in prompts[0]
    assert "300 字" not in prompts[0], "单位要跟着语言,否则英文项目按中文字数报目标"


def test_chinese_body_prompt_has_no_output_rule(monkeypatch):
    """中文路径逐字节不变（不传语言与显式传 zh 必须同一份提示词）。

    这是全部既有中文提示词断言的底座，也是「加语言这件事不许改动存量」的哨兵。
    """
    implicit = _capture_prompts(monkeypatch)
    _design_section()
    explicit = _capture_prompts(monkeypatch)
    _design_section(writing_lang="zh")

    assert explicit[0] == implicit[0]
    assert "输出语言" not in implicit[0]
    assert "目标字数约 300 字" in implicit[0]
