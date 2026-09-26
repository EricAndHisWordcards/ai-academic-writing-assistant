"""测试：写作语言这一层的判据（收敛 / 单位 / 字符上限 / 输出语言宣告）。

这个模块只有六个纯函数，但它是**两个分发点的唯一判据**（`count_units` 的按字/按词、
`effective_format` 的格式合法性），所以每条判据都要有断言钉住 —— 尤其
`output_rule("zh") == ""` 这一条：中文路径下全部提示词逐字节不变这件事，
唯一的技术保证就是它。
"""
import pytest

from app import writing_lang


# ---------------------------------------------------------------
# normalize：读取侧的收敛，永不抛
# ---------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    ("zh", "zh"),
    ("en", "en"),
    ("EN", "en"),            # 大小写不该影响判据
    ("  en  ", "en"),        # 前端多传一个空格同样不该
    ("EN_US", "zh"),         # 认不出的语种回落中文，而不是当成英文
    ("", "zh"),
    (None, "zh"),            # 迁移前的行是 NULL
    (0, "zh"),               # 脏数据：非字符串也得收敛
    ("中文", "zh"),
])
def test_normalize_converges_to_a_supported_lang(raw, want):
    assert writing_lang.normalize(raw) == want


def test_supported_langs_are_what_labels_and_defaults_cover():
    """三张表必须同步：加一个语种只改 SUPPORTED_LANGS 是不够的 ——
    界面的标签与默认语种会各自缺一项，而缺的那一项不会报错，只会显示成空白。"""
    for lang in writing_lang.SUPPORTED_LANGS:
        assert lang in writing_lang.LANG_LABELS, f"{lang} 没有界面标签"
        assert writing_lang.normalize(lang) == lang, f"{lang} 自己都收敛不到自己"
    assert writing_lang.DEFAULT_LANG in writing_lang.SUPPORTED_LANGS


def test_default_lang_is_the_first_option():
    """界面下拉的顺序就是 SUPPORTED_LANGS 的顺序，默认语种必须排在第一个 ——
    否则新建项目是中文、下拉框的第一项却是英文。"""
    assert writing_lang.SUPPORTED_LANGS[0] == writing_lang.DEFAULT_LANG


# ---------------------------------------------------------------
# is_en / words_unit
# ---------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [("en", True), ("zh", False), ("EN", True),
                                       (None, False), ("fr", False)])
def test_is_en(raw, want):
    assert writing_lang.is_en(raw) is want


@pytest.mark.parametrize("raw, want", [("zh", "字"), ("en", "words"), (None, "字")])
def test_words_unit(raw, want):
    """单位是给用户看的，中文的写法就是「字」（不是「字符」）—— 它与
    generate_agent.count_units 的按字/按词同源，两处不能各说一套。"""
    assert writing_lang.words_unit(raw) == want


# ---------------------------------------------------------------
# cap_chars：中文的字符上限在英文里的等值
# ---------------------------------------------------------------
def test_cap_chars_widens_only_for_english():
    assert writing_lang.cap_chars("zh", 60) == 60, "中文口径一个字都不能动"
    assert writing_lang.cap_chars("en", 60) == 180
    assert writing_lang.cap_chars(None, 100) == 100


def test_cap_chars_lets_a_real_english_title_through():
    """这条是那个缺陷的正面证据：一个 12 词的英文论文标题有 80 来个字符，
    中文的 60 字上限会把它**截断在词中间**，而截完仍然像个标题。"""
    title = "The Effect of Enterprise Social Media on Tacit Knowledge Sharing"
    assert len(title) > 60
    assert len(title) <= writing_lang.cap_chars("en", 60)


# ---------------------------------------------------------------
# output_rule：zh 返回空串是全部「逐字节不变」保证的前提
# ---------------------------------------------------------------
def test_chinese_output_rule_is_empty():
    """**整个中文化的保证就在这一行**：空串意味着 zh 路径下提示词一个字节都没变，
    `tests/test_outline_agent.py` 里那批按中文串分发的用例才继续成立。"""
    assert writing_lang.output_rule("zh") == ""
    assert writing_lang.output_rule(None) == ""
    assert writing_lang.output_rule("fr") == ""


def test_english_output_rule_declares_the_language():
    rule = writing_lang.output_rule("en")
    assert rule.startswith("\n\n"), "要能直接拼在提示词末尾，自带分隔"
    assert "英文" in rule
    assert "不要输出任何中文字符" in rule


def test_english_output_rule_covers_json_field_values():
    """选题 / 聚类 / 设计提炼 / 材料分析四个 Agent 产出的都是结构化字段，
    其中一部分会变成论文里的一行字（选题标题、主题维度名、设计取值、材料要点）。
    只写「标题、摘要、正文段落」管不到它们 —— 那些字段留在中文里，正是要修的形态。"""
    rule = writing_lang.output_rule("en")
    assert "取值" in rule


def test_english_output_rule_lets_titles_keep_their_original_wording():
    """不写这一句，模型会把参考文献里**文献原文的题名与作者名**一起翻译掉。
    文末列表那样看着通顺，只是那条文献再也查不到了。"""
    rule = writing_lang.output_rule("en")
    assert "保留原文" in rule
