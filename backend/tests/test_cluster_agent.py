"""测试：文献主题聚类 Agent（cluster_agent）。

这个 agent 的产物**直接决定文献综述的章节结构**，所以它最要紧的性质不是「解析
健壮」，而是「不把不存在的东西当成真的」：编出来的 doc id、模型声称归好了其实没有
归的文献，到了界面上都与真实结果长得一模一样。
"""
import asyncio

from app.agents import cluster_agent
from app.llm import llm


def _docs(n=3):
    return [
        {"id": f"d{i}", "title": f"文献{i}", "authors": "张三", "year": "2023",
         "source": "某期刊", "summary": f"文献{i}的摘要", "pages": []}
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


def _cluster(docs, **kw):
    kw.setdefault("topic", "社交媒体与知识共享")
    kw.setdefault("paper_type", "文献综述")
    return asyncio.run(cluster_agent.cluster_documents(docs, **kw))


# ---------------------------------------------------------------
# 拿不到结果就返回空，而不是抛错或给半份
# ---------------------------------------------------------------
def test_offline_returns_empty_clusters():
    """未配置 LLM 时返回空聚类。

    空 clusters 是调用方唯一的失败信号（_run_cluster 据此把任务标成 error）；
    抛异常或返回半份结果都会让界面显示「已聚类」而实际什么都没变。
    """
    assert _cluster(_docs()) == {"clusters": [], "notes": "", "unassigned": []}


def test_no_documents_returns_empty():
    assert _cluster([])["clusters"] == []


def test_non_dict_payload_returns_empty(monkeypatch):
    """模型返回数组/字符串时不能当场炸，退化成空聚类走正常失败路径。"""
    _stub_llm(monkeypatch, ["不是字典"])
    assert _cluster(_docs())["clusters"] == []


# ---------------------------------------------------------------
# doc_ids 白名单：编出来的 id 一律丢弃
# ---------------------------------------------------------------
def test_invented_doc_ids_are_dropped(monkeypatch):
    """模型编的 id 丢弃，只留真实文献。

    这不是格式问题：界面上会显示一篇查无此文的文献，大纲也会照着一个不存在的
    主题去组织章节，而作者在确认点①上完全看不出那是假的。
    """
    _stub_llm(monkeypatch, {"clusters": [
        {"title": "主题一", "doc_ids": ["d1", "ghost", "d9"], "key_points": []},
        {"title": "主题二", "doc_ids": ["d2"]},
    ]})
    out = _cluster(_docs(3))
    assert out["clusters"][0]["doc_ids"] == ["d1"]
    assert out["clusters"][1]["doc_ids"] == ["d2"]


def test_cluster_with_only_invented_ids_is_dropped(monkeypatch):
    """整簇都是编的 id ⇒ 这簇没有文献支撑，是整个丢掉而不是留个空壳主题。"""
    _stub_llm(monkeypatch, {"clusters": [
        {"title": "编的主题", "doc_ids": ["ghost"]},
        {"title": "真主题", "doc_ids": ["d1"]},
    ]})
    assert [c["title"] for c in _cluster(_docs(2))["clusters"]] == ["真主题"]


def test_duplicate_ids_in_one_cluster_are_deduped(monkeypatch):
    _stub_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": ["d1", "d1", "d2"]}]})
    assert _cluster(_docs(2))["clusters"][0]["doc_ids"] == ["d1", "d2"]


def test_doc_titles_are_filled_by_the_server(monkeypatch):
    """doc_titles 由服务端按 id 回填。

    模型给的标题不可信：它可能顺手改写、漏掉或张冠李戴 —— 而这些标题会显示在
    确认面板上，作者是照着它们决定改不改的。
    """
    _stub_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": ["d2"]}]})
    c = _cluster(_docs(2))["clusters"][0]
    assert c["doc_titles"] == ["文献2"]
    assert c["id"] == "c1", "簇编号由服务端重排，模型给的不作数"


# ---------------------------------------------------------------
# 簇标题的长度与空值
# ---------------------------------------------------------------
def test_titles_are_trimmed_and_length_checked(monkeypatch):
    """空标题与超长标题丢弃，有效标题去掉首尾空白。

    这些标题会直接成为备选节标题，与 _clean_title 受同一把尺子约束（60 字）。
    """
    _stub_llm(monkeypatch, {"clusters": [
        {"title": "   ", "doc_ids": ["d1"]},
        {"title": "长" * (cluster_agent.MAX_TITLE_CHARS + 1), "doc_ids": ["d1"]},
        {"title": "  有效标题  ", "doc_ids": ["d1"]},
    ]})
    assert [c["title"] for c in _cluster(_docs(1))["clusters"]] == ["有效标题"]


def test_cluster_count_is_capped(monkeypatch):
    """簇数超过上限时截断：再多就无法逐簇对应到骨架的主题章了。"""
    _stub_llm(monkeypatch, {"clusters": [
        {"title": f"主题{i}", "doc_ids": [f"d{i}"]} for i in range(1, 9)
    ]})
    assert len(_cluster(_docs(8))["clusters"]) == cluster_agent.MAX_CLUSTERS


# ---------------------------------------------------------------
# unassigned 由服务端算，不采信模型
# ---------------------------------------------------------------
def test_unassigned_is_computed_by_the_server(monkeypatch):
    """模型声称「全都归好了」而其实漏了两篇时，必须由服务端把这两篇列出来。

    采信模型的话，悄悄漏掉一半文献的结果与「全都归好了」在界面上毫无区别。
    """
    _stub_llm(monkeypatch, {
        "clusters": [{"title": "主题", "doc_ids": ["d1"]}],
        "unassigned": [],
    })
    out = _cluster(_docs(3))
    assert [u["doc_id"] for u in out["unassigned"]] == ["d2", "d3"]
    assert out["unassigned"][0]["reason"] == "模型未归入任何主题"


def test_unassigned_ignores_false_claims_about_assigned_docs(monkeypatch):
    """模型说某篇没归入、但它其实在某个簇里时，不得出现在未归入清单里。"""
    _stub_llm(monkeypatch, {
        "clusters": [{"title": "主题", "doc_ids": ["d1"]}],
        "unassigned": [{"doc_id": "d1", "reason": "误报"}],
    })
    assert _cluster(_docs(1))["unassigned"] == []


def test_model_reason_is_kept_for_genuinely_unassigned_docs(monkeypatch):
    _stub_llm(monkeypatch, {
        "clusters": [{"title": "主题", "doc_ids": ["d1"]}],
        "unassigned": [{"doc_id": "d2", "reason": "与核心问题无关"}],
    })
    assert _cluster(_docs(2))["unassigned"][0]["reason"] == "与核心问题无关"


def test_documents_beyond_the_prompt_are_not_blamed_on_the_model(monkeypatch):
    """没进提示词的文献，理由必须如实说是「超出分析范围」。

    说「模型未归入」是假话 —— 它压根没见过这些文献，用户却会据此以为文献质量不行。
    """
    _stub_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": ["d1"]}]})
    out = _cluster(_docs(cluster_agent.MAX_DOCS + 2))
    assert out["unassigned"][-1]["doc_id"] == f"d{cluster_agent.MAX_DOCS + 2}"
    assert "超出本次分析范围" in out["unassigned"][-1]["reason"]


def test_notes_is_passed_through(monkeypatch):
    _stub_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": ["d1"]}],
                            "notes": "整体呈两极格局"})
    assert _cluster(_docs(1))["notes"] == "整体呈两极格局"


# ---------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------
def test_prompt_carries_ids_type_topic_and_target_count(monkeypatch):
    """提示词必须逐篇给出 id（模型要回填它）、带上类型与选题、并说明要几簇。"""
    calls = _stub_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": ["d1"]}]})
    _cluster(_docs(2), target_clusters=3)

    prompt = calls[0]
    for d in _docs(2):
        assert f"【id={d['id']}】" in prompt, "不给出 id，模型只能自己编号"
    assert "文献综述" in prompt
    assert "社交媒体与知识共享" in prompt
    assert "恰好 3 个研究主题维度" in prompt


def test_target_count_is_optional(monkeypatch):
    """不指定簇数时给区间，而不是写死一个数（骨架没有主题章的类型走到这里）。"""
    calls = _stub_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": ["d1"]}]})
    _cluster(_docs(1), target_clusters=0)
    assert f"{cluster_agent.MIN_CLUSTERS}–{cluster_agent.MAX_CLUSTERS}" in calls[0]


# ---------------------------------------------------------------
# 写作语言
# ---------------------------------------------------------------
def test_english_cluster_prompt_declares_the_language(monkeypatch):
    """英文项目的聚类提示词要带输出语言宣告。

    主题名与概述会成为文献综述的**节标题与节内容** —— 是产物文字，不是界面文案。
    提示词不宣告的话，英文项目的文献综述会长出几个中文小节。
    """
    calls = _stub_llm(monkeypatch, {"clusters": [{"title": "Theme", "doc_ids": ["d1"]}]})
    _cluster(_docs(1), writing_lang="en")
    assert "**输出语言**" in calls[0] and "英文" in calls[0]


def test_chinese_cluster_prompt_has_no_output_rule(monkeypatch):
    """中文路径逐字节不变（不传语言与显式传 zh 必须同一份提示词）。"""
    calls = _stub_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": ["d1"]}]})
    _cluster(_docs(1))
    zh_prompt = calls[0]
    _cluster(_docs(1), writing_lang="zh")
    assert calls[1] == zh_prompt
    assert "输出语言" not in zh_prompt


def test_cluster_title_cap_widens_for_english(monkeypatch):
    """簇标题的长度上限跟着语言放宽（中文 60 字，英文 180）。

    与大纲标题同一把尺子：60 个字符对中文是合理的，对英文会把一个正常的主题名
    **截断在词中间**（约十个词的英文标题很常见）。判据只在 `writing_lang.cap_chars`
    一处，别在这里再写一个数字。
    """
    long_en = "The Effect of Enterprise Social Media on Tacit Knowledge Sharing " \
              "in Multinational Corporations"          # 88 字符 > 60，< 180
    too_long = "x" * (cluster_agent.MAX_TITLE_CHARS * 3 + 1)

    _stub_llm(monkeypatch, {"clusters": [
        {"title": long_en, "doc_ids": ["d1"]},
        {"title": too_long, "doc_ids": ["d1"]},
    ]})
    assert [c["title"] for c in _cluster(_docs(1), writing_lang="en")["clusters"]] == [long_en]

    # 中文口径一个字不改：同一个 88 字符的标题在中文下照旧被丢弃
    _stub_llm(monkeypatch, {"clusters": [{"title": long_en, "doc_ids": ["d1"]}]})
    assert _cluster(_docs(1))["clusters"] == []
