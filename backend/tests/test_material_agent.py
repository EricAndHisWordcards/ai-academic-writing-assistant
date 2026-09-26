"""测试：材料分析 Agent（material_agent）。

重点是**校验**而非提示词措辞：模型的输出是自由 JSON，章节标题写错一个字的后果是
生成时永远取不到材料，而界面上却显示「已分析」—— 这种静默失配必须在解析层拦掉。
"""
import asyncio

import pytest

from app.agents import material_agent

SECTIONS = ["1.1 研究背景", "4.1 描述性统计", "4.3 结果讨论"]


def _materials():
    return [
        {"id": "m1", "label": "问卷数据.xlsx", "kind": "file", "text": "样本量 312"},
        {"id": "m2", "label": "访谈记录.docx", "kind": "file", "text": "受访者 A 表示…"},
    ]


def _stub_llm(monkeypatch, payload):
    """替换 llm.chat_json，返回固定结果并记下收到的提示词。"""
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()

    calls: list[str] = []

    async def fake(messages, **kw):
        calls.append(messages[0]["content"])
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(llm, "chat_json", fake)
    return calls


def _analyze(**kw):
    kw.setdefault("materials", _materials())
    kw.setdefault("sections", SECTIONS)
    kw.setdefault("paper_type", "定量/实证研究")
    kw.setdefault("topic", "社交媒体与知识共享")
    kw.setdefault("core_question", "社交媒体使用如何影响知识共享？")
    return asyncio.run(material_agent.analyze_materials(**kw))


# ---------------------------------------------------------------
# 离线与空输入
# ---------------------------------------------------------------
def test_offline_returns_empty_plan():
    """未配置 LLM 时返回空计划（生成环节对空计划是安全的），不抛错。"""
    plan = _analyze()
    assert plan == {"notes": "", "placements": [], "unused": []}


@pytest.mark.parametrize("mats,secs", [([], SECTIONS), (_materials(), [])])
def test_no_materials_or_sections_returns_empty(mats, secs):
    plan = _analyze(materials=mats, sections=secs)
    assert plan["placements"] == [] and plan["unused"] == []


# ---------------------------------------------------------------
# 正常解析
# ---------------------------------------------------------------
def test_placements_are_parsed(monkeypatch):
    _stub_llm(monkeypatch, {
        "notes": "数据进实证章节，访谈进讨论",
        "placements": [
            {"section_title": "4.1 描述性统计", "material_ids": ["m1"],
             "usage": "用 312 份样本交代样本构成",
             "key_points": ["有效样本 312 份", "回收率 92%"]},
        ],
        "unused": [{"material_id": "m2", "reason": "与核心问题无关"}],
    })
    plan = _analyze()
    assert plan["notes"] == "数据进实证章节，访谈进讨论"
    assert len(plan["placements"]) == 1
    p = plan["placements"][0]
    assert p["section_title"] == "4.1 描述性统计"
    assert p["material_ids"] == ["m1"]
    assert p["key_points"] == ["有效样本 312 份", "回收率 92%"]
    assert plan["unused"] == [
        {"material_id": "m2", "label": "访谈记录.docx", "reason": "与核心问题无关"}
    ]


def test_prompt_carries_sections_core_question_and_material_ids(monkeypatch):
    calls = _stub_llm(monkeypatch, {"placements": []})
    _analyze()
    prompt = calls[0]
    assert "4.1 描述性统计" in prompt
    assert "社交媒体使用如何影响知识共享？" in prompt
    assert "m1" in prompt and "问卷数据.xlsx" in prompt
    # 「只能从中选择」是防自造章节的关键约束，值得钉住
    assert "只能从中选择" in prompt


# ---------------------------------------------------------------
# 校验：写错的标题 / 不存在的材料一律丢弃
# ---------------------------------------------------------------
def test_unknown_section_title_is_dropped(monkeypatch):
    _stub_llm(monkeypatch, {
        "placements": [
            {"section_title": "4.2 回归分析", "material_ids": ["m1"], "usage": "x"},
            {"section_title": "4.1 描述性统计", "material_ids": ["m2"], "usage": "y"},
        ],
    })
    plan = _analyze()
    assert [p["section_title"] for p in plan["placements"]] == ["4.1 描述性统计"]


def test_unknown_material_id_is_dropped(monkeypatch):
    _stub_llm(monkeypatch, {
        "placements": [
            {"section_title": "4.1 描述性统计", "material_ids": ["不存在"], "usage": "x"},
        ],
    })
    plan = _analyze()
    assert plan["placements"] == []
    # 两份材料都没被用上，应全部出现在 unused 里
    assert {u["material_id"] for u in plan["unused"]} == {"m1", "m2"}


def test_duplicate_material_ids_are_deduped(monkeypatch):
    _stub_llm(monkeypatch, {
        "placements": [
            {"section_title": "4.1 描述性统计", "material_ids": ["m1", "m1"], "usage": "x"},
        ],
    })
    assert _analyze()["placements"][0]["material_ids"] == ["m1"]


def test_material_used_in_two_sections_is_not_unused(monkeypatch):
    _stub_llm(monkeypatch, {
        "placements": [
            {"section_title": "4.1 描述性统计", "material_ids": ["m1"], "usage": "x"},
            {"section_title": "4.3 结果讨论", "material_ids": ["m1", "m2"], "usage": "y"},
        ],
    })
    plan = _analyze()
    assert plan["unused"] == []


def test_material_silently_omitted_is_reported(monkeypatch):
    """模型完全没提到的材料也要出现在 unused，否则用户不知道它被忽略了。"""
    _stub_llm(monkeypatch, {
        "placements": [
            {"section_title": "4.1 描述性统计", "material_ids": ["m1"], "usage": "x"},
        ],
    })
    plan = _analyze()
    assert plan["unused"] == [
        {"material_id": "m2", "label": "访谈记录.docx", "reason": "模型未给出使用建议"}
    ]


def test_malformed_entries_are_skipped(monkeypatch):
    _stub_llm(monkeypatch, {
        "notes": 123,   # 非字符串
        "placements": [
            "不是字典",
            {"section_title": "4.1 描述性统计", "material_ids": "m1"},   # ids 不是列表
            {"section_title": "4.1 描述性统计", "material_ids": ["m1"],
             "key_points": ["有效样本 312", 42, "  ", ""]},
        ],
        "unused": ["不是字典", {"material_id": "m2", "reason": None}],
    })
    plan = _analyze()
    assert plan["notes"] == ""
    assert len(plan["placements"]) == 1
    assert plan["placements"][0]["key_points"] == ["有效样本 312"]
    assert plan["unused"][0]["reason"] == "模型未给出使用建议"


def test_non_dict_output_returns_empty(monkeypatch):
    _stub_llm(monkeypatch, ["不是字典"])
    assert _analyze()["placements"] == []


def test_llm_exception_propagates(monkeypatch):
    """LLM 报错不在这里吞掉 —— 由 _run_analyze 统一转成任务错误状态。"""
    _stub_llm(monkeypatch, RuntimeError("服务不可用"))
    with pytest.raises(RuntimeError):
        _analyze()


# ---------------------------------------------------------------
# 提示词渲染
# ---------------------------------------------------------------
def test_format_materials_marks_kind_and_id():
    text = material_agent._format_materials([
        {"id": "m1", "label": "问卷.xlsx", "kind": "file", "text": "内容 A"},
        {"id": "m2", "label": "我的思路", "kind": "text", "text": "内容 B"},
    ])
    assert "id=m1" in text and "问卷.xlsx" in text and "上传的文件" in text
    assert "直接粘贴的文本" in text


def test_format_materials_truncates_long_text():
    text = material_agent._format_materials([
        {"id": "m1", "label": "长材料", "kind": "text",
         "text": "字" * (material_agent.ANALYZE_CHARS + 1000)},
    ])
    assert "仅摘录前" in text


# ---------------------------------------------------------------
# 写作语言
# ---------------------------------------------------------------
def test_english_material_prompt_declares_the_language(monkeypatch):
    """英文项目的材料分析提示词要带输出语言宣告。

    这个 Agent 产出的 `usage` / `key_points` 是**给撰稿人的可执行指令**，其中
    `key_points`（「必须出现在正文里的具体事实与数字」）会被逐条注入每一节的正文
    提示词 —— 中文的要点会诱着模型在英文正文里写中文，而下游只看到「正文写出来了」，
    一个字都不会报错。

    材料本身的原文照旧原样进提示词（不翻译），所以这里只断言宣告在不在，不去断言
    提示词里没有中文。
    """
    calls = _stub_llm(monkeypatch, {"placements": []})
    _analyze(writing_lang="en")
    assert "**输出语言**" in calls[0]
    assert "英文" in calls[0]
    assert "取值" in calls[0], "宣告要管到 JSON 字段的取值，不只是各级标题"


def test_chinese_material_prompt_has_no_output_rule(monkeypatch):
    """中文路径逐字节不变（不传语言与显式传 zh 必须同一份提示词）。"""
    calls = _stub_llm(monkeypatch, {"placements": []})
    _analyze()
    zh_prompt = calls[0]
    _analyze(writing_lang="zh")
    assert calls[1] == zh_prompt
    assert "输出语言" not in zh_prompt
