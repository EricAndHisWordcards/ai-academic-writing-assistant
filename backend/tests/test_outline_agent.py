"""测试：大纲生成与字数智能分配（outline_agent）。"""
import asyncio

import pytest

from app import paper_types
from app.agents import outline_agent


@pytest.mark.parametrize("target", [1000, 3000, 8000, 15000])
def test_outline_total_matches_target(target):
    """大纲所有叶子节点字数总和精确等于目标字数。"""
    outline = outline_agent._fallback_outline("定量/实证研究", target)
    total = _sum_budget(outline)
    assert total == target, f"期望 {target}，实际 {total}"


def test_outline_has_multi_level():
    """大纲至少包含章节层级结构。"""
    outline = outline_agent._fallback_outline("课程论文/小论文", 10000)
    assert "chapters" in outline
    assert len(outline["chapters"]) >= 3
    for ch in outline["chapters"]:
        assert "title" in ch
        assert "sections" in ch
        assert len(ch["sections"]) >= 1


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_all_paper_types_supported(paper_type):
    """所有论文类型都能生成大纲。"""
    outline = outline_agent._fallback_outline(paper_type, 5000)
    assert outline["chapters"], f"{paper_type} 大纲为空"


def test_rebalance_rounds_to_target():
    """_rebalance 能将字数按比例缩放到目标值。"""
    outline = {
        "chapters": [
            {"title": "章", "sections": [
                {"title": "1.1", "word_budget": 100, "subsections": []},
                {"title": "1.2", "word_budget": 200, "subsections": []},
            ]},
        ]
    }
    outline_agent._rebalance(outline, 900)
    total = _sum_budget(outline)
    assert total == 900


def _sum_budget(outline):
    total = 0
    for ch in outline.get("chapters", []):
        for sec in ch.get("sections", []):
            if sec.get("subsections"):
                total += sum(int(s.get("word_budget", 0)) for s in sec["subsections"])
            else:
                total += int(sec.get("word_budget", 0))
    return total


# ---------------------------------------------------------------
# _merge_with_skeleton：结构恒等于骨架，措辞尽量采纳 LLM
# ---------------------------------------------------------------
def _llm_like(paper_type, *, extra_subsections=False, chapters_delta=0):
    """构造一份「像 LLM 会返回的」大纲：标题改成贴题措辞，可选地多加子节/章节。"""
    chapters = []
    for i, (ch_title, secs) in enumerate(paper_types.template_for(paper_type)):
        ch = {"title": f"【LLM】{ch_title}", "sections": []}
        for sec_title, _ratio in secs:
            sec = {"title": f"【LLM】{sec_title}", "word_budget": 300}
            if extra_subsections:
                sec["subsections"] = [
                    {"title": f"{sec_title}-子节A", "word_budget": 150},
                    {"title": f"{sec_title}-子节B", "word_budget": 150},
                ]
            else:
                sec["subsections"] = []
            ch["sections"].append(sec)
        chapters.append(ch)
    for extra in range(chapters_delta):
        chapters.append({"title": f"多出来的第 {extra} 章", "sections": [
            {"title": "多余节", "word_budget": 100, "subsections": []},
        ]})
    return {"chapters": chapters}


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_merge_keeps_skeleton_structure(paper_type):
    """无论 LLM 返回什么，章数/节数/节顺序都等于骨架。"""
    outline = outline_agent._merge_with_skeleton(
        _llm_like(paper_type), paper_type, 8000
    )
    tmpl = paper_types.template_for(paper_type)
    assert len(outline["chapters"]) == len(tmpl)
    for ch, (ch_title, secs) in zip(outline["chapters"], tmpl):
        assert len(ch["sections"]) == len(secs)
        # 骨架是两级结构：叶子就在 section 上，不得冒出子节
        for sec in ch["sections"]:
            assert sec["subsections"] == []


def test_merge_adopts_llm_titles():
    """LLM 的贴题标题要被采纳，这是「大纲适配选题」的来源。"""
    outline = outline_agent._merge_with_skeleton(
        _llm_like("文献综述"), "文献综述", 8000
    )
    assert outline["chapters"][0]["title"] == "【LLM】第一章 引言"
    assert outline["chapters"][0]["sections"][0]["title"] == "【LLM】1.1 研究背景与意义"


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_merge_tolerates_extra_subsections(paper_type):
    """LLM 自行增加子节时，仍应采纳它的节标题，而不是整体丢弃。

    这是实测踩到的坑：模型很爱加子节，此前会导致它写的全部标题被弃用，
    只剩骨架的通用措辞。
    """
    outline = outline_agent._merge_with_skeleton(
        _llm_like(paper_type, extra_subsections=True), paper_type, 8000
    )
    assert outline["chapters"][0]["sections"][0]["title"].startswith("【LLM】")
    assert _sum_budget(outline) == 8000


def test_merge_tolerates_extra_chapters():
    """LLM 多写了章节：多余的被忽略，骨架顺序不变。"""
    outline = outline_agent._merge_with_skeleton(
        _llm_like("课程论文/小论文", chapters_delta=2), "课程论文/小论文", 5000
    )
    assert len(outline["chapters"]) == len(paper_types.template_for("课程论文/小论文"))
    assert _sum_budget(outline) == 5000


def test_merge_survives_garbage():
    """缺字段 / 非字典 / 空输入都要退化成纯骨架。"""
    for junk in (
        {},
        {"chapters": None},
        {"chapters": []},
        {"chapters": ["字符串", 123, None]},
        {"chapters": [{"title": "", "sections": "不是列表"}]},
    ):
        outline = outline_agent._merge_with_skeleton(junk, "文献综述", 6000)
        assert len(outline["chapters"]) == len(paper_types.template_for("文献综述"))
        assert _sum_budget(outline) == 6000
        # 标题回落到骨架措辞
        assert outline["chapters"][0]["sections"][0]["title"] == "1.1 研究背景与意义"


def test_flat_allocation_is_rejected():
    """模型把所有节拍成同一数值 = 均分，退回骨架比例。

    这是旧 _BUDGET_TOLERANCE 区间带当初要拦的失败模式；新区间带只拦「离谱值」，
    拦均分改由离散度检测负责，因为它针对的是「一样长」而不是「偏离骨架」。
    """
    outline = outline_agent._fallback_outline("文献综述", 5000)
    allocs = {i: {"words": 454, "rationale": "均分"} for i in range(1, 12)}
    outline_agent._apply_allocations(outline, allocs, "文献综述", 5000)

    leaves = outline_agent._iter_leaves(outline)
    assert sum(l["word_budget"] for l in leaves) == 5000
    # 回到骨架比例 0.14 * 5000 = 700，而不是均分出来的 ~454
    assert leaves[4]["word_budget"] == 700
    assert all(l["rationale"] == "" for l in leaves), "均分结果不该被采信"


def test_out_of_band_value_falls_back_per_leaf():
    """单节离谱值逐叶退回骨架，不牵连同一批里正常的节。"""
    outline = outline_agent._fallback_outline("文献综述", 5000)
    # 1.1 的骨架种子是 500：给 9000 远超 5 倍上限，该被丢弃
    allocs = {
        1: {"words": 9000, "rationale": "离谱"},
        5: {"words": 1200, "rationale": "核心主题，文献最多"},
        6: {"words": 1000, "rationale": "流派分歧大"},
    }
    outline_agent._apply_allocations(outline, allocs, "文献综述", 5000)

    leaves = outline_agent._iter_leaves(outline)
    assert leaves[0]["rationale"] == ""
    assert leaves[4]["rationale"] == "核心主题，文献最多"
    assert leaves[5]["rationale"] == "流派分歧大"
    assert sum(l["word_budget"] for l in leaves) == 5000


def test_merge_review_outline_has_no_empirical_chapters():
    """用 LLM 路径也要保证综述结构无实证色彩。"""
    outline = outline_agent._merge_with_skeleton(
        _llm_like("文献综述"), "文献综述", 6000
    )
    titles = " ".join(
        ch["title"] for ch in outline["chapters"]
    ) + " ".join(
        s["title"] for ch in outline["chapters"] for s in ch["sections"]
    )
    for banned in ("研究假设", "假设检验", "变量与数据", "描述性统计"):
        assert banned not in titles


def test_build_literature_context_caps_document_count():
    """文献清单必须有界，否则提示词会被撑爆。"""
    docs = [
        {"title": f"文献{i}", "authors": "张三", "year": "2023", "source": "期刊",
         "summary": f"摘要{i}", "pages": [{"page": 1, "snippet": "片段"}]}
        for i in range(100)
    ]
    ctx = outline_agent.build_literature_context(docs, max_docs=30)
    assert "文献29" in ctx
    assert "文献30" not in ctx
    assert "另有 70 篇文献未列出" in ctx


def test_build_literature_context_falls_back_to_snippet():
    """摘要缺失（未配置 LLM 时必然如此）要退回首页片段。"""
    docs = [{"title": "无摘要文献", "pages": [{"page": 1, "snippet": "首页里的正文片段"}]}]
    ctx = outline_agent.build_literature_context(docs)
    assert "首页里的正文片段" in ctx
    assert outline_agent.build_literature_context([]) == ""


# ---------------------------------------------------------------
# 字数对齐：最大余数法
# ---------------------------------------------------------------
def test_apportion_is_exact_and_spreads_residue():
    """除不尽时总和仍精确，且余数不落在末节。"""
    out = outline_agent._apportion([1, 1, 1], 100)
    assert sum(out) == 100
    assert sorted(out) == [33, 33, 34]

    # 旧实现把全部舍入残差堆在末节（末节 = target - 前面之和），末节会因此被压小
    # 或撑大。权重 10:10:1、目标 100 时，末节应为 5 而不是 4。
    out = outline_agent._apportion([10, 10, 1], 100)
    assert sum(out) == 100
    assert out[2] == 5, "余数应给小数部分最大的节，而不是全堆在末节"


def test_apportion_never_zero():
    """权重极小的节也要有字数。"""
    out = outline_agent._apportion([1, 10000], 60)
    assert sum(out) == 60
    assert all(w >= 1 for w in out), out


def test_apportion_handles_degenerate_input():
    """全零权重、空列表、非正目标都不该炸。"""
    assert outline_agent._apportion([], 100) == []
    assert sum(outline_agent._apportion([0, 0, 0], 90)) == 90
    assert outline_agent._apportion([1, 2], 0) == [0, 0]


# ---------------------------------------------------------------
# 两遍生成：结构 / 分配分离
# ---------------------------------------------------------------
def _alloc_payload(spec, retitles=None):
    """把 {序号: {...}} 的写法规整成模型实际会返回的 JSON 形状。

    直接传原始形状（含刻意构造的垃圾）时原样透传，便于测退化分支。
    """
    if isinstance(spec, dict) and all(isinstance(k, int) for k in spec):
        payload = {"allocations": [{"index": i, **v} for i, v in sorted(spec.items())]}
    else:
        payload = dict(spec) if spec is not None else {}
    if retitles is not None:
        payload["retitles"] = [
            {"index": i, "title": t} for i, t in sorted(retitles.items())
        ]
    return payload


def _stub_llm(monkeypatch, *, structure=None, allocations=None, retitles=None,
              structure_raises=False, alloc_raises=False):
    """替换 llm.chat_json，按提示词内容分发两遍的输出，并返回收到的提示词列表。"""
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()

    calls: list[str] = []

    async def fake(messages, **kw):
        prompt = messages[0]["content"]
        calls.append(prompt)
        if "篇幅规划专家" in prompt:          # 第二遍
            if alloc_raises:
                raise RuntimeError("分配服务不可用")
            if isinstance(allocations, dict) and allocations is not None:
                return _alloc_payload(allocations, retitles)
            return _alloc_payload(allocations)
        if structure_raises:                  # 第一遍
            raise RuntimeError("大纲服务不可用")
        return structure if structure is not None else {}

    monkeypatch.setattr(llm, "chat_json", fake)
    return calls


def _outline(topic, paper_type, target, ctx="", count=0, on_phase=None):
    return asyncio.run(
        outline_agent.generate_outline(topic, paper_type, target, ctx, count, on_phase)
    )


def test_allocation_prompt_sees_titles_and_literature(monkeypatch):
    """分配那遍必须看到定稿标题与文献清单 —— 否则无从「按内容体量」分配。"""
    allocs = {i: {"words": w, "rationale": f"依据{i}"} for i, w in enumerate(
        [1100, 500, 700, 600, 900, 650, 500, 400, 250, 200, 300], 1)}
    calls = _stub_llm(monkeypatch, structure=_llm_like("文献综述"), allocations=allocs)

    _outline("选题", "文献综述", 5000,
             "1. 某文献\n   摘要：智能辅导系统", 1)

    assert len(calls) == 2, "应当是两遍调用"
    assert "大纲专家" in calls[0]
    assert "篇幅规划专家" in calls[1]
    assert "【LLM】" in calls[1], "分配必须基于定稿后的标题，而不是骨架措辞"
    assert "智能辅导系统" in calls[1], "分配必须能看到文献清单"


def test_allocation_prompt_carries_the_language_declaration(monkeypatch):
    """第二遍也要宣告输出语言 —— 它不只算字数，还产出 `retitles`。

    而那批修订标题会被 `_apply_allocations` 覆盖到叶子上、**印进论文**。第一遍的
    OUTLINE_PROMPT 早就带了 `{output_rule}`，第二遍漏了：英文项目里模型只看到中文指令，
    于是一边按词数分配一边把节标题改回中文，而这一路没有任何东西会报错。

    中文侧必须一个字节都不变 —— 占位符与 `{literature_block}` **同行、无分隔**就是
    为了这一点（`output_rule("zh")` 是空串，另起一行会多出一个尾部换行）。所以这里断言
    中文提示词**原样收尾**于文献块本身。
    """
    allocs = {i: {"words": 500, "rationale": f"依据{i}"} for i in range(1, 12)}
    ctx = "1. 某文献\n   摘要：智能辅导系统"
    tail = outline_agent._literature_block(ctx, 1, sizing=True)

    calls_en = _stub_llm(monkeypatch, structure=_llm_like("文献综述"), allocations=allocs)
    asyncio.run(outline_agent.generate_outline(
        "选题", "文献综述", 5000, ctx, 1, writing_lang="en"
    ))
    assert len(calls_en) == 2, "应当是两遍调用"
    assert "输出语言" in calls_en[1], "第二遍必须也宣告输出语言"

    calls_zh = _stub_llm(monkeypatch, structure=_llm_like("文献综述"), allocations=allocs)
    asyncio.run(outline_agent.generate_outline("选题", "文献综述", 5000, ctx, 1))
    assert "输出语言" not in calls_zh[1], "中文侧不得多出任何字样"
    assert calls_zh[1].endswith(tail), "中文提示词必须原样收尾于文献块（无新增分隔）"


def test_allocation_carries_rationale_and_keeps_totals(monkeypatch):
    """每节的字数依据要写回大纲，总和仍精确等于目标。"""
    allocs = {i: {"words": w, "rationale": f"依据{i}"} for i, w in enumerate(
        [1100, 500, 700, 600, 900, 650, 500, 400, 250, 200, 300], 1)}
    _stub_llm(monkeypatch, structure=_llm_like("文献综述"), allocations=allocs)

    outline = _outline("选题", "文献综述", 5000)

    leaves = outline_agent._iter_leaves(outline)
    assert sum(l["word_budget"] for l in leaves) == 5000
    assert all(l["rationale"] for l in leaves), "每节都该带上依据"


def test_allocation_lets_conclusion_be_shorter_than_core(monkeypatch):
    """结论节应当短于核心主题节。

    直接回归实测缺陷：文献综述里「第五章 结论与展望」两节各 500 字，与「第三章
    研究主题脉络」的核心节（450-500 字）等长 —— 收束章节成了全文最长部分。
    """
    allocs = {
        5: {"words": 1100, "rationale": "文献最密集"},
        6: {"words": 1000, "rationale": "流派分歧大"},
        7: {"words": 800, "rationale": "争议需展开"},
        10: {"words": 150, "rationale": "只回应前文结论"},
        11: {"words": 150, "rationale": "只指出方向"},
    }
    _stub_llm(monkeypatch, structure=_llm_like("文献综述"), allocations=allocs)

    outline = _outline("选题", "文献综述", 5000)

    leaves = outline_agent._iter_leaves(outline)
    core, conclusion = leaves[4], leaves[9]
    assert core["word_budget"] > conclusion["word_budget"] * 5, \
        f"核心主题节 {core['word_budget']} 应远重于结论节 {conclusion['word_budget']}"
    assert leaves[9]["word_budget"] + leaves[10]["word_budget"] < core["word_budget"]


def test_allocation_not_a_uniform_quota(monkeypatch):
    """模型按内容推算的具体数字不该被抹成一组凑整的固定配额。

    这是用户所报缺陷的直接回归测试：旧实现下每个数字都是 50 的整数倍。
    """
    reasoned = [503, 397, 611, 488, 726, 664, 447, 492, 197, 251, 324]
    allocs = {i: {"words": w, "rationale": "按文献量推算"} for i, w in enumerate(reasoned, 1)}
    _stub_llm(monkeypatch, structure=_llm_like("文献综述"), allocations=allocs)

    outline = _outline("选题", "文献综述", 5000)

    budgets = [l["word_budget"] for l in outline_agent._iter_leaves(outline)]
    assert sum(budgets) == 5000
    assert not all(b % 50 == 0 for b in budgets), f"不该是凑整配额：{budgets}"
    # 比例关系要保留：分配最多的节仍应显著重于最少的节
    assert budgets[4] > budgets[8] * 2


def test_allocation_falls_back_per_leaf_when_partial(monkeypatch):
    """只给部分节分配时，已采纳的保留、缺的逐叶回落骨架，而不是整批丢弃。"""
    allocs = {5: {"words": 1200, "rationale": "核心主题"}}
    _stub_llm(monkeypatch, structure=_llm_like("文献综述"), allocations=allocs)

    outline = _outline("选题", "文献综述", 5000)

    leaves = outline_agent._iter_leaves(outline)
    assert leaves[4]["rationale"] == "核心主题"
    assert leaves[4]["word_budget"] > leaves[5]["word_budget"]
    assert sum(l["word_budget"] for l in leaves) == 5000


def test_allocation_garbage_output_degrades_quietly(monkeypatch):
    """分配返回垃圾（缺字段/非数字/非列表）时退回骨架比例，大纲照常产出。"""
    for junk in ({}, {"allocations": None}, {"allocations": []},
                 {"allocations": ["字符串", 42, {"index": 1}]},
                 {"allocations": [{"index": 1, "words": "很多"}]}):
        _stub_llm(monkeypatch, structure=_llm_like("课程论文/小论文"), allocations=junk)
        outline = _outline("选题", "课程论文/小论文", 3000)
        leaves = outline_agent._iter_leaves(outline)
        assert sum(l["word_budget"] for l in leaves) == 3000
        assert all(l["rationale"] == "" for l in leaves)


def test_outline_survives_allocation_failure(monkeypatch):
    """分配那遍抛异常，大纲仍要完整返回，字数退回骨架比例并精确对齐。

    分配失败只该让字数差一点，不该让整个大纲生成失败。
    """
    _stub_llm(monkeypatch, structure=_llm_like("课程论文/小论文"), alloc_raises=True)

    outline = _outline("选题", "课程论文/小论文", 3000)

    leaves = outline_agent._iter_leaves(outline)
    assert sum(l["word_budget"] for l in leaves) == 3000
    # 结构仍然来自模型，没有整体退化成骨架措辞
    assert outline["chapters"][0]["sections"][0]["title"].startswith("【LLM】")


# ---------------------------------------------------------------
# 核心研究问题：全篇围绕同一条主线
# ---------------------------------------------------------------
def _with_core_question(paper_type, core):
    data = _llm_like(paper_type)
    data["core_question"] = core
    return data


def test_core_question_is_carried_into_outline(monkeypatch):
    """第一遍写下的核心研究问题要落进大纲，供界面显式展示。"""
    core = "企业社交媒体的使用如何影响隐性知识共享，其双刃剑效应通过什么机制产生？"
    _stub_llm(monkeypatch, structure=_with_core_question("文献综述", core),
              allocations={5: {"words": 1200, "rationale": "文献最多"}})

    outline = _outline("选题", "文献综述", 5000)

    assert outline["core_question"] == core


@pytest.mark.parametrize("junk", [None, "", "   ", 123, ["问题"], "很长" * 60])
def test_core_question_junk_falls_back_to_blank(monkeypatch, junk):
    """非字符串 / 空 / 超长都置空 —— 前端据此不显示这一行，而不是显示垃圾。"""
    _stub_llm(monkeypatch, structure=_with_core_question("课程论文/小论文", junk))

    outline = _outline("选题", "课程论文/小论文", 3000)

    assert outline["core_question"] == ""


def test_fallback_outline_has_blank_core_question():
    """无 LLM 可用时没有核心研究问题可写，置空串而不是编一个。"""
    assert outline_agent._fallback_outline("课程论文/小论文", 3000)["core_question"] == ""


def test_allocation_prompt_carries_core_question(monkeypatch):
    """复核标题需要基准 —— 第二遍必须拿到第一遍定下的核心研究问题。"""
    core = "社交媒体如何影响隐性知识共享？"
    calls = _stub_llm(
        monkeypatch,
        structure=_with_core_question("文献综述", core),
        allocations={5: {"words": 1200, "rationale": "文献最多"}},
    )

    _outline("选题", "文献综述", 5000)

    assert core in calls[1]


def test_retitle_is_applied(monkeypatch):
    """第二遍的标题修订要写回大纲：跑偏的节被改回贴题。"""
    drifted = _llm_like("文献综述")
    drifted["chapters"][2]["sections"][1]["title"] = "3.2 数字化转型与零售4.0语境下的知识流动"
    drifted["core_question"] = "社交媒体如何影响隐性知识共享？"
    _stub_llm(
        monkeypatch, structure=drifted,
        allocations={5: {"words": 1200, "rationale": "文献最多"}},
        retitles={6: "3.2 隐性知识共享的边界跨越机制"},
    )

    outline = _outline("选题", "文献综述", 5000)

    leaves = outline_agent._iter_leaves(outline)
    assert leaves[5]["title"] == "3.2 隐性知识共享的边界跨越机制"


@pytest.mark.parametrize("bad", [
    {6: ""},                 # 空标题
    {6: "   "},              # 只有空白
    {6: 123},                # 非字符串
    {6: "标题" * 40},         # 超长，与 _clean_title 同一把尺子
    {0: "序号越界"},           # 序号越界
    {99: "序号越界"},
])
def test_bad_retitle_is_ignored(monkeypatch, bad):
    """坏修订宁可不要：不能让它把标题改成一个新的跑偏。"""
    original = _llm_like("文献综述")
    original["core_question"] = "核心问题"
    _stub_llm(monkeypatch, structure=original,
              allocations={5: {"words": 1200, "rationale": "文献最多"}},
              retitles=bad)

    outline = _outline("选题", "文献综述", 5000)

    assert outline_agent._iter_leaves(outline)[5]["title"] == "【LLM】3.2 主题二：主要流派与观点"


def test_retitle_survives_flat_allocation_rejection(monkeypatch):
    """字数被判为「均分凑数」而整批退回骨架时，贴题的标题修订仍要采纳。"""
    original = _llm_like("文献综述")
    original["core_question"] = "核心问题"
    flat = {i: {"words": 454, "rationale": "均分"} for i in range(1, 12)}
    _stub_llm(monkeypatch, structure=original, allocations=flat,
              retitles={6: "3.2 收束到核心问题的主题维度"})

    outline = _outline("选题", "文献综述", 5000)

    leaves = outline_agent._iter_leaves(outline)
    assert all(l["rationale"] == "" for l in leaves), "均分结果不该被采信"
    assert leaves[5]["title"] == "3.2 收束到核心问题的主题维度"


def test_outline_prompt_demands_a_single_core_question():
    """提示词必须显式要求先立主线、禁止把单篇文献的情境当成章节主题。

    实测的跑偏（「3.2 数字化转型与零售4.0语境下……」）根因就是此前提示词里
    没有任何一句要求「全篇围绕同一个核心研究问题」。
    """
    prompt = outline_agent.OUTLINE_PROMPT
    assert "core_question" in prompt
    assert "核心研究问题" in prompt
    assert "零售4.0" in prompt, "要给出直指缺陷的禁例，而不是抽象的「要连贯」"


def test_literature_block_no_longer_invites_single_paper_context():
    """拟标题分支不得再邀请模型把单篇文献的窄情境升格成综述主题。

    旧措辞是「把节标题替换为从这些文献中归纳出的**具体主题名**」——正是它
    产出了与核心问题无关的节标题。
    """
    block = outline_agent._literature_block("1. 某文献\n   摘要：零售4.0", 1)

    assert "替换为从这些文献中归纳出的" not in block
    assert "不要把某篇文献的具体行业、技术、情境或案例当作主题维度" in block
    # sizing=True（分配用）那一支保持原样
    sizing = outline_agent._literature_block("1. 某文献", 1, sizing=True)
    assert "覆盖文献多、结论有分歧的小节篇幅应更大" in sizing


# ---------------------------------------------------------------
# on_phase：两遍边界的进度回调
# ---------------------------------------------------------------
def test_on_phase_reports_both_passes(monkeypatch):
    """回调要按 structure → allocating 的顺序报告，并带上结构规模。"""
    _stub_llm(monkeypatch, structure=_llm_like("文献综述"),
              allocations={5: {"words": 1200, "rationale": "文献最多"}})
    seen: list[tuple[str, dict]] = []

    _outline("选题", "文献综述", 5000, on_phase=lambda p, i: seen.append((p, i)))

    assert [p for p, _ in seen] == ["structure", "allocating"]
    info = dict(seen[1][1])
    assert info["chapters"] == len(paper_types.template_for("文献综述"))
    assert info["sections"] == 11


def test_on_phase_failure_does_not_break_generation(monkeypatch):
    """进度回调是尽力而为：它抛错不能毁掉几十秒的生成结果。"""
    _stub_llm(monkeypatch, structure=_llm_like("课程论文/小论文"))

    def boom(phase, info):
        raise RuntimeError("进度落盘失败")

    outline = _outline("选题", "课程论文/小论文", 3000, on_phase=boom)

    assert outline["chapters"]
    assert sum(l["word_budget"] for l in outline_agent._iter_leaves(outline)) == 3000


def test_on_phase_is_optional(monkeypatch):
    """不传回调时一切照旧（既有调用方无需改动）。"""
    _stub_llm(monkeypatch, structure=_llm_like("课程论文/小论文"))
    assert _outline("选题", "课程论文/小论文", 3000)["chapters"]


# ---------------------------------------------------------------
# 研究设计：进大纲提示词，且只在给的时候进
# ---------------------------------------------------------------
def _outline_with_design(monkeypatch, paper_type, design, label):
    calls = _stub_llm(monkeypatch, structure=_llm_like(paper_type))
    asyncio.run(outline_agent.generate_outline(
        "选题", paper_type, 3000,
        design_context=design, design_label=label,
    ))
    return calls[0]


def test_design_context_reaches_outline_prompt(monkeypatch):
    """设计字段必须进第一遍提示词，并带上禁编造规则。

    实证研究第四章原本靠模型编数据，根因就是这些字段从来没进过提示词。
    """
    design = "数据源与样本：某平台 2023 年 312 份问卷\n主要结果：β=0.42，p<0.01"
    prompt = _outline_with_design(monkeypatch, "定量/实证研究", design, "实证设计")

    assert "作者已给出的实证设计" in prompt
    assert "β=0.42" in prompt
    assert "严禁编造" in prompt
    assert "需作者补充" in prompt, "缺数据的地方要如实留白，而不是补一个假结果"


def test_outline_prompt_is_unchanged_without_design(monkeypatch):
    """没填设计时提示词里一个字的痕迹都不留 —— 这是防回归的关键。

    没有设计步的类型（文献综述、课程论文/小论文）恒走这条路；同一类型内
    「填了 / 没填」也必须是两种不同的提示词。
    """
    with_design = _outline_with_design(
        monkeypatch, "定量/实证研究", "数据源：A 平台", "实证设计"
    )
    without = _outline_with_design(monkeypatch, "定量/实证研究", "", "")

    assert "作者已给出的" not in without
    assert without != with_design
    # 空串与不传参必须等价（routers 两种写法都存在）
    monkey_stub = _stub_llm(monkeypatch, structure=_llm_like("定量/实证研究"))
    asyncio.run(outline_agent.generate_outline("选题", "定量/实证研究", 3000))
    assert monkey_stub[0] == without


def test_type_note_differs_per_type_but_is_never_blank(monkeypatch):
    """类型说明必须逐类不同、且永不缺失。

    七类共用一套提示词，类型说明是它们唯一的差别来源。若某一类的说明为空，
    （或因为参数传错而全部取到同一段）那一类就会拿到另一类的写法 —— 结构上
    看不出来，写出来是另一门学科的话。
    """
    prompts = {}
    for paper_type in paper_types.PAPER_TYPES:
        calls = _stub_llm(monkeypatch, structure=_llm_like(paper_type))
        asyncio.run(outline_agent.generate_outline("选题", paper_type, 3000))
        prompts[paper_type] = calls[0]
        note = paper_types.structure_note_for(paper_type)
        assert note.strip() in calls[0], f"{paper_type} 的类型说明没有进提示词"

    assert len(prompts) == len(paper_types.PAPER_TYPES)
    # 逐份比对而不是「都非空」：两类的说明写重了，两类的论文就会拿到同一套写法
    assert len(set(prompts.values())) == len(prompts), "七类的大纲提示词应当各不相同"


def test_unknown_type_falls_back_to_the_default_type_note(monkeypatch):
    """未知类型退化到默认类型的那一份说明，而不是留空。

    留空的后果不是「少一句话」：提示词里会剩下一段没有内容的规则，模型就自己去猜
    该有什么章节。退化则与 template_for / config_for 的兜底一致 —— 骨架是课程论文的，
    写法也必须跟着是，不能一个退化一个不退。
    """
    default_key = paper_types.DEFAULT_TEMPLATE_KEY
    calls = _stub_llm(monkeypatch, structure=_llm_like(default_key))
    asyncio.run(outline_agent.generate_outline("选题", "不存在的类型", 3000))

    fallback_note = paper_types.structure_note_for("不存在的类型")
    assert fallback_note, "退化后必须仍有一段说明"
    assert fallback_note == paper_types.structure_note_for(default_key)
    assert fallback_note.strip() in calls[0]
    # 「新块为空 ⇒ 整块消失」这条纪律由空串触发：真正没有说明时不留空标题
    assert not outline_agent._type_note_block("")
    assert not outline_agent._type_note_block("   \n  ")


def test_allocation_prompt_gets_no_design_block(monkeypatch):
    """第二遍（字数分配 + 标题复核）不加设计槽：它只管篇幅与贴题。"""
    calls = _stub_llm(monkeypatch, structure=_llm_like("定量/实证研究"))
    asyncio.run(outline_agent.generate_outline(
        "选题", "定量/实证研究", 3000,
        design_context="主要结果：β=0.42", design_label="实证设计",
    ))

    assert len(calls) == 2
    assert "β=0.42" in calls[0]
    assert "β=0.42" not in calls[1]


# ---------------------------------------------------------------
# 作者写作思路：只进第一遍，且只在写过的时候进
# ---------------------------------------------------------------
IDEAS = "先辨析 A 与 B 两个概念，再从机制层面论证二者互补而非替代"


def _outline_with_ideas(monkeypatch, ideas):
    calls = _stub_llm(monkeypatch, structure=_llm_like("课程论文/小论文"))
    asyncio.run(outline_agent.generate_outline(
        "选题", "课程论文/小论文", 3000, writing_ideas=ideas,
    ))
    return calls


def test_writing_ideas_reach_the_outline_prompt(monkeypatch):
    """作者写下的思路必须进第一遍，并要求 core_question 收敛它、以它为准。

    第一遍本来就在自己写 core_question —— 那一步此前是**替作者定主线**。作者既然
    写了思路，模型的职责就退回到「把这段思路收敛成一句话」。
    """
    prompt = _outline_with_ideas(monkeypatch, IDEAS)[0]

    assert IDEAS in prompt
    assert "作者本人的意图，优先于你自己的归纳" in prompt
    assert "以作者写下的这段为准" in prompt


def test_ideas_block_is_empty_without_ideas():
    """没写思路时这一段整块消失（与设计块、聚类块同一条纪律）。"""
    assert outline_agent._ideas_block("") == ""
    assert outline_agent._ideas_block("   \n ") == ""
    assert IDEAS in outline_agent._ideas_block(f"  {IDEAS}  ")


def test_outline_prompt_is_unchanged_without_ideas(monkeypatch):
    """没写思路时第一遍提示词逐字节不变 —— 存量项目里一个字的痕迹都不留。

    没有这一步，「加了个输入框」就会把每一份既有项目的提示词都改一遍，而实测基线
    与既有断言都跟着动。
    """
    with_ideas = _outline_with_ideas(monkeypatch, IDEAS)[0]
    without = _outline_with_ideas(monkeypatch, "")[0]

    assert "作者本人的意图" not in without
    assert without != with_ideas
    # 空串与不传参必须等价（routers 两种写法都存在）
    monkey_stub = _stub_llm(monkeypatch, structure=_llm_like("课程论文/小论文"))
    asyncio.run(outline_agent.generate_outline("选题", "课程论文/小论文", 3000))
    assert monkey_stub[0] == without


def test_allocation_prompt_gets_no_ideas_block(monkeypatch):
    """第二遍（字数分配 + 标题复核）不加思路槽。

    它只管篇幅与贴题，而它复核所依据的 core_question 已在第一遍吸收了这段思路 ——
    把原文再塞一遍，就是给第二遍两个互斥的依据（同 test_allocation_prompt_gets_no_design_block）。
    """
    calls = _outline_with_ideas(monkeypatch, IDEAS)

    assert len(calls) == 2
    assert IDEAS in calls[0]
    assert IDEAS not in calls[1]


# ---------------------------------------------------------------
# 主题聚类：只进第一遍，且只在确认过的时候进
# ---------------------------------------------------------------
CLUSTERS = [
    {"id": "c1", "title": "促进与抑制效应并存", "summary": "两个方向同时存在",
     "doc_ids": ["d1"], "doc_titles": ["文献一"],
     "key_points": ["A 发现正向", "B 发现负向"]},
]


def _outline_with_clusters(monkeypatch, clusters, theme_sections=0, paper_type="文献综述"):
    calls = _stub_llm(monkeypatch, structure=_llm_like(paper_type))
    asyncio.run(outline_agent.generate_outline(
        "选题", paper_type, 3000, "1. 某文献\n   摘要：x", 1,
        clusters=clusters, theme_sections=theme_sections,
    ))
    return calls


def test_confirmed_clusters_reach_the_first_pass_only(monkeypatch):
    """确认过的聚类带上优先级进第一遍，第二遍看不到它。

    第二遍只管篇幅与贴题，它的篇幅依据应当是各主题下文献的实际分量
    （_literature_block(sizing=True)）；再把「已确认的维度」摆一次，模型会拿
    簇数当字数依据，分出来的篇幅就与文献分量脱钩了。
    """
    calls = _outline_with_clusters(monkeypatch, CLUSTERS, theme_sections=3)

    assert len(calls) == 2
    assert "作者已确认的主题聚类" in calls[0]
    assert "促进与抑制效应并存" in calls[0]
    assert "优先于你自己的归纳" in calls[0]
    assert "促进与抑制效应并存" not in calls[1]


def test_clusters_replace_the_self_induced_instruction(monkeypatch):
    """给了确认过的聚类时，「自己归纳 2–4 个主题维度」那句必须消失。

    两句同时在场等于两条互相矛盾的指令，模型会挑对自己省事的那条（自己重新归纳），
    作者确认过的聚类就白确认了。
    """
    with_clusters = _outline_with_clusters(monkeypatch, CLUSTERS, theme_sections=3)[0]
    without = _outline_with_clusters(monkeypatch, [], theme_sections=3)[0]

    assert "2–4 个" not in with_clusters
    assert "2–4 个" in without, "没有聚类时仍要请模型自己归纳主题维度"


def test_cluster_count_mismatch_says_what_to_do(monkeypatch):
    """簇数与主题章节数不一致时必须给出具体办法。

    只说「不一致」等于没说：模型要么硬塞（丢掉维度间的界限），要么自己另立主题
    （不再是作者确认的结果）。多则合并、少则拆分，两个方向都要说清。
    """
    many = [dict(c, id=f"c{i}", title=f"主题{i}") for i, c in enumerate(CLUSTERS * 4, 1)]
    more = _outline_with_clusters(monkeypatch, many, theme_sections=3)[0]
    assert "把关系最近的维度合并" in more
    assert "3 节" in more

    fewer = _outline_with_clusters(monkeypatch, CLUSTERS, theme_sections=5)[0]
    assert "拆成多节" in fewer
    assert "5 节" in fewer

    equal = _outline_with_clusters(monkeypatch, CLUSTERS, theme_sections=1)[0]
    assert "一簇对应一节" in equal


def test_cluster_block_is_empty_without_clusters():
    """空输入返回空串 —— 整块消失，而不是留一段空标题让模型去猜。"""
    assert outline_agent._clusters_block([]) == ""
    assert outline_agent._clusters_block(None) == ""
    assert outline_agent._clusters_block([{"title": ""}]) == ""


def test_literature_block_is_byte_identical_without_clusters():
    """不给聚类时，`_literature_block` 逐字节等于加聚类之前的样子。

    只传 `clusters=[]`（未确认时的默认值）与完全不传必须等价 —— 调用方两种写法
    都存在（_run_outline 恒传 clusters，生成大纲的其它路径不传）。
    """
    ctx, n = "1. 某文献\n   摘要：x", 1
    base = outline_agent._literature_block(ctx, n)
    assert outline_agent._literature_block(ctx, n, clusters=[], theme_sections=0) == base
    assert outline_agent._literature_block(ctx, n, clusters=None, theme_sections=3) == base
    assert "主题聚类" not in base


# ---------------------------------------------------------------
# 英文项目（本轮新增）：骨架、字数单位、输出语言宣告、字符上限
#
# 这一组里有两条是**护栏**而不是功能测试：
# ① `_stub_llm` 靠中文串「篇幅规划专家」分发第二遍，提示词一改就会让本文件里几十个
#    用例静默走错分支 —— 它们照样「通过」，只是通过的是另一条路；
# ② 中文路径下提示词逐字节不变，是保住那批中文提示词断言的前提。
# ---------------------------------------------------------------
_EN_TOPIC = "The effect of enterprise social media on tacit knowledge sharing"


def test_english_outline_uses_the_english_skeleton(monkeypatch):
    """英文项目：标题取英文骨架，两遍调用照旧，第一遍带输出语言宣告。"""
    calls = _stub_llm(monkeypatch, structure={}, allocations={})

    outline = asyncio.run(outline_agent.generate_outline(
        _EN_TOPIC, "定量/实证研究", 4000, writing_lang="en",
    ))

    titles = [ch["title"] for ch in outline["chapters"]]
    assert titles == [t for t, _secs in paper_types.template_for("定量/实证研究", "en")]
    assert titles[0] == "Chapter 1 Introduction"

    assert len(calls) == 2, "两遍调用与语言无关"
    assert "大纲专家" in calls[0]
    assert "篇幅规划专家" in calls[1], "第二遍靠这个中文串分发，不能跟着语言换"
    assert "**输出语言**" in calls[0], "英文路径要带上输出语言宣告"
    assert "4000 words" in calls[1], "字数单位也要跟着语言"


def test_english_outline_prints_no_chinese_titles(monkeypatch):
    """落进大纲的章/节标题里一个汉字都不能有 —— 它会原样印进论文。

    `structure={}` 走的是「模型什么都没给」的骨架兜底，正是这种情况下用户会看到
    的那一份，所以它必须已经是英文的。
    """
    _stub_llm(monkeypatch, structure={}, allocations={})

    outline = asyncio.run(outline_agent.generate_outline(
        _EN_TOPIC, "文献综述", 5000, writing_lang="en",
    ))

    text = "\n".join(ch["title"] for ch in outline["chapters"])
    text += "\n" + "\n".join(
        sec["title"] for ch in outline["chapters"] for sec in ch["sections"]
    )
    for char in text:
        assert not ("一" <= char <= "鿿"), text


def test_fallback_outline_uses_the_english_skeleton():
    """没配 LLM 时的那条路也要给英文骨架（它就是「未配置」下用户看到的大纲）。"""
    outline = outline_agent._fallback_outline("课程论文/小论文", 3000, "en")

    assert [c["title"] for c in outline["chapters"]] == [
        t for t, _secs in paper_types.template_for("课程论文/小论文", "en")
    ]


def test_english_type_note_reaches_the_outline_prompt(monkeypatch):
    """类型说明也得跟着语言换：英文骨架下那些章叫 `Research Hypotheses`。"""
    calls = _stub_llm(monkeypatch, structure={}, allocations={})
    asyncio.run(outline_agent.generate_outline(
        _EN_TOPIC, "定量/实证研究", 4000, writing_lang="en",
    ))

    assert paper_types.structure_note_for("定量/实证研究", "en") in calls[0]
    assert "本类型是定量" not in calls[0]


def test_zh_path_has_no_output_rule(monkeypatch):
    """中文路径下不追加任何输出语言宣告 —— 「zh 逐字节不变」的落点就在这里。"""
    calls = _stub_llm(monkeypatch, structure={}, allocations={})

    _outline("测试选题", "文献综述", 5000)

    assert "输出语言" not in calls[0]
    assert "输出语言" not in calls[1]
    assert "5000 字" in calls[1], "中文下字数单位仍是「字」"


def test_title_cap_widens_for_english():
    """英文标题的上限放宽三倍：60 个字符只有约十个词，正常标题会被切在中途。"""
    realistic = "The Double-Edged Effect of Enterprise Social Media on Tacit Knowledge Sharing"
    assert 60 < len(realistic) <= 180, "这条得真落在两个上限之间，否则测不到东西"

    assert outline_agent._clean_title(realistic, "回退标题", "en") == realistic
    assert outline_agent._clean_title(realistic, "回退标题") == "回退标题"
    # 放宽之后仍要有上限:模型失控吐一段正文时不能整段进标题
    assert outline_agent._clean_title("A" * 400, "回退标题", "en") == "回退标题"


def test_core_question_cap_widens_for_english():
    """核心研究问题同理 —— 100 个字符放不下一个英文长问句。"""
    question = (
        "How does the use of enterprise social media by employees affect tacit "
        "knowledge sharing, and through what mechanisms does this double-edged "
        "effect arise?"
    )
    assert 100 < len(question) <= 300, "这条得真落在两个上限之间"

    assert outline_agent._clean_core_question(question, "en") == question
    assert outline_agent._clean_core_question(question) == ""


def test_retitle_cap_widens_for_english():
    """修订标题与 `_clean_title` 同一把尺子，也必须同宽 —— 否则「修订」本身就成了新的跑偏。"""
    long_title = "R" * 120
    data = {"retitles": [{"index": 1, "title": long_title}]}

    assert outline_agent._parse_retitles(data) == {}
    assert outline_agent._parse_retitles(data, "en") == {1: long_title}


def test_rationale_keep_cap_widens_for_english():
    """依据落库前还要再截一次（界面上只显示一行），这一处也按语言放宽。"""
    data = {"allocations": [{"index": 1, "words": 500, "rationale": "x" * 500}]}

    assert len(outline_agent._parse_allocations(data)[1]["rationale"]) == 60
    assert len(outline_agent._parse_allocations(data, "en")[1]["rationale"]) == 180
