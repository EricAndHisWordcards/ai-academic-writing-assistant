"""GB/T 7714-2015 里**期刊以外**那几类的著录模板。

本轮之前，`citation_format._gb7714` 对 `[M]` / `[D]` / `[C]` / `[N]` 没有各自的
模板 —— 它们与期刊共用同一条兜底路径（只把自己那个类型字母印出来）。那是一条
**有意的取舍**，理由写在 `citation_format` 的模块 docstring 里；本轮把它推翻了，
因为「在一本专著上印 `刊名, 年, 卷(期): 页.`」这种形态对用户是实实在在的损失。

**为什么单独一个模块，而不是写进 `_gb7714` 里面**：那条期刊路径是冻结的 ——
六条逐字节哨兵盯着它，而库里绝大多数条目走它。把新模板写在同一个函数里，下一个人
「顺手统一一下标点」就会动到它。这里是与它物理隔开的一块：`render()` 返回 `None`
就表示「这一条不归我管」，调用方原样走兜底，两边的代码通路不重叠。

三条硬约定，改这个模块之前先读：

1. **触发条件只读本类型专属的新列，绝不读 `source`。** 这是本模块最重要的一条。
   存量里的 `[C]` / `[N]` 条目 `source` 基本都是非空的（提示词一直要求填
   「期刊/会议/来源」）—— 把它算进触发条件，它们会从 `论文集名, 2023.` 变成
   `论文集名. 2023.` 集体走形，而「新字段全空 → 输出逐字节不变」正是这一轮不动
   存量项目的全部依据。`_TRIGGERS` 里一个 `source` 都没有，那是有意的。
2. **`source` 在新形态里当载体用**：出版者（`[M]`）、学位授予单位（`[D]`）、
   论文集名（`[C]`）、报纸名（`[N]`）。**不为它们新增同义列** —— 提示词里
   `source` 的写法就是「期刊/会议/来源」，前端标签是「来源（刊名 / 出版社 /
   学位授予单位 / 论文集名 / 报纸名）」，它本来就是这几样东西。另开一列的后果是：
   模型把出版社抽进 `source`、新列恒空 → 触发条件不成立 → 模板上线了等于没上线。
3. **缺项逐级降级，不能印出一个空的字段位。** 形态见各模板的 docstring。

**已知偏离，如实记档**（与「不做 `[J/OL]`」同一体例：没有那个字段就不印，宁可
如实少印，也不印一个猜出来的）：

* `[N]` 不印版次（GB/T 作 `报纸名, 出版日期(版次).`）；
* `[C]` 不印论文集主要责任者（GB/T 作 `析出题名[C]//责任者. 论文集名. …`）。

`__init__` 与 `render` 的入参里有 `title` 与 `pages` 而不是让本模块自己去取：
`citation_format._title_of` 负责题名与英译方括号、`normalize_pages` 负责页码形态，
两者都是三个格式共用的件。**本模块刻意不 import `citation_format`** —— 那会构成
循环导入（对方要 import 本模块）。所以那两样由调用方算好传进来。
"""
from __future__ import annotations

import re

from app.metadata import clean_meta_value

# 每种类型的**触发字段**：只有这些列至少一个非空，才走本类型的新模板。
#
# 三条要点，每一条都对应一种会被写错的方式：
# ① `source` 不在任何一项里（见模块 docstring 第 1 条）；
# ② 是**按类型分开**的字段集，不是五列的并集 —— 模型会在每篇文献上都试着填新字段，
#    取并集的话，一条专著被填了报纸日期就会掉进报纸的模板；
# ③ 只收本轮新增的三列。往后要加触发项，先问它是不是"没有它这个模板就印不出
#    标准形态"，而不是"填了它更好看"。
_TRIGGERS = {
    "M": ("place", "edition"),
    "D": ("place",),
    "C": ("place",),
    "N": ("publish_date",),
}


def normalize_publish_date(raw) -> str:
    """归一化报纸出版日期成 GB/T 的 `YYYY-MM-DD`。

    接受 `2023-05-04` / `2023/5/4` / `2023.5.4` / `2023年5月4日`（月份与日补零）。

    **认不出的形态原样返回**，与 `normalize_pages` 同一条纪律：只归一化，不猜测。
    特别地不把 `2023` 补成一个假日期 —— 4 位年份是已知的、日级精度是未知的，
    而偏差分毫的日期比一个不完整的日期错得多。`2023-13-45` 这种形态对上但数值非法的
    也不改写：那说明抽取或手填出了别的问题，抹平它反而让用户看不见。

    归一化点放在**渲染侧**（与 `normalize_pages` 一样，在渲染时调用），于是它同时
    覆盖两条写入路径：解析落库那条走 `metadata.clean_reference_fields`，而编辑接口
    那条不过那个函数（用户手填的「2023年5月4日」也要印成 `2023-05-04`）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    m = re.fullmatch(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", text)
    if m is None:
        return text
    year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{year}-{month:02d}-{day:02d}"


def _text(value) -> str:
    """取一个字段并清掉占位词。

    与 `citation_format._title_of` 对 `title_en` 的做法同源：**不能只靠写入侧的
    `clean_reference_fields`** —— 编辑接口那条路径不过那个函数，用户手填的「未提及」
    会原样印进参考文献。判定点仍只有 `metadata.clean_meta_value` 一处。
    """
    return clean_meta_value(value)


def _marker(title: str, source_type: str, container: str = "") -> str:
    """`题名[X].`；给了 `container` 就是 `[C]` 那种析出形态：`题名[X]//论文集名.`

    `//` 紧贴类型标识、前后不留空格，而**句点落在论文集名之后**（不是 `[C]` 之后）——
    这是 GB/T 区分「析出文献」的写法。所以 `[C]` 不能简单拼两段（`[C].` + `论文集名`），
    它那条的收尾必须由这里一起给出。
    """
    head = f"{title}[{source_type}]"
    if container:
        head += f"//{container}"
    return head + "."


def _imprint(place: str, publisher: str, year: str, pages: str = "") -> str:
    """出版项：`出版地: 出版者, 出版年: 引文页码.`，缺项逐级降级。

    逗号只用在一个地方（出版年之前），冒号只用在一个地方（页码之前）——
    有的都印、没有的都省。**缺出版者时冒号跟着一起省**（`北京, 2020.` 而不是
    `北京: , 2020.`），这是这个函数存在的理由：三个模板共用一条降级梯子，
    各写一份迟早漂移。

    全部缺项时返回空串，由调用方决定不追加这一段（与 `_journal_segment` 的
    「收尾成空串」约定同型）。
    """
    seg = ""
    if place and publisher:
        seg = f"{place}: {publisher}"
    elif place:
        seg = place
    elif publisher:
        seg = publisher
    if year:
        seg = f"{seg}, {year}" if seg else year
    if pages:
        seg = f"{seg}: {pages}" if seg else pages
    return f"{seg}." if seg else ""


def _monograph(ref: dict, title: str, source_type: str, pages: str) -> str:
    """`[M]` 专著：`主要责任者. 题名[M]. 版本项. 出版地: 出版者, 出版年: 引文页码.`

    版本项自带一个句点，与题名那一段并列（GB/T 里它是一个独立的著录项）。
    """
    parts = [_marker(title, source_type)]
    edition = _text(ref.get("edition"))
    if edition:
        parts.append(f"{edition}.")
    tail = _imprint(_text(ref.get("place")), _text(ref.get("source")),
                    _text(ref.get("year")), pages)
    if tail:
        parts.append(tail)
    return " ".join(parts)


def _dissertation(ref: dict, title: str, source_type: str, pages: str) -> str:
    """`[D]` 学位论文：`主要责任者. 题名[D]. 出版地: 学位授予单位, 出版年.`

    **不接收 pages** —— 学位论文没有「起止页码」这一项（参数留着只为三个模板同形）。
    培养单位取 `source`。
    """
    parts = [_marker(title, source_type)]
    tail = _imprint(_text(ref.get("place")), _text(ref.get("source")),
                    _text(ref.get("year")))
    if tail:
        parts.append(tail)
    return " ".join(parts)


def _conference_paper(ref: dict, title: str, source_type: str, pages: str) -> str:
    """`[C]` 论文集析出：`析出题名[C]//论文集名. 出版地, 出版年: 引文页码.`

    `//` 与句点的位置由 `_marker` 一起给出（见它的 docstring）。
    论文集名取 `source`；出版者这一项没有字段可装（`source` 已经用于论文集名），
    所以出版项走 `_imprint` 的「只有出版地」那一支。
    """
    head = _marker(title, source_type, _text(ref.get("source")))
    parts = [head]
    tail = _imprint(_text(ref.get("place")), "", _text(ref.get("year")), pages)
    if tail:
        parts.append(tail)
    return " ".join(parts)


def _newspaper(ref: dict, title: str, source_type: str, pages: str) -> str:
    """`[N]` 报纸：`主要责任者. 题名[N]. 报纸名, 出版日期.`

    日期是**日**级精度，而 `year` 那一列装不下（提示词要求它只填 4 位年份），所以
    单独一列 `publish_date`。报纸名取 `source`。版次不印（见模块 docstring）。
    """
    head = _marker(title, source_type)
    source = _text(ref.get("source"))
    date = normalize_publish_date(ref.get("publish_date"))
    seg = f"{source}, {date}" if source and date else (source or date)
    return f"{head} {seg}." if seg else head


_BODIES = {
    "M": _monograph,
    "D": _dissertation,
    "C": _conference_paper,
    "N": _newspaper,
}


def render(ref: dict, title: str, source_type: str, pages: str) -> str | None:
    """按文献类型渲染正文主体；**类型没有专属模板、或本类型的新字段全空时返回 None**。

    返回的是「题名那一段 + 出版信息那一段」的完整主体（不含作者、不含角标前缀）——
    之所以连题名一起返回，是因为 `[C]` 的 `//` 必须紧贴类型标识、中间不能插空格，
    分段返回就没法表达这件事。

    返回 None 有三种情况，调用方一律走兜底：类型不在 `_TRIGGERS` 里（期刊与 R/S/P/G/Z）、
    本类型专属的新列全空（**全部存量条目都在这一支**）、以及类型标识被 `clean_source_type`
    收敛过（越界的字母不会在这里被当成某一类）。
    """
    fields = _TRIGGERS.get(source_type)
    if not fields:
        return None
    if not any(_text(ref.get(f)) for f in fields):
        return None
    return _BODIES[source_type](ref, title, source_type, pages)
