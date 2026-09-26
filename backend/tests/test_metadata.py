"""测试：文献元数据占位词的判定（app.metadata）。

这份判定的三个消费方 —— 写入侧（parse_agent / routers.projects）、显示侧
（citation_format）、存量迁移侧（db）—— 共用同一份清单。所以要测两个方向：
清单里的**每一个**词都判为缺失（漏一个就等于允许它印进参考文献列表），
以及真实值**一个都不能**被误抹（误伤真实作者名比漏掉一个占位词严重得多）。
"""
import pytest

from app import metadata


@pytest.mark.parametrize("raw", sorted(metadata.MISSING_META_VALUES))
def test_every_placeholder_becomes_empty(raw):
    assert metadata.clean_meta_value(raw) == ""


def test_english_placeholders_are_case_insensitive():
    """英文占位词不区分大小写：模型写 "Not Specified" 与 "NOT SPECIFIED" 一样处理。"""
    for raw in ("Not Specified", "NOT SPECIFIED", "N/A", "Unknown", "None", "NIL"):
        assert metadata.clean_meta_value(raw) == ""


def test_placeholder_with_surrounding_whitespace():
    assert metadata.clean_meta_value("  未提及 ") == ""
    assert metadata.clean_meta_value("\tNot specified\n") == ""


def test_real_values_survive():
    """真实值原样保留 —— 这一条是本文件最重要的断言。"""
    for raw in (
        "张三",
        "张三, 李四",
        "Smith J.",
        "Parag Desai, Ali Potia, Brian Salsberg",
        "王五等",
        "佚名",              # GB/T 7714 的正式写法，不是占位词
        "The World Bank",
        "2024",
        "Journal of Marketing",
        "无糖食品消费行为",   # 含「无」，但不是占位词
        "n-gram 语言模型",    # 含「-」，但不是占位词
    ):
        assert metadata.clean_meta_value(raw) == raw


def test_blank_and_none_and_non_string():
    assert metadata.clean_meta_value(None) == ""
    assert metadata.clean_meta_value("") == ""
    assert metadata.clean_meta_value("   ") == ""
    assert metadata.clean_meta_value(2024) == "2024"


def test_clean_meta_fields_only_touches_meta_fields():
    """只动 META_FIELDS 那三项：题名的缺失由 citation_format 用「（未命名文献）」兜底，
    summary 只作为提示词上下文，两者各归各自的规则管，不在这里处理。"""
    doc = {
        "title": "未提及",
        "authors": "未提及",
        "year": "未提供",
        "source": "Not specified",
        "summary": "未提及",
        "pages": [{"page": 1, "snippet": "片段"}],
    }
    out = metadata.clean_meta_fields(doc)

    assert (out["authors"], out["year"], out["source"]) == ("", "", "")
    assert out["title"] == "未提及"
    assert out["summary"] == "未提及"
    assert out["pages"] == [{"page": 1, "snippet": "片段"}]
    assert out is doc, "就地清洗应返回同一个 dict"
