"""测试：参考文献格式化（GB/T 7714 / APA / MLA）。"""
import pytest

from app.citation_format import (
    SUPPORTED_FORMATS,
    citation_formats_for,
    clean_source_type,
    default_format_for,
    effective_format,
    format_note,
    format_reference,
    normalize_pages,
    reference_runs,
)
from app.citation_gb_types import normalize_publish_date


@pytest.fixture
def ref():
    """与生产路径 `_plan_citations` 产出的条目同形状（T9）。

    本轮之前生产只给 num / doc_id / doc_title / authors / year / source 六个字段，
    旧夹具却把 source_type / volume / issue / pages 填满 —— 那是在练一条生产永不走的
    富字段分支：改坏默认值、改坏缺页降级，用例照旧绿。所以夹具按生产形状收窄。

    本轮起生产会**真的**传这四项，但它们常常是空的（抽取侧没抽到就是空串），
    所以夹具仍按「抽到作者与来源、没抽到卷期页」这个**最常见**的形态来 ——
    降级路径照样被跑到。填满卷期页的富形态另有专门的用例，不再依赖这个夹具。
    """
    return {
        "num": 1,
        "doc_id": "d1",
        "doc_title": "人工智能在教育中的应用",
        "authors": "张三",
        "year": "2023",
        "source": "教育研究",
        "volume": "",
        "issue": "",
        "pages": "",
        "source_type": "",
    }


def test_supported_formats():
    assert set(SUPPORTED_FORMATS) == {"gb7714", "apa", "mla"}


def test_gb7714_format(ref):
    out = format_reference(ref, "gb7714", "bracket")
    assert out.startswith("[1]")
    assert "张三" in out
    assert "人工智能在教育中的应用[J]" in out
    assert "2023" in out


def test_gb7714_with_superscript(ref):
    out = format_reference(ref, "gb7714", "superscript")
    assert out.startswith("1.")
    assert not out.startswith("[1]")


def test_apa_format(ref):
    out = format_reference(ref, "apa", "bracket")
    assert out.startswith("[1]")
    assert "张三" in out
    assert "(2023)" in out
    assert "人工智能在教育中的应用" in out


def test_mla_format(ref):
    out = format_reference(ref, "mla", "bracket")
    assert out.startswith("[1]")
    assert '"人工智能在教育中的应用."' in out
    assert "2023" in out


def test_default_is_gb7714(ref):
    """未指定格式时默认 GB/T 7714。"""
    out = format_reference(ref, "unknown", "bracket")
    # unknown 格式回退到 gb7714
    assert "[J]" in out


def test_missing_fields_no_crash(ref):
    """字段缺失时不应崩溃。"""
    minimal = {"num": 1, "doc_title": "题名"}
    for fmt in SUPPORTED_FORMATS:
        out = format_reference(minimal, fmt, "bracket")
        assert "题名" in out


# ---------------------------------------------------------------
# 元数据占位词的降级（本轮修复的缺陷：模型把「未提及」当作者名印了出来）
# ---------------------------------------------------------------
def test_placeholder_author_is_treated_as_absent(ref):
    """占位词不算作者：绝不能印出「[1] 未提及. 题名[J]. 来源, 未提及.」这种一眼假的条目。"""
    for fmt in SUPPORTED_FORMATS:
        out = format_reference(
            {**ref, "authors": "未提及", "year": "Not specified", "source": "未知"},
            fmt, "bracket",
        )
        assert "未提及" not in out, f"{fmt} 把占位词当作者印了出来：{out}"
        assert "Not specified" not in out
        assert "未知" not in out


def test_gb7714_without_author_starts_with_title(ref):
    """GB/T 7714 无责任者时题名打头。"""
    out = format_reference({**ref, "authors": ""}, "gb7714", "bracket")
    assert out == f"[1] {ref['doc_title']}[J]. {ref['source']}, {ref['year']}."


def test_apa_with_author_keeps_author_first(ref):
    """有作者时是「作者. (年份). 题名. 来源.」——改降级规则不能把正常顺序带歪。

    作者位后面那个句点是 APA 的元素分隔点（`Smith, J. K. (2020). …`）。中文著者
    不倒装、不缩写（程序不罗马化中文名），所以这里印成 `张三.`。
    """
    out = format_reference(ref, "apa", "bracket")
    assert out == f"[1] {ref['authors']}. ({ref['year']}). {ref['doc_title']}. {ref['source']}."


def test_apa_without_author_puts_title_first(ref):
    """APA 第 7 版：无作者时题名顶上作者位，年份跟在题名之后。"""
    out = format_reference({**ref, "authors": ""}, "apa", "bracket")
    assert out == f"[1] {ref['doc_title']}. ({ref['year']}). {ref['source']}."


def test_apa_without_year_writes_n_d():
    """APA 缺年份写 (n.d.)，而不是把括号整个省掉。"""
    minimal = {"num": 1, "doc_title": "题名"}
    assert "(n.d.)" in format_reference(minimal, "apa", "bracket")
    assert "(n.d.)" in format_reference({**minimal, "authors": "张三"}, "apa", "bracket")


@pytest.mark.parametrize("fmt", ["gb7714", "mla"])
def test_abbreviated_author_does_not_double_the_period(ref, fmt):
    """`Smith J.` 自带句点，不能再补一个 —— 实测曾印出「Smith J..」。

    两种格式的形态不同但性质相同：GB/T 规范成姓全大写、名缩写（`SMITH J.`），
    MLA 倒装并给缩写补点（`Smith, J.`）。断言因此按格式分开看**具体形态**，
    而「不出现两个句点」这条性质两边一起锁。
    """
    out = format_reference({**ref, "authors": "Smith J."}, fmt, "bracket")
    assert ".." not in out, out
    expected_author = "SMITH J." if fmt == "gb7714" else "Smith, J."
    assert expected_author in out


def test_abbreviated_author_is_normalized_and_keeps_one_period():
    """GB/T 下姓名被规范化，句点仍只有一个：`Smith J.` → `SMITH J.`。

    这条用例原来锁的是「作者名自带的句点不能被吃掉」。引入姓名规范化之后，
    作者串里的句点由 app.names 统一去掉、再由 _as_sentence 补回一个 ——
    「不出现两个句点」这个性质没变，但形态变了，所以断言跟着改。
    """
    out = format_reference(
        {"num": 1, "doc_title": "T", "authors": "Smith J."}, "gb7714", "bracket"
    )
    assert out.startswith("[1] SMITH J. T[J].")
    assert ".." not in out


def test_source_type_defaults_to_journal_when_absent(ref):
    """没抽到类型标识（空串）时必须落到默认 [J] —— 别再印出一个空的方括号。"""
    out = format_reference({**ref, "source_type": ""}, "gb7714", "bracket")
    assert "[J]" in out
    assert "[]" not in out


# ---------------------------------------------------------------
# 期刊著录段（本轮新增：卷 / 期 / 页码 / 类型标识）
# ---------------------------------------------------------------
def test_gb7714_legacy_output_is_byte_identical(ref):
    """**本轮最重要的一条回归线**：没有卷期页的条目，输出必须与改动前逐字节相同。

    库里绝大多数条目都没有这三项（抽取侧从来没有索取过它们），这次改动不该让
    它们中的任何一条被改写。tests/test_db.py 里那三条逐字节断言是同一约束的哨兵。
    """
    assert format_reference(ref, "gb7714", "bracket") == (
        "[1] 张三. 人工智能在教育中的应用[J]. 教育研究, 2023."
    )


@pytest.mark.parametrize(
    "volume, issue, pages, want_seg",
    [
        ("32", "1", "56-75", "教育研究, 2023, 32(1): 56-75."),
        ("32", "1", "", "教育研究, 2023, 32(1)."),
        ("32", "", "56-75", "教育研究, 2023, 32: 56-75."),
        # 无卷有期：期**直接贴在年后面**，中间不加逗号（GB/T 期刊形态如此）
        ("", "1", "56-75", "教育研究, 2023(1): 56-75."),
        ("", "1", "", "教育研究, 2023(1)."),
        ("", "", "56-75", "教育研究, 2023: 56-75."),
        # 全空 → 降级回旧形态，逐字节一致
        ("", "", "", "教育研究, 2023."),
    ],
)
def test_gb7714_journal_segment_degrades_by_missing_fields(
    ref, volume, issue, pages, want_seg
):
    out = format_reference(
        {**ref, "volume": volume, "issue": issue, "pages": pages}, "gb7714", "bracket"
    )
    assert out == f"[1] 张三. 人工智能在教育中的应用[J]. {want_seg}"


@pytest.mark.parametrize(
    "source, year, want_seg",
    [
        ("教育研究", "", "教育研究."),
        ("", "2023", "2023."),
        # 来源与年份都没有 → 整段不输出，不能留一个孤零零的句点
        ("", "", None),
    ],
)
def test_gb7714_source_segment_degrades_by_missing_fields(ref, source, year, want_seg):
    out = format_reference({**ref, "source": source, "year": year}, "gb7714", "bracket")
    if want_seg is None:
        assert out == "[1] 张三. 人工智能在教育中的应用[J]."
    else:
        assert out == f"[1] 张三. 人工智能在教育中的应用[J]. {want_seg}"


def test_gb7714_full_journal_entry(ref):
    """一整套齐备时期刊条目的目标形态（对照 GB/T 7714-2015 的期刊模板）。"""
    out = format_reference(
        {**ref, "volume": "32", "issue": "1", "pages": "56-75", "source_type": "J"},
        "gb7714",
        "bracket",
    )
    assert out == "[1] 张三. 人工智能在教育中的应用[J]. 教育研究, 2023, 32(1): 56-75."


def test_non_journal_type_letter_is_printed(ref):
    """非期刊条目印它**自己**的字母 —— 在一本专著上印 [J] 是明知而印错。

    **这条是 `in` 判定，任何形状都能过** —— 它只拦得住「印成 [J]」这一种坏法。
    逐字节的哨兵在下一条，那条才是本轮改 `_gb7714` 时真正的安全网。
    """
    out = format_reference({**ref, "source_type": "M"}, "gb7714", "bracket")
    assert "[M]" in out
    assert "[J]" not in out


# ---------------------------------------------------------------
# 非期刊类型的完整著录模板（本轮新增：place / edition / publish_date）
# ---------------------------------------------------------------
NON_JOURNAL_TYPES = ("M", "D", "C", "N")


@pytest.mark.parametrize("source_type", NON_JOURNAL_TYPES)
def test_gb7714_non_journal_legacy_output_is_byte_identical(ref, source_type):
    """**「新字段全空 → 输出逐字节不变」是这一轮不动存量项目的全部依据。**

    非期刊类型此前与期刊共用同一条兜底路径（只把自己那个类型字母印出来）。本轮给
    它们各自加了完整模板，但触发条件是「**本类型专属的**新列至少一个非空」——
    新增的 place / edition / publish_date 在全部存量条目上都是空的，所以它们必须
    原样走出改动前那一版。这四条断言描述的就是改动前的输出。

    `source` 在这个夹具里是**非空**的（"教育研究"），这一点是刻意的：它同时是
    「触发条件绝不读 source」那条约定的哨兵。存量里的 `[C]`/`[N]` 条目 source 基本
    都非空 —— 一旦把 source 算进触发条件，这类条目会集体从 `论文集名, 2023.` 变成
    `论文集名. 2023.`，而这条断言会当场变红。
    """
    assert format_reference({**ref, "source_type": source_type}, "gb7714", "bracket") == (
        f"[1] 张三. 人工智能在教育中的应用[{source_type}]. 教育研究, 2023."
    )


def test_gb7714_type_trigger_is_per_type_not_a_union(ref):
    """触发字段**按类型分开**，不取并集。

    提示词一旦开始索取新字段，模型会在每篇文献上都试着填。若触发条件写成「新列有
    任一非空」，一条专著被填了报纸日期就会掉进报纸的模板里，印出
    `题名[M]. 人民日报, 2023-05-04.` 这种四不像。这里是那条约定的哨兵。
    """
    out = format_reference(
        {**ref, "source_type": "M", "publish_date": "2023-05-04"}, "gb7714", "bracket"
    )
    assert out == "[1] 张三. 人工智能在教育中的应用[M]. 教育研究, 2023."


def test_gb7714_monograph_template(ref):
    """`[M]` 专著：`主要责任者. 题名[M]. 版本项. 出版地: 出版者, 出版年: 引文页码.`

    出版者取 `source` —— 提示词里 `source` 的写法就是「期刊/会议/来源」、前端标签
    是「来源（刊名 / 出版社 / 学位授予单位 / 论文集名 / 报纸名）」，专著上它装的就是
    出版社。不为它另开一列：模型会把出版社抽进 `source`，另开的那一列恒空，
    模板就永远不会触发。
    """
    out = format_reference(
        {**ref, "source_type": "M", "place": "北京", "edition": "第3版",
         "source": "高等教育出版社", "year": "2020", "pages": "56-75"},
        "gb7714", "bracket",
    )
    assert out == (
        "[1] 张三. 人工智能在教育中的应用[M]. 第3版. 北京: 高等教育出版社, 2020: 56-75."
    )


@pytest.mark.parametrize(
    "extra, want_tail",
    [
        # 缺版本项 → 那一整段不输出（不是留一个孤零零的句点）
        ({"place": "北京", "source": "高等教育出版社", "year": "2020"},
         "北京: 高等教育出版社, 2020."),
        # 缺出版者（source 空）→ 只印出版地，冒号跟着一起省
        ({"place": "北京", "year": "2020"}, "北京, 2020."),
        # 缺出版地 → 只剩出版者
        ({"edition": "第3版", "source": "高等教育出版社", "year": "2020"},
         "第3版. 高等教育出版社, 2020."),
        # 缺出版年 → 页码用冒号直接接（与 _journal_segment 的收尾约定同型）
        ({"place": "北京", "source": "高等教育出版社", "pages": "56-75"},
         "北京: 高等教育出版社: 56-75."),
        # 只有版本项：触发成立（它是本类型的专属列），出版项整段不输出
        ({"edition": "第3版", "source": "", "year": ""}, "第3版."),
    ],
)
def test_gb7714_monograph_degrades_by_missing_fields(ref, extra, want_tail):
    out = format_reference(
        {**ref, "source_type": "M", "source": "", "year": "", **extra},
        "gb7714", "bracket",
    )
    assert out == f"[1] 张三. 人工智能在教育中的应用[M]. {want_tail}"


def test_gb7714_dissertation_template(ref):
    """`[D]` 学位论文：`出版地: 学位授予单位, 出版年.`

    培养单位取 `source`（学位论文的 `source` 位置装的就是学校/院所）。学位论文
    **不印页码** —— 它没有「起止页码」这一项，印出来是凭空多一个字段。
    """
    out = format_reference(
        {**ref, "source_type": "D", "place": "北京", "source": "北京大学", "year": "2023"},
        "gb7714", "bracket",
    )
    assert out == "[1] 张三. 人工智能在教育中的应用[D]. 北京: 北京大学, 2023."


def test_gb7714_conference_paper_template(ref):
    """`[C]` 论文集析出：`析出题名[C]//论文集名. 出版地, 出版年: 引文页码.`

    `//` **紧贴**类型标识、前后不留空格 —— 这是 GB/T 区分「析出文献」的写法，不是
    句点。论文集主要责任者这一项没有字段可装，如实不印（与「不做 [J/OL]」同一体例：
    宁可如实少印，也不印一个猜出来的）。
    """
    out = format_reference(
        {**ref, "source_type": "C", "place": "北京", "year": "2023", "pages": "12-20",
         "source": "第五届全国人工智能会议论文集"},
        "gb7714", "bracket",
    )
    assert out == (
        "[1] 张三. 人工智能在教育中的应用[C]//第五届全国人工智能会议论文集. "
        "北京, 2023: 12-20."
    )


def test_gb7714_newspaper_template(ref):
    """`[N]` 报纸：`报纸名, 出版日期.`，日期用 `YYYY-MM-DD`。

    报纸日期是**日**级精度，而 `year` 那一列装不下（提示词要求它只填 4 位年份），
    所以单独一列 `publish_date`。版次（GB/T 作 `报纸名, 出版日期(版次).`）没有字段
    可装，如实不印。
    """
    out = format_reference(
        {**ref, "source_type": "N", "year": "", "source": "人民日报",
         "publish_date": "2023-05-04"},
        "gb7714", "bracket",
    )
    assert out == "[1] 张三. 人工智能在教育中的应用[N]. 人民日报, 2023-05-04."


def test_gb7714_newspaper_without_source_prints_only_the_date(ref):
    """报纸名缺失时只剩日期 —— 不能留一个「, 2023-05-04.」那样的前置逗号。"""
    out = format_reference(
        {**ref, "source_type": "N", "year": "", "source": "", "publish_date": "2023-05-04"},
        "gb7714", "bracket",
    )
    assert out == "[1] 张三. 人工智能在教育中的应用[N]. 2023-05-04."


@pytest.mark.parametrize("fmt", ["apa", "mla"])
def test_non_journal_templates_do_not_touch_apa_or_mla(ref, fmt):
    """新模板完全长在 GB/T 那条分支里 —— APA / MLA 的输出一个字都不该变。

    类型标识、`//`、出版地都只属于 GB/T 7714：APA / MLA 压根不读 `source_type`，
    也不读本轮新增的三列（它们的容器名/出版者形态与 GB/T 不是一回事，真要合规是
    另一轮的事）。填满新字段而输出不变，是「这条分支没被串进去」的判据。
    """
    plain = format_reference({**ref, "authors": "Smith J."}, fmt, "bracket")
    filled = format_reference(
        {**ref, "authors": "Smith J.", "source_type": "M", "place": "北京",
         "edition": "第3版", "publish_date": "2023-05-04"},
        fmt, "bracket",
    )
    assert plain == filled


@pytest.mark.parametrize(
    "raw, want",
    [
        ("M", "M"),
        ("d", "D"),  # 小写收敛成大写
        (" J ", "J"),  # 两侧空白
        # 越界一律回落 J：这个值会被直接拼进正文文本，而它来自模型输出
        ("X", "J"),
        ("", "J"),
        (None, "J"),
        ("[M]", "J"),
        # `[J/OL]` 这类电子双标识本轮不做：按标准它必须跟 URL 与引用日期配对，
        # 印一个没有 URL 的 `[J/OL]` 比 `[J]` 更不合规。所以它落到 J。
        ("J/OL", "J"),
    ],
)
def test_source_type_whitelist(raw, want):
    assert clean_source_type(raw) == want


@pytest.mark.parametrize(
    "raw, want",
    [
        ("56-75", "56-75"),
        ("56–75", "56-75"),  # en dash（英文 PDF 的常见印法）
        ("56—75", "56-75"),  # em dash
        ("56 - 75", "56-75"),
        ("pp. 56-75", "56-75"),
        ("P.56-75", "56-75"),
        ("56", "56"),
        # 认不出的形态原样保留，不猜（电子刊的 e-locator）
        ("e12345", "e12345"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_pages(raw, want):
    assert normalize_pages(raw) == want


@pytest.mark.parametrize(
    "raw, want",
    [
        ("2023-05-04", "2023-05-04"),
        ("2023/5/4", "2023-05-04"),        # 补零到两位
        ("2023.5.4", "2023-05-04"),
        ("2023年5月4日", "2023-05-04"),      # 中文印法（报纸/网页上最常见）
        ("2023 年 5 月 4 日", "2023-05-04"),
        ("  2023-05-04  ", "2023-05-04"),
        # 认不出的形态原样返回：只归一化，不猜测（与 normalize_pages 同一条纪律）。
        # 特别地**不把 `2023` 补成一个假日期** —— 4 位年份是已知的、日级精度是未知的。
        ("2023", "2023"),
        ("2023年5月", "2023年5月"),
        ("2023-13-45", "2023-13-45"),      # 形态对但月份非法：不改写，交给用户看
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_publish_date(raw, want):
    assert normalize_publish_date(raw) == want


def test_author_normalization_differs_per_format(ref):
    """三种格式各自规范化姓名 —— 形态不同，但都不再是「原样印出」。

    这条用例的前身叫 `test_author_normalization_is_gb7714_only`，它锁的是
    「姓名规范化只进 GB/T 分支、APA / MLA 原样输出」。那是 APA / MLA 还是占位实现
    时的事实，本轮**有意推翻**：APA / MLA 现在也规范姓名（各自的形态），所以改名并
    同时断言三种形态。留着旧名字与旧前提会让这条用例变成一句假话。
    """
    src = {**ref, "authors": "Smith, J. K."}
    gb = format_reference(src, "gb7714", "bracket")
    assert gb.startswith("[1] SMITH J K.")
    apa = format_reference(src, "apa", "bracket")
    assert "Smith, J. K." in apa
    mla = format_reference(src, "mla", "bracket")
    assert "Smith, J. K." in mla


# ---------------------------------------------------------------
# APA 第 7 版（本轮把占位实现做成真身）
# ---------------------------------------------------------------
def test_apa_full_journal_entry(ref):
    """全字段 APA 期刊条目（对照 APA 7 的期刊模板）。"""
    out = format_reference(
        {**ref, "authors": "Smith, John K.", "year": "2020",
         "doc_title": "Title of the article", "source": "Journal Name",
         "volume": "32", "issue": "1", "pages": "56-75"},
        "apa", "bracket",
    )
    assert out == "[1] Smith, J. K. (2020). Title of the article. Journal Name, 32(1), 56-75."


def test_apa_tail_normalizes_pages(ref):
    """APA 的页码也要归一到半角连字符 —— 英文 PDF 常印 en dash。"""
    out = format_reference(
        {**ref, "pages": "56–75", "volume": "32"}, "apa", "bracket"
    )
    assert "32, 56-75." in out


def test_apa_tail_keeps_volume_when_source_missing(ref):
    """刊名没抽到、卷期页抽到了 —— 卷期页不能跟着一起丢（读者还要知道是第几卷）。"""
    out = format_reference(
        {**ref, "source": "", "volume": "32", "issue": "1", "pages": "56-75"},
        "apa", "bracket",
    )
    assert "32(1), 56-75." in out


def test_apa_no_source_no_volume_no_pages_has_no_trailing_junk(ref):
    """刊源段整段为空时不能留一个孤零零的句点。"""
    out = format_reference({**ref, "source": "", "year": ""}, "apa", "bracket")
    assert out == f"[1] 张三. (n.d.). {ref['doc_title']}."


@pytest.mark.parametrize("count, want_ellipsis, want_amp", [(20, False, True), (21, True, False)])
def test_apa_author_count_boundary(ref, count, want_ellipsis, want_amp):
    """APA 7 §9.8：≤20 位全列（末位前 `&`）；≥21 位列前 19 + `…` + 末位（**无 `&`**）。

    21 位那条边界最容易写成「和 20 位一样、只是中间省了几个」—— 省号的出现**同时**
    取消了末位前的 `&`，这两件事必须一起断言，否则错的那一半看不出来。
    """
    names = "; ".join(f"G{i}. S{i:02d}" for i in range(count))
    out = format_reference({**ref, "authors": names}, "apa", "bracket")

    assert ("…" in out) is want_ellipsis
    assert ("&" in out) is want_amp
    assert "S18" in out
    if count == 20:
        assert "S19" in out
    else:
        assert "S19" not in out
    # 末位（最后一位著者）**永远在**：省号省的只是中间那一段
    assert f"S{count - 1:02d}" in out


def test_apa_does_not_invent_a_translation(ref):
    """`title_en` 为空（存量文献、或题名本来就是英文）时只印原题名。

    宁可少一个方括号，也不能印一个猜出来的译名 —— 存量文献全都没有这一列，
    这条降级路径在真实库里是**常态**而不是边界。
    """
    out = format_reference({**ref, "title_en": ""}, "apa", "bracket")
    # 判据取整串而不是「不含 `[`」：序号前缀本身就是 `[1]`，那样断言恒假。
    assert out == f"[1] 张三. (2023). {ref['doc_title']}. 教育研究."


def test_apa_non_english_title_gets_bracketed_translation(ref):
    """APA 7 §9.38：非英语文献**原题名在前、英译在后方括号里**。"""
    out = format_reference(
        {**ref, "title_en": "Application of AI in Education"}, "apa", "bracket"
    )
    assert f"{ref['doc_title']} [Application of AI in Education]." in out


def test_apa_translation_that_repeats_the_title_is_dropped(ref):
    """模型把原题名原样回填时不能印成「题名 [题名]」——那不是英译，是回声。"""
    out = format_reference({**ref, "title_en": ref["doc_title"]}, "apa", "bracket")
    assert out.count(ref["doc_title"]) == 1


def test_apa_placeholder_translation_is_dropped(ref):
    """「未提及」当译名印进方括号，与当作者名印出来一样假。"""
    out = format_reference({**ref, "title_en": "未提及"}, "apa", "bracket")
    assert out == f"[1] 张三. (2023). {ref['doc_title']}. 教育研究."


def test_gb7714_ignores_title_en(ref):
    """GB/T 7714 不读英译题名（它没有这条要求），方括号里只该有类型标识。"""
    out = format_reference(
        {**ref, "title_en": "Application of AI in Education"}, "gb7714", "bracket"
    )
    assert out == "[1] 张三. 人工智能在教育中的应用[J]. 教育研究, 2023."


# ---------------------------------------------------------------
# MLA 第 9 版（同上，本轮做成真身）
# ---------------------------------------------------------------
def test_mla_full_journal_entry(ref):
    """全字段 MLA 条目：容器里的卷/期/页必须带 `vol.` / `no.` / `pp.` 前缀。

    首位著者的**名写全**（`Smith, John K.`）—— 这与 APA 的 `Smith, J. K.` 是同一处
    输入下最显眼的差别，所以这条用全名输入、不靠缩写输入糊过去。
    """
    out = format_reference(
        {**ref, "authors": "Smith, John K.", "year": "2020",
         "doc_title": "Title of the article", "source": "Journal Name",
         "volume": "32", "issue": "1", "pages": "56-75"},
        "mla", "bracket",
    )
    assert out == (
        '[1] Smith, John K. "Title of the article." '
        "Journal Name, vol. 32, no. 1, 2020, pp. 56-75."
    )


def test_mla_year_follows_volume_not_before_it(ref):
    """MLA 的要素顺序是 容器 → 卷 → 期 → **年** → 页（与 APA 的「卷(期), 页」不同）。

    照抄 APA 的顺序会把年份写到卷期前面 —— 这是两种格式之间最容易串的一处。
    """
    out = format_reference(
        {**ref, "volume": "32", "issue": "1", "pages": "56-75"}, "mla", "bracket"
    )
    assert "教育研究, vol. 32, no. 1, 2023, pp. 56-75." in out


@pytest.mark.parametrize(
    "pages, want",
    [
        ("56-75", "pp. 56-75."),
        ("56", "p. 56."),  # 单页用 p.（判据是页码里有没有连字符）
        ("56–75", "pp. 56-75."),  # en dash 先归一，`pp.` 判据因此仍然成立
        ("pp. 56-75", "pp. 56-75."),  # 自带前缀先被剥掉，不会印成 `pp. pp. 56-75`
    ],
)
def test_mla_page_prefix(ref, pages, want):
    out = format_reference({**ref, "pages": pages}, "mla", "bracket")
    assert out.endswith(want), out


def test_mla_degrades_without_source_or_year(ref):
    """容器信息全缺时不能留一串孤零零的逗号。"""
    out = format_reference({**ref, "source": "", "year": ""}, "mla", "bracket")
    assert out == f'[1] 张三. "{ref["doc_title"]}."'


def test_mla_non_english_title_gets_bracketed_translation(ref):
    """MLA 同样要求非英语文献给英译（本轮与 APA 共用 `_title_of`）。"""
    out = format_reference(
        {**ref, "title_en": "Application of AI in Education"}, "mla", "bracket"
    )
    assert f'"{ref["doc_title"]} [Application of AI in Education]."' in out


# ---------------------------------------------------------------
# 语言 → 格式（用户要求：「选英文就不该能选 GB」）
# ---------------------------------------------------------------
def test_english_never_offers_gb7714():
    """英文写作下 GB/T 7714 **不在**可选项里 —— 这是那件事的唯一数据源。"""
    assert citation_formats_for("en") == ["apa", "mla"]
    assert "gb7714" not in citation_formats_for("en")


def test_chinese_keeps_all_three():
    """中文写作下三项都在（作者拍板），只是 APA/MLA 带一句「中文期刊极少用」的提示。"""
    assert citation_formats_for("zh") == SUPPORTED_FORMATS
    assert format_note("zh", "apa") != ""
    assert format_note("zh", "mla") != ""
    # 中文下的默认格式（GB/T）没什么要提示的；英文下两种格式都是常用形态。
    assert format_note("zh", "gb7714") == ""
    assert format_note("en", "apa") == ""


@pytest.mark.parametrize(
    "lang, want_formats, want_default",
    [("zh", SUPPORTED_FORMATS, "gb7714"), ("en", ["apa", "mla"], "apa")],
)
def test_formats_and_defaults_by_lang(lang, want_formats, want_default):
    assert citation_formats_for(lang) == want_formats
    assert default_format_for(lang) == want_default
    # 未知语言收敛到中文（而不是抛异常，也不是造出一份空的格式清单）
    assert citation_formats_for("fr") == SUPPORTED_FORMATS
    assert default_format_for(None) == "gb7714"


@pytest.mark.parametrize(
    "lang, fmt, want",
    [
        ("zh", "gb7714", "gb7714"),
        ("zh", "apa", "apa"),          # 中文下 APA 合法（决策 2）
        ("en", "apa", "apa"),
        ("en", "mla", "mla"),
        ("en", "gb7714", "apa"),       # 这一条就是「英文不能选 GB」
        ("en", "nonsense", "apa"),
        ("zh", "nonsense", "gb7714"),
        (None, None, "gb7714"),
    ],
)
def test_effective_format_converges(lang, fmt, want):
    """格式解析的唯一入口：越界、或不被该语言接受的格式，一律回落到该语言的默认值。"""
    assert effective_format(lang, fmt) == want


def test_english_lang_cannot_render_gb7714(ref):
    """库里存着 `en + gb7714` 这种脏组合时，渲染出来也只可能是 APA。

    脏组合有两个真实来源：改过语言但没跟着改格式的存量项目、以及手改过的库。
    印一份格式错了的列表，比回落到该语言的默认格式坏得多。
    """
    out = format_reference(ref, "gb7714", "bracket", lang="en")
    assert "[J]" not in out
    assert "(2023)" in out


def test_zh_lang_leaves_gb7714_byte_identical(ref):
    """反向：显式传 `lang="zh"` 与不传是同一个字节 —— 尾参默认值不改既有输出。"""
    assert format_reference(ref, "gb7714", "bracket", lang="zh") == format_reference(
        ref, "gb7714", "bracket"
    )


# ---------------------------------------------------------------
# 题名的终止符：`?` / `!` / `。` / `？` / `！` 本身就是句末标点，不该再叠一个句点
# ---------------------------------------------------------------
# 这一组是本轮才可能被走到的：APA / MLA 之前是占位实现（作者串原样印出），
# 没人盯着它们的标点。现在它们是真身，题名以问号收尾时那个多出来的句点会印进论文。
@pytest.mark.parametrize("title", [
    "Is AI Creative?",           # 英文问句题名
    "What a Breakthrough!",      # 英文叹句
    "什么是智能？",                # 中文全角问号（只认半角的话这条会印出「？.」）
])
def test_apa_without_author_keeps_one_ending_mark(ref, title):
    """无作者时题名顶作者位、年份紧跟其后：`Is AI Creative? (2023).`。

    这条分支最容易被漏 —— 有作者那条至少还有作者串在前面挡着，读起来不顺眼会有人发现；
    无作者时错误直接落在句首。
    """
    out = format_reference(
        {**ref, "doc_title": title, "authors": ""}, "apa", "bracket"
    )
    assert out == f"[1] {title} (2023). 教育研究.", out


def test_apa_with_author_keeps_one_ending_mark(ref):
    out = format_reference({**ref, "doc_title": "Is AI Creative?"}, "apa", "bracket")
    assert out == "[1] 张三. (2023). Is AI Creative? 教育研究.", out


def test_mla_ending_mark_stays_inside_the_quotes(ref):
    """MLA 的终止符在引号**里面**：`"Is AI Creative?"`，且不再补句点。

    这一处特别容易改错：MLA 的形态是 `"Title."`（句点在引号内），照搬就会得到
    `"Is AI Creative?."` —— 引号里两个句末标点。
    """
    out = format_reference({**ref, "doc_title": "Is AI Creative?"}, "mla", "bracket")
    assert '"Is AI Creative?"' in out, out
    assert '?."' not in out, out


def test_translated_title_still_gets_a_period_after_the_bracket(ref):
    """带英译方括号时结尾是 `]`，照补句点 —— 方括号是插入成分，不是句末。

    所以 `_terminated` 不需要知道方括号的事，这条把它钉住。
    """
    out = format_reference(
        {**ref, "doc_title": "什么是智能？", "title_en": "What is intelligence?"},
        "apa", "bracket",
    )
    assert "什么是智能？ [What is intelligence?]." in out, out


@pytest.mark.parametrize("title", ["Is AI Creative?", "What a Breakthrough!", "什么是智能？"])
def test_gb7714_title_terminator_is_untouched(ref, title):
    """GB/T 7714 这条分支**不做终止符处理**，而且是刻意的：类型标识方括号夹在题名与
    句点之间（`题名?[J].`），任何收尾都不冲突。

    加这条断言是为了拦住「顺手统一」——把三处改成同一个 `_terminated` 会动到中文侧
    逐字节不变的保证，而它看上去只是一次去重。
    """
    out = format_reference({**ref, "doc_title": title}, "gb7714", "bracket")
    assert out == f"[1] 张三. {title}[J]. 教育研究, 2023.", out


# ---------------------------------------------------------------
# 语料表：加斜体之前的输出逐条冻住
# ---------------------------------------------------------------
# 上面那些哨兵一共只有 6 个固定输入 —— **用 6 个点验一条曲线**。这一节的用途是给
# 「format_reference 内部改成产出片段（文本 + 是否斜体）」那次重构做一张网：把
# 13 种条目形状 × 3 种格式 × 2 种角标样式共 78 条的输出逐字节钉住，重构后必须逐条
# 相等。
#
# **它是一张冻结网、不是一个独立的判据**：表里的值是重构前的实现自己跑出来的，
# 所以它在加进来的那一刻必然是绿的（此刻它只证明"我没抄错"）。真正的独立判据是上面
# 那些手写的逐字节断言 —— 表的价值在于覆盖它们够不到的那 70 多条组合。
#
# 表红了怎么办：**问"为什么非动不可"，而不是把新值抄进来** —— 抄一遍等于把网作废。
# 唯一正当的红是"这次重构本来就要改这条输出"，那时改的应该是本文件上面某条手写
# 断言（它描述的是意图），而不是这张表。
_CORPUS = {
    "最小条目": {"num": 1, "doc_title": "题名"},
    "中文期刊全字段": {
        "num": 1, "doc_title": "人工智能在教育中的应用", "authors": "张三",
        "year": "2023", "source": "教育研究", "volume": "32", "issue": "1",
        "pages": "56-75", "source_type": "J",
    },
    "英文期刊全字段": {
        "num": 2, "doc_title": "Title of the article", "authors": "Smith, John K.",
        "year": "2020", "source": "Journal Name", "volume": "32", "issue": "1",
        "pages": "56-75", "source_type": "J",
    },
    "无作者": {
        "num": 3, "doc_title": "Is AI Creative?", "authors": "", "year": "2023",
        "source": "教育研究",
    },
    "无年份": {"num": 4, "doc_title": "题名", "authors": "张三", "source": "教育研究"},
    "英译题名": {
        "num": 5, "doc_title": "什么是智能？", "authors": "李四", "year": "2022",
        "source": "哲学研究", "title_en": "What is intelligence?",
    },
    "刊名缺失有卷期页": {
        "num": 6, "doc_title": "T", "authors": "Smith, J. K.", "year": "2021",
        "source": "", "volume": "32", "issue": "1", "pages": "56-75",
    },
    "专著": {
        "num": 7, "doc_title": "人工智能导论", "authors": "王五", "year": "2020",
        "source": "高等教育出版社", "source_type": "M", "place": "北京",
        "edition": "第3版", "pages": "56-75",
    },
    "论文集析出": {
        "num": 8, "doc_title": "深度学习综述", "authors": "赵六", "year": "2023",
        "source": "第五届全国人工智能会议论文集", "source_type": "C", "place": "北京",
        "pages": "12-20",
    },
    "报纸": {
        "num": 9, "doc_title": "AI 改变教育", "authors": "钱七", "source": "人民日报",
        "source_type": "N", "publish_date": "2023-05-04",
    },
    "占位词": {
        "num": 10, "doc_title": "题名", "authors": "未提及", "year": "Not specified",
        "source": "未知",
    },
    # 下面两条专门压斜体分段的边界：刊名有卷号没有、有卷号没刊名 —— 斜体范围的
    # 起点与终点都在这两种形态上换位置，最容易只对一半。
    "斜体边界有刊名无卷": {
        "num": 11, "doc_title": "T", "authors": "Smith, John K.", "year": "2020",
        "source": "Journal Name", "issue": "1", "pages": "56-75",
    },
    "斜体边界有卷无刊名": {
        "num": 12, "doc_title": "T", "authors": "Smith, John K.", "year": "2020",
        "source": "", "volume": "32", "pages": "56-75",
    },
}

# 语料表：每个形状 6 条 —— 3 种格式 × 2 种角标样式。
# 这批值是**加斜体之前**的实现逐条跑出来的原样输出。
_CORPUS_EXPECTED = (
    # 最小条目
    ('最小条目', 'gb7714', 'bracket', '[1] 题名[J].'),
    ('最小条目', 'gb7714', 'superscript', '1. 题名[J].'),
    ('最小条目', 'apa', 'bracket', '[1] 题名. (n.d.).'),
    ('最小条目', 'apa', 'superscript', '1. 题名. (n.d.).'),
    ('最小条目', 'mla', 'bracket', '[1] "题名."'),
    ('最小条目', 'mla', 'superscript', '1. "题名."'),
    # 中文期刊全字段
    ('中文期刊全字段', 'gb7714', 'bracket', '[1] 张三. 人工智能在教育中的应用[J]. 教育研究, 2023, 32(1): 56-75.'),
    ('中文期刊全字段', 'gb7714', 'superscript', '1. 张三. 人工智能在教育中的应用[J]. 教育研究, 2023, 32(1): 56-75.'),
    ('中文期刊全字段', 'apa', 'bracket', '[1] 张三. (2023). 人工智能在教育中的应用. 教育研究, 32(1), 56-75.'),
    ('中文期刊全字段', 'apa', 'superscript', '1. 张三. (2023). 人工智能在教育中的应用. 教育研究, 32(1), 56-75.'),
    ('中文期刊全字段', 'mla', 'bracket', '[1] 张三. "人工智能在教育中的应用." 教育研究, vol. 32, no. 1, 2023, pp. 56-75.'),
    ('中文期刊全字段', 'mla', 'superscript', '1. 张三. "人工智能在教育中的应用." 教育研究, vol. 32, no. 1, 2023, pp. 56-75.'),
    # 英文期刊全字段
    ('英文期刊全字段', 'gb7714', 'bracket', '[2] SMITH J K. Title of the article[J]. Journal Name, 2020, 32(1): 56-75.'),
    ('英文期刊全字段', 'gb7714', 'superscript', '2. SMITH J K. Title of the article[J]. Journal Name, 2020, 32(1): 56-75.'),
    ('英文期刊全字段', 'apa', 'bracket', '[2] Smith, J. K. (2020). Title of the article. Journal Name, 32(1), 56-75.'),
    ('英文期刊全字段', 'apa', 'superscript', '2. Smith, J. K. (2020). Title of the article. Journal Name, 32(1), 56-75.'),
    ('英文期刊全字段', 'mla', 'bracket', '[2] Smith, John K. "Title of the article." Journal Name, vol. 32, no. 1, 2020, pp. 56-75.'),
    ('英文期刊全字段', 'mla', 'superscript', '2. Smith, John K. "Title of the article." Journal Name, vol. 32, no. 1, 2020, pp. 56-75.'),
    # 无作者
    ('无作者', 'gb7714', 'bracket', '[3] Is AI Creative?[J]. 教育研究, 2023.'),
    ('无作者', 'gb7714', 'superscript', '3. Is AI Creative?[J]. 教育研究, 2023.'),
    ('无作者', 'apa', 'bracket', '[3] Is AI Creative? (2023). 教育研究.'),
    ('无作者', 'apa', 'superscript', '3. Is AI Creative? (2023). 教育研究.'),
    ('无作者', 'mla', 'bracket', '[3] "Is AI Creative?" 教育研究, 2023.'),
    ('无作者', 'mla', 'superscript', '3. "Is AI Creative?" 教育研究, 2023.'),
    # 无年份
    ('无年份', 'gb7714', 'bracket', '[4] 张三. 题名[J]. 教育研究.'),
    ('无年份', 'gb7714', 'superscript', '4. 张三. 题名[J]. 教育研究.'),
    ('无年份', 'apa', 'bracket', '[4] 张三. (n.d.). 题名. 教育研究.'),
    ('无年份', 'apa', 'superscript', '4. 张三. (n.d.). 题名. 教育研究.'),
    ('无年份', 'mla', 'bracket', '[4] 张三. "题名." 教育研究.'),
    ('无年份', 'mla', 'superscript', '4. 张三. "题名." 教育研究.'),
    # 英译题名
    ('英译题名', 'gb7714', 'bracket', '[5] 李四. 什么是智能？[J]. 哲学研究, 2022.'),
    ('英译题名', 'gb7714', 'superscript', '5. 李四. 什么是智能？[J]. 哲学研究, 2022.'),
    ('英译题名', 'apa', 'bracket', '[5] 李四. (2022). 什么是智能？ [What is intelligence?]. 哲学研究.'),
    ('英译题名', 'apa', 'superscript', '5. 李四. (2022). 什么是智能？ [What is intelligence?]. 哲学研究.'),
    ('英译题名', 'mla', 'bracket', '[5] 李四. "什么是智能？ [What is intelligence?]." 哲学研究, 2022.'),
    ('英译题名', 'mla', 'superscript', '5. 李四. "什么是智能？ [What is intelligence?]." 哲学研究, 2022.'),
    # 刊名缺失有卷期页
    ('刊名缺失有卷期页', 'gb7714', 'bracket', '[6] SMITH J K. T[J]. 2021, 32(1): 56-75.'),
    ('刊名缺失有卷期页', 'gb7714', 'superscript', '6. SMITH J K. T[J]. 2021, 32(1): 56-75.'),
    ('刊名缺失有卷期页', 'apa', 'bracket', '[6] Smith, J. K. (2021). T. 32(1), 56-75.'),
    ('刊名缺失有卷期页', 'apa', 'superscript', '6. Smith, J. K. (2021). T. 32(1), 56-75.'),
    ('刊名缺失有卷期页', 'mla', 'bracket', '[6] Smith, J. K. "T." vol. 32, no. 1, 2021, pp. 56-75.'),
    ('刊名缺失有卷期页', 'mla', 'superscript', '6. Smith, J. K. "T." vol. 32, no. 1, 2021, pp. 56-75.'),
    # 专著
    ('专著', 'gb7714', 'bracket', '[7] 王五. 人工智能导论[M]. 第3版. 北京: 高等教育出版社, 2020: 56-75.'),
    ('专著', 'gb7714', 'superscript', '7. 王五. 人工智能导论[M]. 第3版. 北京: 高等教育出版社, 2020: 56-75.'),
    ('专著', 'apa', 'bracket', '[7] 王五. (2020). 人工智能导论. 高等教育出版社, 56-75.'),
    ('专著', 'apa', 'superscript', '7. 王五. (2020). 人工智能导论. 高等教育出版社, 56-75.'),
    ('专著', 'mla', 'bracket', '[7] 王五. "人工智能导论." 高等教育出版社, 2020, pp. 56-75.'),
    ('专著', 'mla', 'superscript', '7. 王五. "人工智能导论." 高等教育出版社, 2020, pp. 56-75.'),
    # 论文集析出
    ('论文集析出', 'gb7714', 'bracket', '[8] 赵六. 深度学习综述[C]//第五届全国人工智能会议论文集. 北京, 2023: 12-20.'),
    ('论文集析出', 'gb7714', 'superscript', '8. 赵六. 深度学习综述[C]//第五届全国人工智能会议论文集. 北京, 2023: 12-20.'),
    ('论文集析出', 'apa', 'bracket', '[8] 赵六. (2023). 深度学习综述. 第五届全国人工智能会议论文集, 12-20.'),
    ('论文集析出', 'apa', 'superscript', '8. 赵六. (2023). 深度学习综述. 第五届全国人工智能会议论文集, 12-20.'),
    ('论文集析出', 'mla', 'bracket', '[8] 赵六. "深度学习综述." 第五届全国人工智能会议论文集, 2023, pp. 12-20.'),
    ('论文集析出', 'mla', 'superscript', '8. 赵六. "深度学习综述." 第五届全国人工智能会议论文集, 2023, pp. 12-20.'),
    # 报纸
    ('报纸', 'gb7714', 'bracket', '[9] 钱七. AI 改变教育[N]. 人民日报, 2023-05-04.'),
    ('报纸', 'gb7714', 'superscript', '9. 钱七. AI 改变教育[N]. 人民日报, 2023-05-04.'),
    ('报纸', 'apa', 'bracket', '[9] 钱七. (n.d.). AI 改变教育. 人民日报.'),
    ('报纸', 'apa', 'superscript', '9. 钱七. (n.d.). AI 改变教育. 人民日报.'),
    ('报纸', 'mla', 'bracket', '[9] 钱七. "AI 改变教育." 人民日报.'),
    ('报纸', 'mla', 'superscript', '9. 钱七. "AI 改变教育." 人民日报.'),
    # 占位词
    ('占位词', 'gb7714', 'bracket', '[10] 题名[J].'),
    ('占位词', 'gb7714', 'superscript', '10. 题名[J].'),
    ('占位词', 'apa', 'bracket', '[10] 题名. (n.d.).'),
    ('占位词', 'apa', 'superscript', '10. 题名. (n.d.).'),
    ('占位词', 'mla', 'bracket', '[10] "题名."'),
    ('占位词', 'mla', 'superscript', '10. "题名."'),
    # 斜体边界有刊名无卷
    ('斜体边界有刊名无卷', 'gb7714', 'bracket', '[11] SMITH J K. T[J]. Journal Name, 2020(1): 56-75.'),
    ('斜体边界有刊名无卷', 'gb7714', 'superscript', '11. SMITH J K. T[J]. Journal Name, 2020(1): 56-75.'),
    ('斜体边界有刊名无卷', 'apa', 'bracket', '[11] Smith, J. K. (2020). T. Journal Name, (1), 56-75.'),
    ('斜体边界有刊名无卷', 'apa', 'superscript', '11. Smith, J. K. (2020). T. Journal Name, (1), 56-75.'),
    ('斜体边界有刊名无卷', 'mla', 'bracket', '[11] Smith, John K. "T." Journal Name, no. 1, 2020, pp. 56-75.'),
    ('斜体边界有刊名无卷', 'mla', 'superscript', '11. Smith, John K. "T." Journal Name, no. 1, 2020, pp. 56-75.'),
    # 斜体边界有卷无刊名
    ('斜体边界有卷无刊名', 'gb7714', 'bracket', '[12] SMITH J K. T[J]. 2020, 32: 56-75.'),
    ('斜体边界有卷无刊名', 'gb7714', 'superscript', '12. SMITH J K. T[J]. 2020, 32: 56-75.'),
    ('斜体边界有卷无刊名', 'apa', 'bracket', '[12] Smith, J. K. (2020). T. 32, 56-75.'),
    ('斜体边界有卷无刊名', 'apa', 'superscript', '12. Smith, J. K. (2020). T. 32, 56-75.'),
    ('斜体边界有卷无刊名', 'mla', 'bracket', '[12] Smith, John K. "T." vol. 32, 2020, pp. 56-75.'),
    ('斜体边界有卷无刊名', 'mla', 'superscript', '12. Smith, John K. "T." vol. 32, 2020, pp. 56-75.'),
)

_CORPUS_BY_CASE = {(n, f, s): out for n, f, s, out in _CORPUS_EXPECTED}
_CORPUS_FORMATS = ("gb7714", "apa", "mla")
_CORPUS_STYLES = ("bracket", "superscript")
# 同一个 case 列表给两条用例用（逐字节那条、以及「文本 == 片段拼接」那条）——
# 各抄一份的话，往语料里加形状时漏改其中一处，那条用例就悄悄只剩一半覆盖。
_CORPUS_CASES = [(n, f, s) for n, f, s, _ in _CORPUS_EXPECTED]


@pytest.mark.parametrize(
    "name, fmt, style", _CORPUS_CASES,
    ids=["%d" % i for i in range(len(_CORPUS_EXPECTED))],
)
def test_corpus_output_is_byte_identical(name, fmt, style):
    """加斜体之后，这 78 条组合的输出必须与加之前逐字节相同。

    断言文案里带着形状名/格式/样式（参数 id 是序号，光看 id 认不出是哪一条），
    这样红的时候能一眼看出改坏的是哪一段分支。
    """
    got = format_reference(dict(_CORPUS[name]), fmt, style)
    assert got == _CORPUS_BY_CASE[(name, fmt, style)], f"{name} / {fmt} / {style}"


def test_corpus_covers_every_shape_in_both_styles():
    """语料表必须把每个形状 × 三种格式 × 两种样式都覆盖到。

    没有这条的话，往 `_CORPUS` 里加一个形状却忘了重跑生成脚本，新形状就一条断言
    都没有 —— 表看起来还是绿的，而它已经不再描述全部语料（本项目"漏一处是静默的"
    那个老形状）。顺带把重复项也拦下：同一组合出现两次，说明生成时漏改了循环。
    """
    covered = [(n, f, s) for n, f, s, _ in _CORPUS_EXPECTED]
    assert len(covered) == len(set(covered)), "语料表里有重复的组合"
    assert set(covered) == {
        (name, fmt, style)
        for name in _CORPUS
        for fmt in _CORPUS_FORMATS
        for style in _CORPUS_STYLES
    }


# ---------------------------------------------------------------
# 斜体片段（docx 用；txt / md 仍然是纯文本）
# ---------------------------------------------------------------
# 这一节锁的是**斜体范围**，不是文字 —— 文字已经由上面那 78 条逐字节钉死了。
# 范围错了的两种典型坏法（整段一起斜体、只斜体了刊名漏掉卷号）**都能过"有斜体"
# 那种弱断言**，所以每条都断言"斜体的恰好是这几段"。
def _italic_spans(ref, fmt, style="bracket"):
    return [r["text"] for r in reference_runs(ref, fmt, style) if r["italic"]]


def test_apa_italic_span_is_the_journal_name_and_volume(ref):
    """APA 7 §9.34：**刊名与卷号**斜体，期号、页码、收尾句点都正体。

    三处边界一起断：斜体的起点（刊名整段）、刊名与卷号**之间那个逗号**（它跟着
    斜体走，印成 `*Journal Name, 32*(1), 56-75.`）、以及终点（卷号之后全正体）。
    """
    out = reference_runs(
        {**ref, "source": "Journal Name", "volume": "32", "issue": "1", "pages": "56-75"},
        "apa",
    )
    assert [r["text"] for r in out if r["italic"]] == ["Journal Name", ", 32"]
    assert [r["text"] for r in out if not r["italic"]][-3:] == ["(1)", ", 56-75", "."]


def test_apa_without_source_italicizes_the_volume_alone(ref):
    """刊名缺失时卷号自己斜体，且**不带前导逗号**（那个逗号是刊名与卷号的连接符）。"""
    out = reference_runs({**ref, "source": "", "volume": "32", "pages": "56-75"}, "apa")
    assert [r["text"] for r in out if r["italic"]] == ["32"]


def test_apa_without_volume_italicizes_only_the_journal_name(ref):
    """没有卷号时斜体到刊名为止 —— **期号与页码不跟着斜体**。

    与上一条成对：刊名/卷号之间那个逗号属于斜体，卷号/期号之间那个**不属于**。
    两处逗号长得一样、归属相反，一条"把刊源段整段斜体"的实现会同时蒙对这两条 ——
    所以两条都要有。
    """
    out = reference_runs(
        {**ref, "source": "Journal Name", "issue": "1", "pages": "56-75"}, "apa"
    )
    assert [r["text"] for r in out if r["italic"]] == ["Journal Name"]


def test_apa_source_segment_of_pages_only_has_no_italic_at_all(ref):
    """刊源段只剩页码时一个斜体片段都没有（不是"斜体了一段空的"）。

    只剩页码这条分支最容易漏：斜体的两个来源（刊名、卷号）都不在，`_apa_tail`
    里那段 `f", {pages}" if runs else pages` 的 `else` 就是它 —— 此时连那个逗号
    都不该出现（`56-75.`，不是 `, 56-75.`），语料表里那条逐字节钉着。
    """
    out = reference_runs(
        {**ref, "source": "", "volume": "", "issue": "", "pages": "56-75"}, "apa"
    )
    assert [r["text"] for r in out if r["italic"]] == []
    assert "".join(r["text"] for r in out).endswith("56-75.")


def test_mla_italicizes_the_container_only(ref):
    """MLA 9：**容器名**斜体，卷/期/年/页与段间的 `, ` 全正体。

    与 APA 的差别值得盯住：APA 的斜体是"刊名 + 逗号 + 卷号"，MLA 的只有容器名
    一段。照抄 APA 的切法会多斜体一个 `, vol. 32`。
    """
    out = reference_runs(
        {**ref, "source": "Journal Name", "volume": "32", "issue": "1", "pages": "56-75"},
        "mla",
    )
    assert [r["text"] for r in out if r["italic"]] == ["Journal Name"]


def test_mla_without_container_has_no_italic_at_all(ref):
    """容器名缺失 → 一个斜体片段都没有（`vol. 32` 不顶上斜体位）。"""
    out = reference_runs({**ref, "source": "", "volume": "32", "year": "2020"}, "mla")
    assert [r["text"] for r in out if r["italic"]] == []
    assert "vol. 32" in "".join(r["text"] for r in out)


@pytest.mark.parametrize("source_type", ["J", "M", "D", "C", "N", "R"])
def test_gb7714_never_italicizes(ref, source_type):
    """GB/T 7714 一律正体 —— 期刊、本轮新建的 `[M]/[D]/[C]/[N]` 模板、兜底类型都一样。

    这不是"漏做"：GB/T 用类型标识方括号区分文献类型，**不用斜体**。四类模板的
    专属字段全填满也不该冒出一个斜体片段（模块 docstring 末条记着这件事）。
    """
    out = reference_runs(
        {**ref, "source_type": source_type, "place": "北京", "edition": "第3版",
         "publish_date": "2023-05-04", "source": "人民日报"},
        "gb7714",
    )
    assert [r["text"] for r in out if r["italic"]] == []


@pytest.mark.parametrize(
    "name, fmt, style", _CORPUS_CASES,
    ids=["%d" % i for i in range(len(_CORPUS_EXPECTED))],
)
def test_runs_text_is_exactly_the_rendered_string(name, fmt, style):
    """`format_reference` 的输出**等于** `reference_runs` 各段文本的拼接（78 条）。

    这条把两者之间那个"定义"关系钉住：斜体范围可以随便调，但**改动著录串本身**
    必须同时经过这两条投影。将来若有人为了"省一次分配"再写一条只产文本的快路径，
    这里会红 —— 那正是"同一件事两份实现"的开端。

    它**此刻是构造上恒真的**（后者就是这么实现的），所以它不是独立判据；独立判据
    是上面那些逐字节与斜体范围的手写断言。顺带钉住两件前端依赖的形状：没有空片段
    （docx 会为每一段发一个 `<w:r>`），片段只有 text / italic 两个键。
    """
    ref = dict(_CORPUS[name])
    runs = reference_runs(ref, fmt, style)
    assert "".join(r["text"] for r in runs) == format_reference(ref, fmt, style)
    assert all(r["text"] for r in runs), "没有空片段"
    assert all(set(r) == {"text", "italic"} for r in runs), "片段只有这两个键"
