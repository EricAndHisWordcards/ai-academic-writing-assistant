"""测试：个人著者姓名规范化（`app.names`）—— GB/T 7714-2015 / APA 7 / MLA 9。

这个模块最初是为「英文文献的著者形态与中文文献差别很大」而新增的：英文 PDF 首屏
给的是「名 姓」自然序、姓名之间写 `and` / `&`、姓只有首字母大写；数据库导出则常是
`Smith, J. K.` 这种逗号倒装。抽取侧原样把作者串交下来，渲染侧零处理 —— 于是印出来
的条目看着有作者，实际不符合 GB/T 7714，中文期刊的格式审查一眼就能挑出来。

**三列一起断言是这份表的存在理由。** 切分姓名的那一层（哪一截是姓、哪一截是名）是
三种格式共用的，而渲染形态分三份：GB/T 要 `SMITH J K`，APA 7 要 `Smith, J. K.`，
MLA 9 要 `Smith, John K.`。三列并排钉住，是为了让「共用切分层被顺手改坏」这件事
同时被三种格式的形态断言接住 —— 只测其中一列的话，改坏了另外两列看不出来。

**GB/T 那一列是回归线**：它的取值全部来自本模块只有 GB/T 一个格式时的原始断言，
一个字没动。加 APA / MLA 不该改写任何一条既有输出。
"""
import pytest

from app.names import normalize_authors, normalize_authors_apa, normalize_authors_mla

# (原始著者串, GB/T 期望, APA 期望, MLA 期望)
_CASES = [
    # ---- 自然序：姓是最后一个「词」（英文 PDF 首屏最常见的形态）----
    ("John K. Smith", "SMITH J K", "Smith, J. K.", "Smith, John K."),
    ("John K Smith", "SMITH J K", "Smith, J. K.", "Smith, John K."),
    ("Anna Doe", "DOE A", "Doe, A.", "Doe, Anna"),
    # 全大写自然序：不能因为「首词全大写」就把它当成已规范的姓在前形态
    ("JOHN K SMITH", "SMITH J K", "Smith, J. K.", "Smith, John K."),
    # 连字符名：GB/T 每段各取一个首字母；APA 保留连字符；MLA 全写
    ("Jean-Luc Picard", "PICARD J L", "Picard, J.-L.", "Picard, Jean-Luc"),
    ("John Smith-Jones", "SMITH-JONES J", "Smith-Jones, J.", "Smith-Jones, John"),
    # 荷兰/德语前置小品词跟着姓走
    ("J. D. van der Waals", "VAN DER WAALS J D", "van der Waals, J. D.",
     "van der Waals, J. D."),
    # ---- 逗号倒装：逗号之前是姓 ----
    ("Smith, J. K.", "SMITH J K", "Smith, J. K.", "Smith, J. K."),
    ("Smith, John K.", "SMITH J K", "Smith, J. K.", "Smith, John K."),
    ("van der Waals, J. D.", "VAN DER WAALS J D", "van der Waals, J. D.",
     "van der Waals, J. D."),
    # ---- 多人：GB/T 之间一律逗号，and / & 都要吃掉 ----
    ("John K. Smith, Anna Doe, R. Roe", "SMITH J K, DOE A, ROE R",
     "Smith, J. K., Doe, A., & Roe, R.", "Smith, John K., et al."),
    ("John K. Smith, Anna Doe, and R. Roe", "SMITH J K, DOE A, ROE R",
     "Smith, J. K., Doe, A., & Roe, R.", "Smith, John K., et al."),
    # MLA 把名全写**只在输入里还留着名的时候**做得到：这串已经只剩缩写，
    # `Smith, John K.` 里的 `John` 没有任何来源 —— 所以 MLA 列与 APA 列在这里同形。
    ("Smith, J. K., Doe, A., & Roe, R.", "SMITH J K, DOE A, ROE R",
     "Smith, J. K., Doe, A., & Roe, R.", "Smith, J. K., et al."),
    # 两位：APA 末位前 `&`，MLA 只有首位倒装、次位自然序
    ("SMITH J K; DOE A", "SMITH J K, DOE A", "Smith, J. K., & Doe, A.",
     "Smith, J. K., and A. Doe"),
    # ---- 已规范形态：规范化必须幂等 ----
    ("SMITH J K", "SMITH J K", "Smith, J. K.", "Smith, J. K."),
    ("SMITH J K, DOE A", "SMITH J K, DOE A", "Smith, J. K., & Doe, A.",
     "Smith, J. K., and A. Doe"),
    # ---- 中文：三种格式都原样保留（程序没有把中文名转写成拼音的能力）----
    ("张三", "张三", "张三", "张三"),
    ("张三, 李四, 王五", "张三, 李四, 王五", "张三, 李四, & 王五", "张三, et al."),
    ("王玲玲, 戴会超, 王琼", "王玲玲, 戴会超, 王琼", "王玲玲, 戴会超, & 王琼",
     "王玲玲, et al."),
    ("王玲玲，戴会超，王琼", "王玲玲, 戴会超, 王琼", "王玲玲, 戴会超, & 王琼",
     "王玲玲, et al."),
    # ---- 切不开的块：三种格式一致地原样输出 ----
    ("World Health Organization", "World Health Organization",
     "World Health Organization", "World Health Organization"),
    ("Smith", "Smith", "Smith", "Smith"),
    # ---- 空值 ----
    ("", "", "", ""),
    ("   ", "", "", ""),
]


@pytest.mark.parametrize("raw, gb, _apa, _mla", _CASES)
def test_normalize_authors(raw, gb, _apa, _mla):
    """GB/T 7714-2015 列：姓前名后、姓全大写、名缩写不加点、≤3 人全列。"""
    assert normalize_authors(raw) == gb


@pytest.mark.parametrize("raw, _gb, apa, _mla", _CASES)
def test_normalize_authors_apa(raw, _gb, apa, _mla):
    """APA 第 7 版列：姓前名后、名缩写**带点**、末位前 `&`、名不倒序去掉。"""
    assert normalize_authors_apa(raw) == apa


@pytest.mark.parametrize("raw, _gb, _apa, mla", _CASES)
def test_normalize_authors_mla(raw, _gb, _apa, mla):
    """MLA 第 9 版列：首位倒装、名全写、两位 `, and `、三位以上 `, et al.`。"""
    assert normalize_authors_mla(raw) == mla


def test_at_most_three_authors_are_listed_in_full():
    """≤3 人必须全列，且**不加** et al. —— 对 2 人也加标记是最容易写错的一条。"""
    out = normalize_authors("John K. Smith, Anna Doe, R. Roe")
    assert out == "SMITH J K, DOE A, ROE R"
    assert "et al." not in out


def test_more_than_three_authors_are_truncated_to_three():
    """GB/T：超过 3 人只著录前 3 位，其后加标记。第 4 位起不再出现。"""
    out = normalize_authors("John K. Smith, Anna Doe, R. Roe, P. Poe")
    assert out == "SMITH J K, DOE A, ROE R, et al."
    assert "POE" not in out


def test_truncation_marker_follows_the_documents_language():
    """`等` / `et al.` 按**该条文献的语言**选，不按界面语言。

    中文作者写的外文文献（`ZHANG Wei, …`）里一个汉字都没有，所以得到 `et al.` ——
    这正是英文兼容性的一个落点：按界面语言选的话它会印成 `等`。
    """
    assert normalize_authors("张三, 李四, 王五, 赵六").endswith(", 等")
    assert normalize_authors("ZHANG Wei, LI Ming, WANG Hua, CHEN Lei").endswith(
        ", et al."
    )


def test_organization_author_is_left_alone():
    """机构著者保持原形。把它按人名处理会拧成 `ORGANIZATION WORLD HEALTH`。"""
    assert normalize_authors("World Health Organization") == "World Health Organization"
    assert normalize_authors("联合国教科文组织") == "联合国教科文组织"


def test_bare_single_word_is_left_alone():
    """单个词无法判形态（可能是机构、也可能只写了姓）——原样保留，不猜。"""
    assert normalize_authors("Smith") == "Smith"


# ---------------------------------------------------------------
# APA / MLA 各自的人数据规则（都不是 GB/T 那种「三位截断」，别串了）
# ---------------------------------------------------------------
def _many(count: int) -> str:
    """造 `count` 位可辨认的著者，每位是 `S00 A.` 这种「姓 + 缩写」形态。

    第 19 / 20 / 21 位必须**各自可辨**，所以可辨认的那一截放在**姓**上：
    名的缩写在渲染时只取首字母（`A.` 恒为 `A.`），拿它当序号会全部撞在一起。
    """
    return "; ".join(f"S{i:02d} A." for i in range(count))


def test_apa_lists_twenty_authors_in_full():
    """APA 7 §9.8：≤20 位全部列出，末位前用 `&`、不出省略号。"""
    out = normalize_authors_apa(_many(20))
    assert "…" not in out
    assert "S19" in out
    assert out.endswith("& S19, A.")
    assert out.count("&") == 1


def test_apa_truncates_at_twenty_one_and_drops_the_ampersand():
    """≥21 位：前 19 位 + `…` + 末位，且此时末位前**不加 `&`**。

    「省号出现」与「`&` 消失」是同一条规定里的两半，必须一起断言 ——
    只断言省号的话，错的那一半看不出来（印成 `… & 末位` 就很常见）。
    """
    out = normalize_authors_apa(_many(21))
    assert out.count("…") == 1
    assert "&" not in out
    assert "S18" in out          # 第 19 位在（前 19 位全列）
    assert "S19" not in out      # 第 20 位被省掉
    assert "S20" in out          # 末位永远在
    assert out.endswith("S20, A.")


def test_mla_never_uses_ampersand_and_gb_never_uses_and():
    """三种格式的连接词互不串门：GB/T 逗号、APA `&`、MLA `and`。"""
    two = "John K. Smith, Anna Doe"
    gb, apa, mla = (
        normalize_authors(two), normalize_authors_apa(two), normalize_authors_mla(two)
    )
    assert gb == "SMITH J K, DOE A"
    assert apa == "Smith, J. K., & Doe, A."
    assert mla == "Smith, John K., and Anna Doe"
    assert "&" not in gb and "&" not in mla and " and " not in gb and " and " not in apa


def test_mla_keeps_the_comma_before_and():
    """`Smith, John K., and Alice Doe` —— `and` 前面那个逗号是 MLA 的固定形态。

    `, and ` 写成 ` and ` 是这里最常见的写错，而它看起来完全正常。
    """
    out = normalize_authors_mla("John K. Smith, Anna Doe")
    assert ", and " in out


def test_mla_truncates_at_three_with_et_al():
    """MLA：3 位及以上只写首位 + `, et al.`（不是 GB/T 的「前 3 位」）。"""
    out = normalize_authors_mla("John K. Smith, Anna Doe, R. Roe")
    assert out == "Smith, John K., et al."
    assert "DOE" not in out and "Roe" not in out


def test_all_three_render_formats_are_language_neutral():
    """三种渲染形态都不看语言：中文姓名在三种格式里都原样输出。

    这一条钉住的是「`等` / `et al.` 的语言判断只属于 GB/T」——
    APA / MLA 的人数据规则与文献语言无关（MLA 对 3 位以上中文著者也写 `et al.`）。
    """
    zh = "张三, 李四, 王五, 赵六"
    assert normalize_authors_apa(zh) == "张三, 李四, 王五, & 赵六"
    assert normalize_authors_mla(zh) == "张三, et al."
