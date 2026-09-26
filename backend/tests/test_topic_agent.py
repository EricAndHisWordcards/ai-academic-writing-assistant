"""测试：选题 Agent（topic_agent）。

`recommend_topics` 的产物直接决定界面上那排候选卡片，而卡片读的是
`item.title / item.question / item.feasibility`。此前这条路上**没有任何逐条校验**：
模型回一串字符串（`["选题一", "选题二"]`），界面照单全收，渲染出来是一排**空白
卡片**，点了也没反应（选中后写进项目的是 `item.title`，在那里是 undefined）。

另外几个 agent 都逐条校验（relevance / cluster / material），这里对齐同一把尺子。
"""
import asyncio

from app.agents import topic_agent
from app.llm import llm


def _stub_llm(monkeypatch, payload):
    """打开 LLM 并让 chat_json 直接返回给定载荷。"""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()

    async def fake(messages, **kw):
        return payload

    monkeypatch.setattr(llm, "chat_json", fake)


def _recommend(monkeypatch, payload, count=3):
    _stub_llm(monkeypatch, payload)
    return asyncio.run(
        topic_agent.recommend_topics("机器学习", "课程论文/小论文", 5000, count=count)
    )


# ---------------------------------------------------------------
# 读得懂的照收，且归一成界面真正会读的那几个键
# ---------------------------------------------------------------
def test_valid_topics_are_kept(monkeypatch):
    topics = _recommend(monkeypatch, [
        {"title": "选题一", "question": "问题一", "feasibility": "可行一", "match": 85},
        {"title": "选题二", "question": "问题二", "feasibility": "可行二"},
    ])
    assert topics[0] == {
        "title": "选题一", "question": "问题一", "feasibility": "可行一", "match": 85,
    }
    # match 不在时就不给这个键 —— 界面按 `t.match != null` 决定显不显示
    assert "match" not in topics[1]


def test_title_is_trimmed(monkeypatch):
    topics = _recommend(monkeypatch, [{"title": "  有空格  "}])
    assert topics[0]["title"] == "有空格"


def test_dedicated_topics_key_still_works(monkeypatch):
    """模型把数组包在 topics 里也是既有行为，不能因为加了校验就丢掉这条。"""
    topics = _recommend(monkeypatch, {"topics": [{"title": "选题一"}]})
    assert [t["title"] for t in topics] == ["选题一"]


# ---------------------------------------------------------------
# 读不懂的丢弃，一个都不剩时退回兜底
# ---------------------------------------------------------------
def test_string_items_are_dropped(monkeypatch):
    """[`"选题一"`] 这种载荷正是空白卡片的来源 —— 一条都不该留下。

    全部不可用 ⇒ 与调用失败同一种处理（退回兜底模板）。对调用方来说它们是同一件事：
    这一刻的推荐不可用。
    """
    topics = _recommend(monkeypatch, ["选题一", "选题二"])
    assert [t["title"] for t in topics] == [
        "机器学习研究现状与趋势分析",
        "机器学习的影响因素与作用机制研究",
        "机器学习实践中的关键问题与对策",
    ]


def test_items_without_a_usable_title_are_dropped(monkeypatch):
    """title 是唯一的硬要求：没有标题等于没有这张卡片。"""
    topics = _recommend(monkeypatch, [
        {"title": ""},
        {"title": "   "},
        {"question": "只有问题没有标题"},
        "字符串",
        42,
        None,
        {"title": "合格的"},
    ])
    assert [t["title"] for t in topics] == ["合格的"]


def test_non_string_fields_become_empty_strings(monkeypatch):
    """数字 / 对象 / 数组都不许流进 React 子节点。"""
    topics = _recommend(monkeypatch, [
        {"title": "选题一", "question": 123, "feasibility": {"a": 1}},
    ])
    assert topics[0]["question"] == ""
    assert topics[0]["feasibility"] == ""


def test_non_numeric_match_is_dropped(monkeypatch):
    """match 只在真的是数字时保留。

    界面是 `t.match != null && 匹配度 {t.match}`，模型回个「高」或一个对象就会原样
    印在卡片上 —— 与兜底选题刻意不给 match 是同一条规矩（那个分数得是比出来的）。
    """
    topics = _recommend(monkeypatch, [
        {"title": "甲", "match": "高"},
        {"title": "乙", "match": None},
        {"title": "丙", "match": True},
        {"title": "丁", "match": 0},
    ], count=4)
    assert ["match" in t for t in topics] == [False, False, False, True]
    # 0 是合格的匹配度，不能被当成「没有」——所以判据是 isinstance 而不是真值
    assert topics[3]["match"] == 0


def test_count_still_limits_the_result(monkeypatch):
    topics = _recommend(
        monkeypatch,
        [{"title": f"选题{i}"} for i in range(5)],
        count=2,
    )
    assert [t["title"] for t in topics] == ["选题0", "选题1"]


def test_non_list_payload_falls_back(monkeypatch):
    topics = _recommend(monkeypatch, {"notes": "我只想说句话"})
    assert len(topics) == 3
    assert all(t.get("title") for t in topics)


def test_offline_still_falls_back():
    """未配置 LLM 时不发请求，直接给兜底（既有行为，校验不改变它）。"""
    topics = asyncio.run(
        topic_agent.recommend_topics("机器学习", "课程论文/小论文", 5000)
    )
    assert len(topics) == 3


# ---------------------------------------------------------------
# 写作语言
# ---------------------------------------------------------------
def _capture_prompt(monkeypatch, payload) -> list[str]:
    """打开 LLM，记下提示词，并固定返回给定载荷。"""
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()
    seen: list[str] = []

    async def fake(messages, **kw):
        seen.append(messages[-1]["content"])
        return payload

    monkeypatch.setattr(llm, "chat_json", fake)
    return seen


def test_english_topic_prompt_declares_the_language_and_the_unit(monkeypatch):
    """英文项目的选题提示词要带输出语言宣告，字数单位也要跟着语言。

    候选标题会被用户挑中、写进 `projects.topic`，最终成为**论文的标题** —— 中文候选
    会让整篇英文论文从标题起就是中文。单位那处同理：给英文项目说「5000 字」，
    模型按中文体量估可行性，推荐出来的题与目标篇幅对不上。
    """
    prompts = _capture_prompt(monkeypatch, [{"title": "A Topic"}])
    asyncio.run(topic_agent.recommend_topics(
        "digital transformation", "课程论文/小论文", 5000, writing_lang="en",
    ))

    assert "**输出语言**" in prompts[0]
    assert "5000 words" in prompts[0]
    assert "5000 字" not in prompts[0]


def test_chinese_topic_prompt_has_no_output_rule(monkeypatch):
    """中文路径逐字节不变（不传语言与显式传 zh 必须同一份提示词）。"""
    prompts = _capture_prompt(monkeypatch, [{"title": "选题一"}])
    asyncio.run(topic_agent.recommend_topics("机器学习", "课程论文/小论文", 5000))
    asyncio.run(topic_agent.recommend_topics(
        "机器学习", "课程论文/小论文", 5000, writing_lang="zh",
    ))

    assert prompts[0] == prompts[1]
    assert "输出语言" not in prompts[0]
    assert "5000 字" in prompts[0]


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def test_english_fallback_topics_are_english():
    """未配置 LLM 时的兜底选题也要是英文的。

    这是英文项目在**没配 LLM** 时唯一能看见的东西。中文模板会让一篇英文论文顶着
    中文标题开头，而用户很可能就这么挑了一条往下走。

    唯一允许留着汉字的地方是 `feasibility` 里的**类型名**（决策 3：界面文案与类型名
    一律中文，英文项目里也不翻译）。这条边界要钉住，否则下一个人会顺手把 `title`
    也「本地化」掉 —— 而它不是界面文案，它会被写进 `projects.topic`、成为论文标题。
    """
    topics = asyncio.run(topic_agent.recommend_topics(
        "digital transformation", "课程论文/小论文", 5000, writing_lang="en",
    ))
    assert len(topics) == 3
    for t in topics:
        assert t["title"] and t["question"] and t["feasibility"]
        assert not _has_cjk(t["title"]), t
        assert not _has_cjk(t["question"]), t
        assert "课程论文/小论文" in t["feasibility"], "类型名照旧用中文"


def test_english_fallback_after_an_unusable_model_answer_is_english(monkeypatch):
    """模型回了不可用的东西时那条兜底路径也要带语言。

    这是**最容易漏的一处**：`is_configured` 为假的那条分支一眼看得见，而「模型回
    一串字符串」这条藏在函数中段，漏掉它的话英文项目会在这里拿到三条中文候选。
    """
    _stub_llm(monkeypatch, ["选题一", "选题二"])          # 全是字符串，一条都不可用
    topics = asyncio.run(topic_agent.recommend_topics(
        "digital transformation", "课程论文/小论文", 5000, writing_lang="en",
    ))
    assert len(topics) == 3
    assert not _has_cjk(topics[0]["title"]), topics[0]
