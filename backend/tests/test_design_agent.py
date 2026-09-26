"""测试：研究设计提炼 Agent（design_agent）的语言边界。

范围只限**提示词里的语言**。提炼的校验逻辑（`_parse` 的字段白名单）与「提炼不出
东西算失败」那条路由行为已经在 `tests/test_api.py` 的 `extract_design` 用例里覆盖
（`_run_extract_design` 是它在生产代码里的唯一调用点），别在这里再写一份 —— 那两处
写重了，改了行为要同时改两遍，早晚有一遍忘了。

本文件要钉的是这个 Agent 独有的那条边界：**字段名不跟随语言，字段取值跟随。**
字段名是前端表单的结构，也是 `_parse` 的白名单键；把它「本地化」一下，提炼会整条
静默失效 —— 键对不上 ⇒ 全空取值 ⇒ 前端如常显示「没提炼出东西，请手填」，没人知道
是提示词被改了。
"""
import asyncio

from app.agents import design_agent
from app.llm import llm

FIELDS = ["数据源与样本", "变量与测量", "分析方法", "主要结果"]


def _materials():
    return [{"id": "m1", "label": "调研数据.xlsx", "kind": "file", "text": "样本量 312"}]


def _capture_prompt(monkeypatch, payload) -> list[str]:
    """打开 LLM，记下提示词，并固定返回给定载荷。"""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()
    seen: list[str] = []

    async def fake(messages, **kw):
        seen.append(messages[0]["content"])
        return payload

    monkeypatch.setattr(llm, "chat_json", fake)
    return seen


def _extract(**kw):
    kw.setdefault("materials", _materials())
    kw.setdefault("fields", FIELDS)
    kw.setdefault("paper_type", "定量/实证研究")
    kw.setdefault("design_label", "实证设计")
    kw.setdefault("topic", "社交媒体与知识共享")
    return asyncio.run(design_agent.extract_design(**kw))


def test_english_design_prompt_declares_the_language(monkeypatch):
    """英文项目的提炼提示词要带输出语言宣告。

    提炼出的取值会作为「作者已给出的研究设计」**逐字**注入每一节的正文提示词：英文
    项目下若是中文取值，模型会顺着这份已知条件用中文写第四章 —— 而那是整篇论文里
    最要紧的一章。
    """
    prompts = _capture_prompt(monkeypatch, {"fields": {}})
    _extract(writing_lang="en")

    assert "**输出语言**" in prompts[0]
    assert "英文" in prompts[0]
    assert "取值" in prompts[0], "宣告要管到 JSON 字段的取值，不只是各级标题"


def test_english_design_prompt_keeps_the_chinese_field_names(monkeypatch):
    """字段名不跟随语言 —— 翻译一下，`_parse` 的白名单就一个键都匹配不上。

    这是本文件的主角：同一份提示词里，字段名保持中文、取值要求英文，两件事同时成立。
    """
    prompts = _capture_prompt(monkeypatch, {"fields": {}})
    _extract(writing_lang="en")

    for field in FIELDS:
        assert f"  - {field}" in prompts[0], field


def test_chinese_design_prompt_has_no_output_rule(monkeypatch):
    """中文路径逐字节不变（不传语言与显式传 zh 必须同一份提示词）。"""
    first = _capture_prompt(monkeypatch, {"fields": {}})
    _extract()
    second = _capture_prompt(monkeypatch, {"fields": {}})
    _extract(writing_lang="zh")

    assert second[0] == first[0]
    assert "输出语言" not in first[0]
