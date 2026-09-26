"""测试：引用相关性 Agent（relevance_agent）。

这个 agent 的产物决定「哪篇文献进哪一节」，所以它最要紧的两条性质是：
1. **不把不存在的东西当成真的** —— 编出来的 doc id 与大纲外的章节标题一律丢弃
   （与 cluster_agent 同一把尺子，同源底线是「0 幻觉引用」）；
2. **拿不到判断时返回 None，不返回半份** —— 半份判断会让调用方以为「模型判过了」，
   从而不去做兜底归属；None 才是「这次判断不可用」的唯一信号。
"""
import asyncio

from app.agents import relevance_agent, schedule_agent
from app.llm import llm


def _outline():
    return {
        "chapters": [
            {"title": "第一章 引言", "sections": [
                {"title": "1.1 研究背景", "word_budget": 500, "subsections": []},
                {"title": "1.2 研究问题", "word_budget": 500, "subsections": []},
            ]},
            {"title": "第二章 主体论述", "sections": [
                {"title": "2.1 现状与问题", "word_budget": 800,
                 "subsections": [{"title": "2.1.1 现状", "word_budget": 400},
                                 {"title": "2.1.2 问题", "word_budget": 400}]},
            ]},
        ]
    }


def _docs(n=3):
    return [
        {"id": f"d{i}", "title": f"文献{i}", "authors": "张三", "year": "2023",
         "source": "某期刊", "summary": f"文献{i}研究的是{i}号问题", "pages": []}
        for i in range(1, n + 1)
    ]


def _stub_llm(monkeypatch, payload):
    """打开 LLM 并打桩 chat_json，返回收到的提示词列表。"""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()
    calls: list[str] = []

    async def fake(messages, **kw):
        calls.append(messages[0]["content"])
        return payload

    monkeypatch.setattr(llm, "chat_json", fake)
    return calls


def _assign(payload_or_exc, monkeypatch, docs=None, outline=None):
    calls = _stub_llm(monkeypatch, payload_or_exc)
    result = asyncio.run(relevance_agent.assign_by_relevance(
        outline or _outline(), docs if docs is not None else _docs(), paper_type="课程论文/小论文",
    ))
    return result, calls


# ---------------------------------------------------------------
# 拿不到判断就返回 None
# ---------------------------------------------------------------
def test_offline_returns_none():
    """未配置 LLM 时返回 None —— 调用方据此退回确定性兜底。"""
    result = asyncio.run(relevance_agent.assign_by_relevance(_outline(), _docs()))
    assert result is None


def test_no_documents_returns_none():
    assert asyncio.run(relevance_agent.assign_by_relevance(_outline(), [])) is None


def test_outline_without_leaf_sections_returns_none():
    """没有可引用的叶子章节时不发这次调用（发了也只会得到一堆会被丢弃的标题）。"""
    assert asyncio.run(
        relevance_agent.assign_by_relevance({"chapters": []}, _docs())
    ) is None


def test_non_dict_payload_returns_none(monkeypatch):
    assert _assign(["不是字典"], monkeypatch)[0] is None


def test_missing_assignments_key_returns_none(monkeypatch):
    assert _assign({"notes": "我只想说句话"}, monkeypatch)[0] is None


def test_llm_exception_returns_none(monkeypatch):
    """调用抛异常时返回 None，不把异常抛给路由 —— 它是同步请求，抛出来就是 500。

    （生产路径上更常见的是超时与坏 JSON，两者都在这一条里。）
    """
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    llm.reload()

    async def boom(messages, **kw):
        raise RuntimeError("模型挂了")

    monkeypatch.setattr(llm, "chat_json", boom)
    assert asyncio.run(
        relevance_agent.assign_by_relevance(_outline(), _docs())
    ) is None


def test_timeout_returns_none(monkeypatch):
    """超时也走兜底：这条调用挂在**同步请求**上，不设上限时一个按钮能转很久。"""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(relevance_agent, "REQUEST_TIMEOUT", 0.01)
    llm.reload()

    async def slow(messages, **kw):
        await asyncio.sleep(1)
        return {"assignments": []}

    monkeypatch.setattr(llm, "chat_json", slow)
    assert asyncio.run(
        relevance_agent.assign_by_relevance(_outline(), _docs())
    ) is None


# ---------------------------------------------------------------
# 白名单：编出来的 id 与标题一律丢弃
# ---------------------------------------------------------------
def test_unknown_doc_id_is_dropped(monkeypatch):
    result, _ = _assign(
        {"assignments": [{"doc_id": "编造的", "section": "1.1 研究背景", "reason": "幻觉"}]},
        monkeypatch,
    )
    assert result is None, "一条有效条目都没有时返回 None，而不是空列表"


def test_section_outside_outline_is_dropped(monkeypatch):
    """章节标题必须逐字命中大纲叶子 —— 差一个字、或是「有子节的那一节」都不算。"""
    result, _ = _assign(
        {"assignments": [
            {"doc_id": "d1", "section": "1.1 研究背景（改过的标题）", "reason": "x"},
            {"doc_id": "d2", "section": "2.1 现状与问题", "reason": "有子节，不是成文单元"},
            {"doc_id": "d3", "section": "1.2 研究问题", "reason": "y"},
        ]},
        monkeypatch,
    )
    assert [a["doc_id"] for a in result] == ["d3"]


def test_duplicate_doc_id_keeps_only_first(monkeypatch):
    """同一篇文献写两遍时只收第一条：后一条等于把前一条的判断静默改掉。"""
    result, _ = _assign(
        {"assignments": [
            {"doc_id": "d1", "section": "1.1 研究背景", "reason": "先选的"},
            {"doc_id": "d1", "section": "2.1 现状与问题", "reason": "又改主意了"},
        ]},
        monkeypatch,
    )
    assert len(result) == 1
    assert result[0]["section"] == "1.1 研究背景"


def test_malformed_items_are_skipped(monkeypatch):
    result, _ = _assign(
        {"assignments": ["d1", {"doc_id": 3, "section": "1.1 研究背景"},
                         {"doc_id": "d1", "section": "1.2 研究问题", "reason": "合规的"}]},
        monkeypatch,
    )
    assert [a["doc_id"] for a in result] == ["d1"]


def test_reason_is_trimmed_and_capped(monkeypatch):
    """理由是界面上的一行说明，过长要截断（否则它会挤掉绑定列表本身）。"""
    result, _ = _assign(
        {"assignments": [{"doc_id": "d1", "section": "1.1 研究背景", "reason": "  " + "很" * 200}]},
        monkeypatch,
    )
    assert result[0]["reason"] == "很" * relevance_agent.MAX_REASON_CHARS


def test_missing_reason_becomes_empty_string(monkeypatch):
    result, _ = _assign(
        {"assignments": [{"doc_id": "d1", "section": "1.1 研究背景"}]}, monkeypatch
    )
    assert result[0]["reason"] == ""


# ---------------------------------------------------------------
# 提示词：白名单必须与绑定的键集逐字相同
# ---------------------------------------------------------------
def test_whitelist_matches_binding_keys():
    """章节白名单取自 schedule_agent._collect_sections，与绑定那份键集逐字相同。

    两边各走一遍大纲，迟早会漂移 —— 届时模型选中的标题在绑定里不存在，会被静默丢弃，
    表现为「模型判了却一篇都没归上」。所以这里钉住：白名单 == uniform_distribute 的键集。
    """
    outline = _outline()
    text, allowed = relevance_agent.build_section_context(outline)
    assert allowed == set(schedule_agent.uniform_distribute(outline, _docs()).keys())
    assert "2.1.1 现状" in allowed, "叶子是子节时，可引用单元就是子节"
    assert "2.1 现状与问题" not in allowed, "有子节的节不是成文单元"
    for title in allowed:
        assert title in text


def test_prompt_lists_documents_with_ids_and_asks_for_full_coverage(monkeypatch):
    """提示词带上文献 id 与摘要，并明确要求「每一篇都要有归属」。

    「每一篇」这条必须在提示词里说出来：归属是模型的活，而补漏是代码的活 —— 模型
    不知道要全覆盖时，会把一批文献判成「无关」而不给出归属，那些文献就只能靠兜底
    按顺序塞进最轻的章节，白白丢掉这次判断的价值。
    """
    _, calls = _assign({"assignments": []}, monkeypatch)
    prompt = calls[0]
    assert "每一篇文献都要给出一个归属" in prompt
    assert "【id=d1】" in prompt and "文献1研究的是1号问题" in prompt
    assert "1.1 研究背景" in prompt and "2.1.1 现状" in prompt
    assert "课程论文/小论文" in prompt, "类型用长名（含适用范围）进提示词"


def test_prompt_truncates_beyond_max_docs(monkeypatch):
    """超过 MAX_DOCS 的文献不进提示词，但**要告诉模型**还有多少篇。"""
    docs = _docs(relevance_agent.MAX_DOCS + 4)
    _, calls = _assign({"assignments": []}, monkeypatch, docs=docs)
    prompt = calls[0]
    assert "【id=d50】" in prompt, "第 MAX_DOCS 篇还在"
    assert "【id=d51】" not in prompt, "超出的那篇不发出去"
    assert f"另有 {4} 篇文献未列出" in prompt


def test_scale_hint_is_smaller_for_short_types(monkeypatch):
    """短篇类型（课程论文）给更小的每节建议篇数，且两条提示都不得暗示可以漏归。"""
    short = relevance_agent._scale_hint("课程论文/小论文")
    long = relevance_agent._scale_hint("文献综述")
    assert "1~2 篇" in short and "1~4 篇" in long
    for hint in (short, long):
        assert "不要为了让每节都有" in hint or "多出来的文献归到最接近的那一节" in hint
