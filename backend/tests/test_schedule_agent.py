"""测试：均匀打散引用算法（schedule_agent）。"""
import pytest

from app.agents import schedule_agent


def _make_outline(chapter_titles):
    """构造一个简单大纲，每个章节一个 section。"""
    chapters = []
    for t in chapter_titles:
        chapters.append({
            "title": t,
            "sections": [{"title": t, "word_budget": 1000, "subsections": []}],
        })
    return {"chapters": chapters}


def _make_docs(n):
    """构造 n 篇文献，每篇 2 页。"""
    docs = []
    for i in range(n):
        docs.append({
            "id": f"d{i+1}",
            "title": f"文献{i+1}",
            "pages": [{"page": i * 2 + 1}, {"page": i * 2 + 2}],
        })
    return docs


def test_docs_more_than_sections_each_section_covered():
    """文档数 > 章节数：每章至少绑定 1 篇，无遗漏。"""
    outline = _make_outline(["章1", "章2", "章3"])
    docs = _make_docs(5)
    binding = schedule_agent.uniform_distribute(outline, docs)

    # 每个章节都有绑定
    assert set(binding.keys()) == {"章1", "章2", "章3"}
    for sec, items in binding.items():
        assert len(items) >= 1

    # 无遗漏：所有文档都被绑定到某处
    all_bound = {it["doc_id"] for items in binding.values() for it in items}
    assert all_bound == {d["id"] for d in docs}


def test_no_duplicate_in_same_section():
    """同一文档不在同一章节重复绑定。"""
    outline = _make_outline(["章1", "章2"])
    docs = _make_docs(4)
    binding = schedule_agent.uniform_distribute(outline, docs)

    for sec, items in binding.items():
        ids = [it["doc_id"] for it in items]
        assert len(ids) == len(set(ids)), f"{sec} 存在重复绑定"


def test_docs_less_than_sections_all_covered():
    """文档数 < 章节数：所有章节都有引用。"""
    outline = _make_outline(["章1", "章2", "章3", "章4", "章5"])
    docs = _make_docs(2)
    binding = schedule_agent.uniform_distribute(outline, docs)

    # 所有章节都有绑定
    for sec in ["章1", "章2", "章3", "章4", "章5"]:
        assert sec in binding
        assert len(binding[sec]) >= 1


def test_every_binding_has_page():
    """每个绑定项都含可追溯的页码。"""
    outline = _make_outline(["章1", "章2"])
    docs = _make_docs(3)
    binding = schedule_agent.uniform_distribute(outline, docs)

    for items in binding.values():
        for it in items:
            assert it["page"] in (1, 2, 3, 4, 5, 6)
            assert it["doc_id"]
            assert it["doc_title"]


def test_empty_inputs_return_empty():
    """空大纲或空文献返回空绑定。"""
    assert schedule_agent.uniform_distribute({"chapters": []}, _make_docs(3)) == {}
    assert schedule_agent.uniform_distribute(_make_outline(["章1"]), []) == {}


# ---------------------------------------------------------------
# fill_gaps：补相关性调度的缺口（四条承诺由它守住，而不是由模型守住）
# ---------------------------------------------------------------
def _assigned(binding):
    return {it["doc_id"] for items in binding.values() for it in items}


def test_fill_gaps_places_every_document():
    """模型只归了一篇，库里另一篇必须被补进去 —— 无遗漏是代码保证的。

    这条是本模块最要紧的性质：模型判定「这篇与本大纲无关」之后，界面上没有任何地方
    能把文献加回来（本步没有绑定编辑器），那篇文献用户就再也用不上了。所以无遗漏
    不能委托给模型。
    """
    outline = _make_outline(["章1", "章2"])
    docs = _make_docs(2)
    binding = schedule_agent.fill_gaps({"章1": [{"doc_id": "d1", "reason": "贴题"}]}, outline, docs)

    assert _assigned(binding) == {"d1", "d2"}


def test_fill_gaps_covers_every_section():
    """模型把文献全塞进一节，空着的那节必须被补上一篇（全章节覆盖）。

    空章节在正文里就是「这一节没有任何引用」，而它在界面上与「这一节本来就不需要
    引用」长得一模一样 —— 用户无从分辨。
    """
    outline = _make_outline(["章1", "章2", "章3"])
    docs = _make_docs(2)
    binding = schedule_agent.fill_gaps(
        {"章1": [{"doc_id": "d1"}, {"doc_id": "d2"}]}, outline, docs
    )

    for sec in ("章1", "章2", "章3"):
        assert binding[sec], f"{sec} 没有引用"


def test_fill_gaps_never_duplicates_within_a_section():
    """模型在同一节里重复给同一篇只留一次，且跨节复用不违规。"""
    outline = _make_outline(["章1", "章2", "章3"])
    docs = _make_docs(2)
    binding = schedule_agent.fill_gaps(
        {"章1": [{"doc_id": "d1"}, {"doc_id": "d1", "reason": "改主意了"}],
         "章2": [{"doc_id": "d2"}]},
        outline, docs,
    )

    for sec, items in binding.items():
        ids = [it["doc_id"] for it in items]
        assert len(ids) == len(set(ids)), f"{sec} 存在同节重复"
    # 第 3 节是补出来的：只能复用已有文献，而这是合法形态（同节不重复即可）
    assert binding["章3"]
    assert _assigned(binding) == {"d1", "d2"}


def test_fill_gaps_keys_follow_outline_order():
    """返回的键序按大纲，不按模型给的顺序 —— 界面逐节展示绑定。

    模型的输出顺序天然是它自己的思路顺序，照抄会让界面上的绑定顺序与大纲对不上。
    """
    outline = _make_outline(["章1", "章2", "章3"])
    binding = schedule_agent.fill_gaps(
        {"章3": [{"doc_id": "d1"}], "章1": [{"doc_id": "d2"}]}, outline, _make_docs(2)
    )

    assert list(binding) == ["章1", "章2", "章3"]


def test_fill_gaps_drops_unknown_doc_and_keeps_reason():
    """库外的 doc_id 一律丢弃；模型给的理由带进绑定，没给就不带那个键。"""
    outline = _make_outline(["章1"])
    binding = schedule_agent.fill_gaps(
        {"章1": [{"doc_id": "编造的", "reason": "幻觉"}, {"doc_id": "d1", "reason": " 贴题 "}]},
        outline, _make_docs(1),
    )

    assert [it["doc_id"] for it in binding["章1"]] == ["d1"]
    assert binding["章1"][0]["reason"] == "贴题"

    plain = schedule_agent.fill_gaps({"章1": [{"doc_id": "d1"}]}, outline, _make_docs(1))
    assert "reason" not in plain["章1"][0]


def test_fill_gaps_with_empty_binding_covers_everything():
    """空绑定退化成均匀铺开：每条承诺照样成立（模型一个字都没给的那条路）。"""
    outline = _make_outline(["章1", "章2", "章3"])
    docs = _make_docs(5)
    binding = schedule_agent.fill_gaps({}, outline, docs)

    assert list(binding) == ["章1", "章2", "章3"]
    assert _assigned(binding) == {d["id"] for d in docs}
    for items in binding.values():
        assert items


def test_fill_gaps_degenerate_inputs():
    """没有叶子章节 → 空（调用方据此走基线）；没有文献 → 每节空列表。"""
    assert schedule_agent.fill_gaps({}, {"chapters": []}, _make_docs(2)) == {}
    binding = schedule_agent.fill_gaps({}, _make_outline(["章1", "章2"]), [])
    assert binding == {"章1": [], "章2": []}


def test_fill_gaps_every_binding_has_page():
    """补出来的绑定项也有确定页码 —— 引用要能溯源到具体页。"""
    outline = _make_outline(["章1", "章2", "章3"])
    docs = _make_docs(2)
    binding = schedule_agent.fill_gaps({"章1": [{"doc_id": "d1"}]}, outline, docs)

    for items in binding.values():
        for it in items:
            assert it["page"] in (1, 2, 3, 4)
            assert it["doc_title"]
