"""测试：文献元数据提取（parse_agent）。

只测「提取出来的三个字段干不干净」这一件事：真正的 PDF 文本提取由 pdf_parser 负责，
提取质量由真实模型决定，这里打桩的是模型那一层。
"""
import asyncio
import re

import pytest

from app.agents import parse_agent
from app.config import settings
from app.llm import llm


def _extract(monkeypatch, payload: dict) -> dict:
    """把 LLM 打桩成返回给定 JSON，再跑一次元数据提取。"""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")

    async def fake_chat_json(messages, **kw):
        return payload

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return asyncio.run(parse_agent.extract_metadata("首屏文本", "a.pdf"))


def test_placeholder_metadata_is_cleaned(monkeypatch):
    """模型爱用「未提及」「Not specified」占位 —— 出这个函数时必须已经是空串。

    这一层不能只靠提示词：提示词说了「缺失就输出空串」，但模型偶尔不听话是常态。
    """
    out = _extract(monkeypatch, {
        "title": "题名", "authors": "未提及", "year": "Not specified",
        "source": "未知", "summary": "摘要",
    })
    assert (out["authors"], out["year"], out["source"]) == ("", "", "")
    # 只清元数据三项，题名与摘要不动
    assert out["title"] == "题名"
    assert out["summary"] == "摘要"


# 提示词那份 JSON 骨架里要求的全部键。
#
# **从提示词里现读，不在这里手抄一份**：手抄的那份将来会与提示词各自漂移，而漂移的
# 方向恰好是「提示词又要了一个字段，而 extract_metadata 的两条分支都没跟着给」——
# 那种漏只在真实调用（或未配置 LLM）的那条路上炸，是这一组用例存在的全部理由。
# 骨架用 `{{` `}}` 转义过（PARSE_PROMPT 要过 .format()），所以先剥一层。
def _prompt_keys() -> set[str]:
    block = parse_agent.PARSE_PROMPT.split("{{", 1)[1].split("}}", 1)[0]
    return set(re.findall(r'"(\w+)":', block))


def test_prompt_skeleton_keys_are_all_provided(monkeypatch):
    """提示词要的每一个键，正常分支都必须给得出（判据现读提示词）。

    这条与下面 `test_missing_keys_are_filled` 是**两个角度**：那条是手写的清单，
    读起来知道「设计上该有哪些字段」；这条不问清单、只问提示词，所以它拦得住
    「清单和提示词一起被忘掉」这种自洽的漂移。
    """
    keys = _prompt_keys()
    assert {"place", "edition", "publish_date"} <= keys, "提示词骨架自己得先要到这三项"
    out = _extract(monkeypatch, {"title": "题名"})
    missing = sorted(keys - set(out))
    assert not missing, f"提示词要了、两条清洗分支都没给：{missing}"


def test_missing_keys_are_filled(monkeypatch):
    """模型少给键时也要补全：下游按这些键取值。"""
    out = _extract(monkeypatch, {"title": "题名"})
    for field in (
        "authors", "year", "source", "summary",
        # 期刊著录四项（上一轮新增）：`_run_parse` 落库时逐键取用，缺键会变成
        # 一个 KeyError 或者一个落库的空洞 —— 两种都不该由「模型少给一个键」引起。
        "volume", "issue", "page_range", "source_type",
        # 非期刊类型的三项（本轮新增）：[M] 要出版地与版本项、[D]/[C] 要出版地、
        # [N] 要出版日期。它们同属「有模板才印得出标准形态」的那一组，见
        # citation_gb_types._TRIGGERS。
        "place", "edition", "publish_date",
    ):
        assert field in out
    assert (out["authors"], out["year"], out["source"]) == ("", "", "")
    assert (out["volume"], out["issue"], out["page_range"], out["source_type"]) == (
        "", "", "", ""
    )
    assert (out["place"], out["edition"], out["publish_date"]) == ("", "", "")


def test_journal_placeholders_are_cleaned(monkeypatch):
    """卷/期/页码也要过「占位词算缺失」这一关。

    这四项会被直接拼进文末参考文献（GB/T 7714 的期刊形态
    `刊名, 年, 卷(期): 起止页码.`），「未提及」漏过去就是
    「教育研究, 2023, 未提及(未提及): 未提及.」——比不印更糟。
    """
    out = _extract(monkeypatch, {
        "title": "题名", "volume": "未提及", "issue": "N/A",
        "page_range": "未提供", "source_type": "J",
    })
    assert (out["volume"], out["issue"], out["page_range"]) == ("", "", "")
    # 类型标识不是自由文本，清洗它的是渲染侧的白名单，这里原样留着
    assert out["source_type"] == "J"


def test_page_range_prefix_is_not_stripped_here(monkeypatch):
    """`pp. 56-75` 在这里原样保留 —— 剥前缀是渲染侧 normalize_pages 的活。

    两处分工：这里是「什么算缺失」，那里是「怎么印」。**刻意不在这里顺手剥**，
    否则同一个值有两个改写点，日后加一种页码写法就要改两处。
    """
    out = _extract(monkeypatch, {"title": "题名", "page_range": "pp. 56-75"})
    assert out["page_range"] == "pp. 56-75"


def test_source_type_is_not_whitelisted_at_extraction(monkeypatch):
    """越界的类型标识原样落库，由渲染侧收敛成 J。

    库里留原话的意义：用户打开文献编辑界面时，看到的是模型**到底写了什么**，
    而不是一个被悄悄改写过的 `J`（那会让人永远查不出模型爱填「期刊」这个词）。
    """
    out = _extract(monkeypatch, {"title": "题名", "source_type": "期刊"})
    assert out["source_type"] == "期刊"


def test_unconfigured_llm_fallback_also_has_every_prompt_key():
    """未配置 LLM 的兜底分支与正常分支**同形状**。

    少一个键不会在这里报错，而是在 `_run_parse` 落库那一步 —— 一个只在
    「没配 key」这条路上才出现的崩，恰好是平时跑不到的那条路。

    键集同样**现读提示词**：兜底那一份是手写的字典，它是这份清单里最容易被忘掉的一处
    （加字段的人改完提示词与清洗清单，通常想不到还有一个 from-scratch 的字典）。
    """
    out = asyncio.run(parse_agent.extract_metadata("首屏文本", "a.pdf"))
    missing = sorted(_prompt_keys() - set(out))
    assert not missing, f"提示词要了、兜底字典没给：{missing}"
    assert out["title"] == "a"


# ---------------------------------------------------------------
# PDF 硬换行合并
# ---------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, want",
    [
        # ---- 要合并的 ----
        # 英文断词连字符：接上，连字符去掉
        ("educa-\ntion", "education"),
        # 英文硬换行：换成空格。**下一行以大写开头也要合并** —— 那是人名，不是新段落
        ("John K. Smith, Anna\nDoe, R. Roe", "John K. Smith, Anna Doe, R. Roe"),
        # 续行带缩进时照样要合并（pypdf 常留行首空格）
        ("John K. Smith, Anna\n  Doe, R. Roe", "John K. Smith, Anna Doe, R. Roe"),
        # 行以逗号结尾：后面是汉字不加空格，是英文加空格
        ("张三, 李四,\n王五", "张三, 李四,王五"),
        ("Smith, J. K.;\nDoe, A.", "Smith, J. K.; Doe, A."),
        # 中文行内断行：直接接上，**不加空格**（中文之间加空格是错的）
        ("人工智能在教育中的\n应用研究", "人工智能在教育中的应用研究"),
        # ---- 以下四条是**刻意的不合并**，不是漏了 ----
        # 句号结尾 = 一句说完了，下一行是新段落/新小节/下一条参考文献
        ("Smith, Anna.\nDoe, Robert", "Smith, Anna.\nDoe, Robert"),
        (
            "[1] 张三. 题名[J]. 教育研究, 2023, 32(1): 56-75.\n"
            "[2] 李四. 另一篇[J]. 学报, 2024, 12(2): 1-9.",
            "[1] 张三. 题名[J]. 教育研究, 2023, 32(1): 56-75.\n"
            "[2] 李四. 另一篇[J]. 学报, 2024, 12(2): 1-9.",
        ),
        # 汉字行尾接非汉字（中英混排边界）：不猜
        ("一项研究\n[J]. 教育研究", "一项研究\n[J]. 教育研究"),
        # 非字母前的连字符不是断词（`-` 后是大写）
        ("Smith-\nJones", "Smith-\nJones"),
    ],
)
def test_merge_hard_wraps(raw, want):
    assert parse_agent.merge_hard_wraps(raw) == want


def test_merge_hard_wraps_known_cost():
    """把这条**已知代价**钉住：真的连字符复合词排到行尾会被误接。

    不修它是有意的 —— 区分「断词连字符」与「复合词连字符」要词典，而在题名/作者区
    遇到复合词换行的概率远低于 PDF 断词。写在这里是为了让下一个人知道它被看见过。
    """
    assert parse_agent.merge_hard_wraps("self-\nesteem") == "selfesteem"


def test_merge_hard_wraps_is_empty_safe():
    assert parse_agent.merge_hard_wraps("") == ""
    assert parse_agent.merge_hard_wraps(None) == ""


def test_parse_prompt_asks_for_the_journal_fields():
    """提示词必须**显式索取**卷期页与类型标识。

    这是本轮最容易被漏掉的一环：抽取侧原先只问 5 个字段，于是卷期页在源头就不存在，
    渲染侧再合规也无米下锅。用例锁的是「有没有问」这件事本身。
    """
    prompt = parse_agent.PARSE_PROMPT
    for key in ("volume", "issue", "page_range", "source_type"):
        assert key in prompt, key
    # 类型标识必须是**单选**字母表，否则模型会写「期刊论文」这种词
    assert "期刊论文 J" in prompt
    # 原有的防占位词段不能因为这轮扩字段被挤掉
    assert "一律输出空字符串" in prompt


def test_extract_truncates_after_merging(monkeypatch):
    """**先合并再截断**：反过来接缝正好落在截断点上，等于白接。

    造法：让断词贴着上限。原文 `N+1` 字（只差末尾那个 `b` 超限），合并后正好 `N`。
    先合并的会留下 `…ab`，先截断的留下 `…a-` 且 `b` 被切掉 —— 断言 `ab` 在不在，
    正好区分两种顺序。
    """
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    seen = {}

    async def fake_chat_json(messages, **kw):
        seen["content"] = messages[0]["content"]
        return {"title": "题名"}

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    head = "x" * (parse_agent.MAX_EXTRACT_CHARS - 2)
    asyncio.run(parse_agent.extract_metadata(head + "a-\nb", "a.pdf"))
    assert "ab" in seen["content"], "接缝被切掉了：合并发生在截断之后"


def test_real_metadata_survives(monkeypatch):
    out = _extract(monkeypatch, {
        "title": "题名", "authors": "Smith J.", "year": "2023", "source": "教育研究",
    })
    assert (out["authors"], out["year"], out["source"]) == ("Smith J.", "2023", "教育研究")


def test_llm_failure_propagates(monkeypatch):
    """调用失败必须抛出去，不能被降级成一份「看起来正常」的空元数据。

    这是既存缺陷的回归测试：此处原有一个裸 except，失败时返回
    `{title: 文件名, 其余全空}`，与「这篇文献真的没写作者」在界面上完全无法区分，
    用户因此没有任何理由去重试。承接方是 `_run_parse` 的逐篇 try
    （见 test_api.py::test_parse_failure_keeps_already_parsed_documents）。
    """
    monkeypatch.setattr(settings, "llm_api_key", "test-key")

    async def boom(messages, **kw):
        raise RuntimeError("模型服务不可用")

    monkeypatch.setattr(llm, "chat_json", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(parse_agent.extract_metadata("首屏文本", "a.pdf"))


def test_non_dict_reply_raises(monkeypatch):
    """模型返回数组（合法 JSON，但不是我们要的对象）时同样抛，不静默降级。"""
    monkeypatch.setattr(settings, "llm_api_key", "test-key")

    async def fake_chat_json(messages, **kw):
        return ["一个数组"]

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    with pytest.raises(ValueError):
        asyncio.run(parse_agent.extract_metadata("首屏文本", "a.pdf"))
