"""测试：润色 Agent（polish_agent）。

重点不是提示词措辞，而是**润色返回的东西能不能用**：这个环节的输出会直接成为正文，
而它对输入的唯一硬约束原本只有「长度不低于一半」。角标掉一个、改一个号，正文照样
通顺、文末列表照样完整 —— 这种失配只有代码能拦，提示词第 5 条管不住（本项目既有
教训：模型偶尔不听话是常态）。
"""
import asyncio

from app.agents import polish_agent


def _stub_llm(monkeypatch, output):
    """替换 llm.chat，返回固定文本。"""
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()

    async def fake(messages, **kw):
        if isinstance(output, Exception):
            raise output
        return output

    monkeypatch.setattr(llm, "chat", fake)


def _polish(text: str) -> str:
    return asyncio.run(polish_agent.polish(text))


# ---------------------------------------------------------------
# 角标计数
# ---------------------------------------------------------------
def test_markers_counts_bracket_and_superscript():
    counts = polish_agent.markers("甲[1]乙[2, 3]丙[1]丁¹戊²²")
    assert counts["[1]"] == 2
    assert counts["[2,3]"] == 1  # 内部空白归一化
    assert counts["¹"] == 1
    assert counts["²²"] == 1


def test_markers_ignores_non_numeric_brackets():
    """[注]、[受访者 A] 是正文内容，不是角标 —— 误判会把它当成「被改动过」。"""
    assert polish_agent.markers("见[注]与[受访者 A]的说明") == {}


# ---------------------------------------------------------------
# 离线 / 空输入：原样返回
# ---------------------------------------------------------------
def test_polish_offline_returns_text_unchanged():
    assert _polish("原文 [1] 内容") == "原文 [1] 内容"


def test_polish_empty_text_unchanged(monkeypatch):
    """空文本别送给模型：它会回一句「请提供需要润色的原文」并被写进正文。"""
    _stub_llm(monkeypatch, "请提供需要润色的原文")
    assert _polish("   ") == "   "


# ---------------------------------------------------------------
# 结果可用性
# ---------------------------------------------------------------
def test_polish_accepts_rewrite_that_keeps_markers(monkeypatch):
    _stub_llm(monkeypatch, "本研究采用问卷调查法收集数据[1]，样本量为 312 份[2, 3]。")
    out = _polish("我们用了问卷法收数据[1]，样本 312 份[2, 3]。")
    assert out.startswith("本研究")


def test_polish_rejects_shorter_than_half(monkeypatch):
    """长度严重偏离 = 答非所问，保留原文。"""
    source = "本研究采用问卷调查法收集数据[1]，样本量为 312 份[2, 3]，覆盖三个省份。"
    _stub_llm(monkeypatch, "好的")
    assert _polish(source) == source


def test_polish_rejects_dropped_marker(monkeypatch):
    """掉一个角标：正文照样通顺，但那条引用没了 —— 必须交回原文。

    这是本文件的主角。润色把 [7] 吃掉了，读者会以为那句话没有出处；更糟的是
    文末列表里第 7 条还在，列表与正文从此对不上。一次润色失败只损失可读性。
    """
    source = "这一结论已被多项研究证实[7]，且在跨文化样本中同样成立。"
    _stub_llm(monkeypatch, "这一结论已被多项研究反复证实，且在跨文化样本中同样成立。")
    assert _polish(source) == source


def test_polish_rejects_renumbered_marker(monkeypatch):
    """改号比掉号更坏：编号错了，读者点到的是一条不存在的文献或另一条文献。"""
    source = "该效应在后续研究中得到重复验证[3]。"
    _stub_llm(monkeypatch, "该效应在后续研究中得到重复验证[2]。")
    assert _polish(source) == source


def test_polish_rejects_invented_marker(monkeypatch):
    """凭空多出角标：等于给一句话安上了一条它没有的出处。"""
    source = "样本量为 312 份，覆盖三个省份。"
    _stub_llm(monkeypatch, "样本量为 312 份[1]，覆盖三个省份。")
    assert _polish(source) == source


def test_polish_rejects_style_change(monkeypatch):
    """方括号被改成上标也算变动：角标样式由引用调度那一刻定死，润色不该顺手换掉。"""
    source = "该结论已获多项研究支持[1]。"
    _stub_llm(monkeypatch, "该结论已获多项研究支持¹。")
    assert _polish(source) == source


def test_polish_allows_marker_reorder(monkeypatch):
    """同一节内调换引用次序**不算**改动：编号含义由引用调度定死，先后不影响归属。"""
    source = "甲观点[1]与乙观点[2]都被质疑过。"
    _stub_llm(monkeypatch, "乙观点[2]与甲观点[1]都存在争议。")
    assert _polish(source) == "乙观点[2]与甲观点[1]都存在争议。"


# ---------------------------------------------------------------
# 输出语言宣告
# ---------------------------------------------------------------
def _capture_prompts(monkeypatch, output: str) -> list[str]:
    """替换 llm.chat，按顺序记下送进去的提示词，并固定返回一段改写。

    要断言的是**提示词里有什么**，不是模型回了什么 —— 所以固定回一段长度合格、
    角标不变的改写，让流程能走到尾。
    """
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()
    seen: list[str] = []

    async def fake(messages, **kw):
        seen.append(messages[-1]["content"])
        return output

    monkeypatch.setattr(llm, "chat", fake)
    return seen


def test_english_output_rule_reaches_the_polish_prompt(monkeypatch):
    """润色也要接输出语言宣告。

    它的输入输出都是**正文本身**：不宣告的话，模型拿到的是一段中文写的润色指令
    （去 AI 痕迹、降重），交回一段中文改写是最自然的反应 —— 而下游只看到「正文变了」，
    一个字都不会报错，整节英文正文就这样被换掉。
    """
    prompts = _capture_prompts(monkeypatch, "This study uses a survey design[1].")
    asyncio.run(polish_agent.polish("This study uses a survey design[1].", "en"))

    assert len(prompts) == 1
    assert "**输出语言**" in prompts[0]
    assert "英文" in prompts[0]


def test_chinese_polish_prompt_has_no_output_rule(monkeypatch):
    """中文路径逐字节不变 —— 加语言这件事一个字都不许改动既有中文提示词。

    不传语言（存量调用点）与显式传 "zh" 必须是同一份提示词。
    """
    prompts = _capture_prompts(monkeypatch, "本研究采用问卷法[1]。")
    asyncio.run(polish_agent.polish("本研究采用问卷法[1]。"))
    asyncio.run(polish_agent.polish("本研究采用问卷法[1]。", "zh"))

    assert len(prompts) == 2
    assert prompts[0] == prompts[1]
    assert "输出语言" not in prompts[0]
