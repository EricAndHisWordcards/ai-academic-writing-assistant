"""参考文献列表格式化：支持 GB/T 7714 / APA / MLA 三种标准。

输入文献条目字段：num（序号）、doc_title（题名）、title_en（题名英译）、
authors（作者）、year（年份）、source（来源/期刊）、volume（卷）、issue（期）、
pages（页码）、source_type（文献类型标识）。

**三种格式各自规范化著者姓名，做不成一份共用的输出**：GB/T 要 `SMITH J K`
（姓全大写、名缩写不加点），APA 7 要 `Smith, J. K., & Doe, A.`（姓前名后、
缩写带点、末位用 `&`），MLA 9 要 `Smith, John K., and Alice Doe.`（名全写）。
切分姓名的那一层是共用的（见 app.names），渲染形态分三份。

**哪些格式在哪种语言下可用**由本模块决定（`citation_formats_for`）：英文论文
不该出现 GB/T 7714。`format_reference` 内部走 `effective_format` 收敛，所以
「库里存着 en + gb7714」这种脏状态渲染出来也只会是 APA —— 它不可能印出一份
格式错了的列表。

两条**既知取舍**，写在这里免得被当成漏了：

* **序号与顺序保留**。APA / MLA 的列表本应字母序且不编号，但本项目正文里的角标
  `[n]` 是生成时**烘死**在正文文本里的，只改列表形态等于让正文与列表对不上。
  所以列表向 APA/MLA 的姓名与字段形态靠，编号与顺序照旧。这是**有意的偏离**。
* **不做斜体**。APA 的刊名/卷号、MLA 的容器名本该斜体，而 txt / md 是纯文本、
  docx 那一条路径也按整段渲染（斜体范围要结构化判断）。所以一律正体输出。
  —— 这一条**本轮收窄了**：`txt / md` 那半句仍然成立（纯文本里斜体没有意义，还是
  有意取舍），但 docx 现在**做**斜体。原先"斜体范围要结构化判断"是没做的**理由**，
  本轮就把那个结构化做了：`format_reference` 现在同时产出片段（见
  `reference_runs`），而 `formatted` **定义为**这些片段文本的拼接 —— 斜体是同一份
  渲染的结构化形式，不是另开的第二条渲染线（详见 `_flatten` 与 `Run`）。
  GB/T 7714 一律正体：该标准用类型标识方括号而非斜体区分文献类型，不存在"漏做"。
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple

from app import citation_gb_types as gb_types
from app import names, writing_lang
from app.metadata import clean_meta_fields, clean_meta_value
from app.writing_lang import DEFAULT_LANG

# 文献类型标识白名单（GB/T 7714-2015）。**只认单字母**：
# `[J/OL]` 这类电子双标识本轮不做 —— 按标准它必须跟 URL 与引用日期配对，
# 而那两个字段不在本轮范围内，印一个没有 URL 的 `[J/OL]` 比 `[J]` 更不合规。
#
# 这是一份**白名单而不是说明书**：值最终会被直接拼进正文文本（`题名[X]`），
# 而它的来源是模型抽取的输出 —— 不收敛就等于让模型往正文里写任意字符。
SOURCE_TYPES = ("J", "M", "D", "C", "N", "R", "S", "P", "G", "Z")
DEFAULT_SOURCE_TYPE = "J"

# 支持的格式标识
SUPPORTED_FORMATS = ["gb7714", "apa", "mla"]
DEFAULT_FORMAT = "gb7714"

# 格式标识 → 下拉框里显示的名字。与 SUPPORTED_FORMATS 是同一张表的两种投影，由
# /api/meta 下发：前端把 `citation_formats_by_lang` 里那一串映射成选项文字，**自己不
# 另立一份格式清单** —— 抄一份过去，界面上就会多出一个后端会 400 拒掉的选项，而它
# 看起来完全正常。
#
# 「（默认）」这句话**不在标签里**：默认格式随语言变（zh 是 GB/T、en 是 APA），
# 写进标签就会在另一种语言下印着一个错的默认标记 —— 它由前端按
# `default_citation_format_by_lang` 现判。
FORMAT_LABELS = {
    "gb7714": "GB/T 7714",
    "apa": "APA（第 7 版）",
    "mla": "MLA（第 9 版）",
}

# 角标样式白名单。取值就是 `_num_prefix` 认识的那两个（`bracket` = `[1]`、
# `superscript` = `1.`）。它不随语言变 —— 两种语言都用方括号角标。
#
# 之所以要有白名单：`_num_prefix` 对认不出的值**一律当方括号渲染**，于是库里存着
# 「上标」与存着「拼错的样式名」长得一模一样，用户看到的都是一份方括号正文，
# 却不知道自己选的上标没生效。白名单让这种值在写库之前就被拦下。
SUPPORTED_CITE_STYLES = ["bracket", "superscript"]

# 角标样式标识 → 下拉框里显示的名字。与 FORMAT_LABELS 同样由 /api/meta 下发，
# 前端**不另立一份**（那张手写的 `<option>` 本轮已改成从这张表派生）。
#
# 这张表还有第二个用处：那道 400 的文案。此前它直接印内部标识
# （「不支持的角标样式：endnote（可选：bracket、superscript）」），而用户在界面上
# 从来没见过 `bracket` 这三个字 —— 他见过的是「[1] 方括号角标」。**报错时说的名字
# 必须与界面上说的名字是同一个**，否则用户拿着报错文案对不上自己选的那一项。
CITE_STYLE_LABELS = {
    "bracket": "[1] 方括号角标",
    "superscript": "纯数字上标（¹ ² 风格）",
}


def _names(values: list[str], labels: dict[str, str]) -> str:
    """`['gb7714','apa']` → `GB/T 7714（gb7714）、APA（第 7 版）（apa）`。

    **两样都印**。这个串只出现在报错文案里，而报错文案有两位读者：界面上的用户见过的是
    「APA（第 7 版）」这个名字，而直接调接口的人手里只有 `apa` 这个标识 —— 只印前者
    他就无从知道该往请求里填什么，只印后者则用户拿着报错文案对不上自己选过的那一项。
    （先前只印标识，于是那句「不支持的角标样式：…（可选：bracket、superscript）」在界面上
    说的名字与下拉框里的名字不是一个。）

    未登记的标识原样输出、且不加括号：这个函数出现在文案里的场合本来就包括"值不在白名单
    里"，那时它没有标签可印 —— 而报错文案不该因为查不到标签就崩。
    """
    return "、".join(
        f"{labels[v]}（{v}）" if v in labels else v for v in values
    )


def format_names(formats: list[str]) -> str:
    """格式标识列表的报错用名，见 `_names`。"""
    return _names(formats, FORMAT_LABELS)


def cite_style_names(styles: list[str]) -> str:
    """角标样式版本，理由同 `format_names`。"""
    return _names(styles, CITE_STYLE_LABELS)

# 语言 → 该语言下**符合习惯**的著录格式。这是「选英文就不该能选 GB」这条要求的
# **唯一数据源**：界面的下拉项（经 /api/meta 下发）与后端那道 400 都从这里取。
_FORMATS_BY_LANG = {
    "zh": ["gb7714", "apa", "mla"],
    "en": ["apa", "mla"],
}
# 每种语言的默认格式：新建项目的初值与「当前格式在新语言下不合法」时的落点。
# zh 的默认就是建表那列 DEFAULT 'gb7714'，两处同值。
_DEFAULT_FORMAT_BY_LANG = {"zh": DEFAULT_FORMAT, "en": "apa"}

# 中文写作下 APA / MLA 仍然可选（作者拍板「三项都在」），但中文期刊极少用它们 ——
# 界面上要如实说一句，而不是让用户以为三者等价。**是提示不是拦**：不报错、不置灰。
#
# 键可以指向**该语言下选不到**的格式（`en` + `gb7714`）：用途正是解释它为什么不在
# 下拉框里。后端原样下发全表，由前端挑「本语言被排除了哪几个」再取说明 —— 于是那句
# 「英文论文不用 GB/T 7714」是后端说的，不是前端手写的第二份判据（抄一份过去的话，
# 后端改表之后那句话就变成假话，而它看起来完全正常）。
_FORMAT_NOTES_BY_LANG = {
    "zh": {
        "apa": "APA 第 7 版，英文学术写作的常用格式；中文期刊极少使用。",
        "mla": "MLA 第 9 版，主要用于英文人文学科；中文期刊极少使用。",
    },
    "en": {
        "gb7714": "GB/T 7714 是中文期刊的著录标准，英文论文不用它。",
    },
}


def citation_formats_for(lang: Any) -> list[str]:
    """该语言下可以选的著录格式，顺序即下拉框顺序。"""
    return list(_FORMATS_BY_LANG[writing_lang.normalize(lang)])


def default_format_for(lang: Any) -> str:
    return _DEFAULT_FORMAT_BY_LANG[writing_lang.normalize(lang)]


def effective_format(lang: Any, fmt: Any) -> str:
    """把「语言 + 格式」收敛成一个一定合法的格式，永不抛异常。

    **这是格式解析的唯一入口**，必须被四处共用：渲染（`format_reference`）、启动时的
    快照重算（`db._migrate_reference_snapshots`）、写状态的闸门（`confirm_citations`
    的 400 判据）、以及 `/api/meta` 的默认值。漏掉任何一处，「这一轮会写出的文末列表」
    与「重算出来的」就可能不是同一个字符串 —— `citations_stale` 会常年亮着，而用户
    按了重排也修不好（这正是上一轮反复踩过的形状）。

    与 `confirm_citations` 的分工：那里**先校验再 400**（用户显式提交的越界格式要说
    清原因），这里只是**兜底收敛**（脏数据、改了语言没跟着改格式的存量项目）。两处都
    走这一份映射，不各写一份判据。
    """
    name = str(fmt or "").strip()
    return name if name in citation_formats_for(lang) else default_format_for(lang)


def format_note(lang: Any, fmt: Any) -> str:
    """该语言下这个格式的一句说明；没有要说的就是空串。

    键集是**全部格式**、不是一个语言的子集：`en` + `gb7714` 这种「该语言下选不到」的
    组合也要取得到说明，因为下拉框旁边那句"它为什么不在这儿"就是它。
    """
    notes = _FORMAT_NOTES_BY_LANG.get(writing_lang.normalize(lang)) or {}
    return notes.get(str(fmt or "").strip(), "")


def clean_source_type(value: Any) -> str:
    """把任意的类型标识收敛到白名单内，越界一律回落 `J`。

    用于**渲染侧**（值可能来自模型输出，脏值必须静默收敛，不能把整条参考文献弄坏）。
    用户在编辑界面手填的越界值不走这里 —— 那是显式输入，应当报 400 而不是被悄悄改掉。
    """
    text = str(value or "").strip().upper()
    return text if text in SOURCE_TYPES else DEFAULT_SOURCE_TYPE


def normalize_pages(raw: Any) -> str:
    """归一化起止页码：去 `p.` / `pp.` 前缀、去空白、各种连接号统一成半角 `-`。

    只归一化，不猜测：认不出的形态（如电子刊的 `e12345`）原样返回。
    页码在英文 PDF 里常印成 en dash（`56–75`），而 GB/T 用半角连字符。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^pp?\.?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[-‐-―−~－～]\s*", "-", text)
    return text.strip()


class Run(NamedTuple):
    """著录串里一段连续的文字，带它是否斜体（`Run` = 排版意义上的「一个字块」）。

    斜体是**渲染属性、不是文本**：同一份著录串交给 txt / md 就是纯文本、交给 docx
    才有斜体可言。所以「文字」与「斜体范围」必须一起产出，且**只有一处产出** ——
    见 `reference_runs` 与 `format_reference` 的关系（后者是前者各段文本的拼接）。
    """
    text: str
    italic: bool = False


# 段与段之间的那个空格。它是一段**正体**文字，与任何一段的内容无关。
_PLAIN_SPACE = Run(" ")


def _runs_joined(runs: list[Run], sep: str) -> list[Run]:
    """用 `sep`（正体）把若干片段连成一串 —— 等价于对它们的文本做 `sep.join`。"""
    out: list[Run] = []
    for i, r in enumerate(runs):
        if i:
            out.append(Run(sep))
        out.append(r)
    return out


def _flatten(parts: list) -> list[Run]:
    """把「片段列表」压平成 Run 序列：段与段之间插一个正体空格。

    一个「片段」要么是 `str`（整段正体），要么本身就是一个 `Run` 列表（段内自己
    安排斜体范围）。

    **那个空格是这条改动的保真关键**：它让下面 `format_reference` 的
    `"".join(各段文本)` 与改动前的 `" ".join(parts)` 逐字节相等 —— 改动前 parts
    里全是字符串，本次只是把其中几段换成了带斜体标记的列表，**分段与拼接的规则
    一个字没动**。

    空片段不在这里过滤（`" ".join` 也不过滤）：过滤会悄悄把 `a` + 空 + `b` 之间的
    两个空格收成一个，那正是"顺手改好了"式的静默改字。语料表里有一条断言钉着
    「没有任何一段是空的」，所以这条纪律不会变成盲区。
    """
    out: list[Run] = []
    for i, part in enumerate(parts):
        if i:
            out.append(_PLAIN_SPACE)
        if isinstance(part, str):
            out.append(Run(part))
        else:
            out.extend(part)
    return out


def _journal_segment(source: str, year: str, volume: str, issue: str, pages: str) -> str:
    """拼期刊条目的「刊名, 年, 卷(期): 起止页.」那一段，缺项逐级降级。

    完整形态是 `刊名, 年, 卷(期): 起止页码.`，缺哪项省哪项，但有两条形态约定：
    无卷有期时期**直接贴在年后面不加逗号**（`教育研究, 2023(1): 56-75.`）；
    无卷无期时页码用冒号直接接（`教育研究, 2023: 56-75.`）。

    **降级结果必须与加卷期页之前的旧输出逐字节一致** —— 库里绝大多数老条目没有
    卷期页，它们不该被这次改动改写。`tests/test_db.py` 里三条逐字节断言就是这条
    约束的哨兵：改这个函数时那三条必须仍然绿。
    """
    seg = source
    if year:
        seg = f"{seg}, {year}" if seg else year
    if volume:
        seg = f"{seg}, {volume}" if seg else volume
        if issue:
            seg += f"({issue})"
    elif issue:
        seg += f"({issue})"
    if pages:
        seg = f"{seg}: {pages}" if seg else pages
    return f"{seg}." if seg else ""


def _parts(ref: dict, fmt: str, cite_style: str, lang: Any) -> list:
    """一条文献 → 片段列表（`str` 或 `Run` 列表，见 `_flatten`）。

    **这是唯一的渲染实现**：`format_reference`（纯文本）与 `reference_runs`
    （带斜体）都从它派生，所以"正文里印出来的字"与"docx 里那个斜体范围"不可能
    对不上 —— 它们描述的是同一串片段。
    """
    # 进具体格式之前先清洗作者/年份/来源：这是**用户看到文字之前的最后一道闸**，
    # 不该替上游担保「这三个字段一定是干净的真实值」——模型在文献里找不到作者时
    # 写的「未提及」「Not specified」一旦被当成人名印出来，就是
    # 「[1] 未提及. 题名[J]. 来源, 未提及.」这种一眼假的条目。
    ref = clean_meta_fields(dict(ref))
    # 语言与格式的组合在这里收敛一次：库里存着 en + gb7714（改过语言但没跟着改格式
    # 的存量项目、或手改过的库）时，渲染出来也只可能是 APA —— 印一份格式错了的
    # 列表，比回落到该语言的默认格式坏得多。
    resolved = effective_format(lang, fmt)
    prefix = _num_prefix(ref.get("num", 0), cite_style)
    if resolved == "apa":
        body = _apa(ref)
    elif resolved == "mla":
        body = _mla(ref)
    else:  # 默认 gb7714
        body = _gb7714(ref)
    return [prefix, body]


def reference_runs(
    ref: dict, fmt: str, cite_style: str = "bracket", lang: Any = DEFAULT_LANG
) -> list[dict]:
    """一条文献 → 片段列表 `[{"text": ..., "italic": ...}]`。

    **只在响应里现算，不落库**：`references_json` 里仍然只有 `formatted` 与那十来个
    元字段（库形状一改，快照比对、`_refs_key`、存量迁移全都要跟着动，而它们比的全是
    `formatted`）。挂载点见 `projects._with_reference_runs`。

    与 `format_reference` 的关系是这条改动的全部要害：
    **后者的输出定义为前者的各段文本拼接**。于是"同一份著录串的两种投影"由构造保证
    一致，而不是靠两处实现互相记得改 —— 后者正是本项目反复踩过的形状。
    """
    return [
        {"text": r.text, "italic": r.italic}
        for r in _flatten(_parts(ref, fmt, cite_style, lang))
    ]


def format_reference(
    ref: dict, fmt: str, cite_style: str = "bracket", lang: Any = DEFAULT_LANG
) -> str:
    """按指定格式与角标样式格式化单条参考文献（纯文本形态）。

    cite_style 决定序号前缀：bracket -> [1]；superscript -> 1.
    `lang` 是**项目写作语言**，只用来解析格式是否合法（见 `effective_format`）——
    它不进任何一条分支：三种格式的形态差异全在文本里，与写作语言无关。

    输出**定义为 `reference_runs` 各段文本的拼接**（见那个函数）。所以这里没有
    自己的拼装逻辑：斜体范围改了、片段顺序改了，这一段文本自动跟着改。

    改动前这里是 `f"{prefix} {body}".strip()`。那个 `.strip()` 是死代码，去掉它
    不改变任何一条输出：`body` 永远非空（三种格式都无条件地把题名放进 parts，而
    `_title_of` 有「（未命名文献）」这个非空兜底），两端也不会有空格。这一点由
    `tests/test_citation_format.py` 末尾那张 78 条的语料表逐字节钉住 —— 不是靠
    这几句话担保。
    """
    return "".join(r["text"] for r in reference_runs(ref, fmt, cite_style, lang))


def _num_prefix(num: int, cite_style: str) -> str:
    if cite_style == "superscript":
        return f"{num}."
    return f"[{num}]"


def _as_sentence(name: str) -> str:
    """给作者串补句点，但先剥掉它自带的尾部句点。

    `Smith J.` 这类缩写名自带句点，直接 f"{authors}." 会印成「Smith J..」。
    """
    return name.rstrip(".") + "."


# 本身就是句末标点的收尾字符：中文题名用全角，英文题名用半角，两边都要认 ——
# 只认半角的话，`什么是智能？` 会印成 `什么是智能？.`，与 `Is AI Creative?.` 是同一个
# 缺陷的两种写法。`。` 也在内（中文题名偶尔以句号收尾）。
_SENTENCE_ENDINGS = (".", "?", "!", "。", "？", "！")


def _terminated(text: str) -> str:
    """给题名这类「一句式」片段收尾；已经是终止符就原样返回。

    与 `_as_sentence` 的分工：那个是给**作者串**用的，只剥句点（缩写名自带的点），
    因为人名不可能以问号收尾；题名可以 —— 无条件补句点会把 `Is AI Creative?` 印成
    `Is AI Creative?.`，把 `什么是智能？` 印成 `什么是智能？.`。

    带英译方括号时（`原文题名 [English translation]`）结尾是 `]`，照补句点：
    方括号是插入成分，不是句末。所以这里不需要知道方括号的事。
    """
    if text.endswith(_SENTENCE_ENDINGS):
        return text
    return text + "."


def _gb7714(ref: dict) -> list[Run]:
    """GB/T 7714-2015 顺序编码制（期刊完整著录，非期刊类型走各自的模板）。

    期刊格式：主要责任者. 题名[文献类型标识]. 刊名, 年, 卷(期): 起止页码.
    无责任者（作者缺失）时题名打头，符合该标准惯例。

    非期刊类型（`[M]` / `[D]` / `[C]` / `[N]`）本轮**有了各自的完整模板**，见
    `app.citation_gb_types`（那个模块记着三条硬约定与两条已知偏离）。触发条件是
    「本类型专属的新列（place / edition / publish_date）至少一个非空」——而全部存量
    条目这三列都是空的，所以它们原样走出下面这条兜底路径，**逐字节不变**。
    其余类型（`R` / `S` / `P` / `G` / `Z`）仍只有兜底路径，但照样印**自己**的类型
    标识字母：在一本专著上印 `[J]` 是明知而印错。
    """
    # 姓名规范化只在这里做，见模块 docstring。空串进空串出，作者段随之整段不输出。
    authors = names.normalize_authors(ref.get("authors") or "")
    title = _title_of(ref)
    source_type = clean_source_type(ref.get("source_type"))
    # 页码只归一化一次，两个分支共用：兜底与类型模板对页码的要求是同一个（都是
    # normalize_pages 那个共用件），各算一遍迟早会漂。
    pages = normalize_pages(ref.get("pages"))

    parts = []
    if authors:
        parts.append(_as_sentence(authors))
    # 类型模板返回的是「题名段 + 出版信息段」的**完整主体**，而不只是出版信息 ——
    # `[C]` 的 `//` 必须紧贴类型标识、中间不能插空格，分段返回就没法表达。
    # 返回 None 一律表示「这一条不归它管」：期刊、R/S/P/G/Z、以及新列全空的存量条目。
    body = gb_types.render(ref, title, source_type, pages)
    if body is None:
        # 这条**不用 `_terminated`**（不是漏了）：类型标识方括号夹在题名与句点之间，
        # `题名?[J].` 本来就无歧义，任何收尾都不冲突。别为了「统一」把它改掉 ——
        # 改了会动到中文侧逐字节不变的保证。
        parts.append(f"{title}[{source_type}].")
        seg = _journal_segment(
            ref.get("source") or "",
            ref.get("year") or "",
            ref.get("volume") or "",
            ref.get("issue") or "",
            pages,
        )
        if seg:
            parts.append(seg)
    else:
        parts.append(body)
    # GB/T 7714 一律正体：该标准用类型标识方括号区分文献类型，**不用斜体**
    # （模块 docstring 末条记着这件事）。所以这里的分段全由正体片段构成。
    return _flatten(parts)


def _title_of(ref: dict, with_translation: bool = False) -> str:
    """题名；`with_translation` 时给非英语文献的题名接一个方括号英译。

    APA 7 §9.38 / MLA 9 对非英语文献的要求是**原文题名在前、英译在后方括号里**。
    英译来自抽取时就存下的 `title_en`（见 parse_agent 的提示词）。存量文献这一列
    是空的 —— 没法凭空翻译 —— 那种条目照旧只印原文题名：少一个方括号，好过印一个
    猜出来的译名。GB/T 不读它（那条分支不传 with_translation）。
    """
    title = ref.get("doc_title") or "（未命名文献）"
    if not with_translation:
        return title
    # title_en 不在 META_FIELDS 里，clean_meta_fields 不管它，这里单独清洗一遍
    # （「未提及」当译名印进方括号，与当作者名印出来一样假）。
    translated = clean_meta_value(ref.get("title_en"))
    if not translated or translated == title:
        return title
    return f"{title} [{translated}]"


def _apa(ref: dict) -> list[Run]:
    """APA 第 7 版（期刊条目）。

    形态：`Smith, J. K., & Doe, A. (2020). Title. Journal, 32(1), 56-75.`
    著者形态由 `names.normalize_authors_apa` 负责（倒装、缩写带点、末位 `&`、
    20/21 人的分界）。两条降级规则按 APA 规范：**无作者时题名顶上作者位**
    （作者位空缺由题名补），年份缺失写 `(n.d.)` 而不是省掉括号。
    """
    authors = names.normalize_authors_apa(ref.get("authors") or "")
    year = ref.get("year") or ""
    title = _title_of(ref, with_translation=True)

    parts = []
    if authors:
        # 有作者：作者 → (年份) → 题名。`_as_sentence` 给作者串补句点：缩写名自带
        # 句点（`Smith, J. K.`），直接再接一个就成了 `K..`。
        parts.append(_as_sentence(authors))
        parts.append(f"({year or 'n.d.'}).")
        parts.append(_terminated(title))
    else:
        # 无作者：**题名顶上作者位**，年份跟着题名（APA 第 7 版的规定）
        parts.append(_terminated(title))
        parts.append(f"({year or 'n.d.'}).")
    tail = _apa_tail(ref)
    if tail:
        parts.append(tail)
    return _flatten(parts)


def _apa_tail(ref: dict) -> list[Run]:
    """APA 的刊源段：`Journal, 32(1), 56-75.`，缺项逐级降级。

    与 GB/T 的 `_journal_segment` 同一套降级思路、不同标点（APA 全用逗号，卷期用
    括号而非冒号，页码不写 `pp.`）。

    **刊名缺失时不再把卷期页一起丢掉**：抽不到刊名是常有的事，而卷期页恰好抽到了
    （索引页上与刊名分行印着）—— 丢掉的话读者连「这是第几卷第几期」都看不到。

    返回片段而非字符串，只为一件事：**APA 7 §9.34 的斜体范围** —— 刊名与卷号斜体，
    期号与页码正体。两处边界要一起看：刊名与卷号之间那个逗号**跟着斜体走**
    （`*Journal Name, 32*(1), 56-75.`）；而没有刊名时卷号自己斜体、不留前导逗号，
    收尾句点也单独一段、正体（斜体范围到卷号为止）。

    "缺项逐级降级"的判据换成了 `runs` 是否为空 —— 与改动前看 `seg` 是否为空等价
    （每一条 append 都恰好对应改动前的一次赋值）。这 12 种缺项组合在
    `tests/test_citation_format.py` 的语料表里逐条冻着。
    """
    source = ref.get("source") or ""
    volume = ref.get("volume") or ""
    issue = ref.get("issue") or ""
    pages = normalize_pages(ref.get("pages"))

    runs: list[Run] = []
    if source:
        runs.append(Run(source, italic=True))
    if volume:
        runs.append(Run(f", {volume}" if source else volume, italic=True))
        if issue:
            runs.append(Run(f"({issue})"))
    elif issue:
        runs.append(Run(f", ({issue})" if source else f"({issue})"))
    if pages:
        runs.append(Run(f", {pages}" if runs else pages))
    if not runs:
        return []
    return [*runs, Run(".")]


def _mla(ref: dict) -> list[Run]:
    """MLA 第 9 版（期刊条目）。

    形态：`Smith, John K., and Alice Doe. "Title." Journal, vol. 32, no. 1, 2020, pp. 56-75.`
    著者形态由 `names.normalize_authors_mla` 负责（首位倒装、次位自然序、3 人以上 `et al.`）。

    容器里的卷/期/页**必须带 `vol.` / `no.` / `pp.` 前缀**，这是 MLA 与 APA 最好认的
    一处区别（APA 是裸数字）。缺项逐级降级，前缀跟着自己那一段走。
    """
    authors = names.normalize_authors_mla(ref.get("authors") or "")
    title = _title_of(ref, with_translation=True)

    parts = []
    if authors:
        parts.append(_as_sentence(authors))
    # 终止符落在引号**里面**（MLA 的形态就是 `"Title."`）；`_terminated` 让以问号
    # 或叹号收尾的题名保持 `"Is AI Creative?"`，而不是 `"Is AI Creative?."`。
    parts.append('"' + _terminated(title) + '"')

    # 顺序按 MLA 的要素表：容器名 → 卷 → 期 → 出版年 → 页码（年份在卷期之后，
    # 与 APA 的「卷(期), 页码」顺序不同，别照抄过来）。
    #
    # 斜体只在**容器名**上（MLA 9：期刊名/书名这类"容器"斜体，篇名与卷期页年正体）——
    # 所以 `meta` 是 Run 列表而不是字符串列表，段间那个 `, ` 由 `_runs_joined` 补、
    # 正体。这与 APA 不同：APA 的逗号在刊名与卷号**之间**要跟着斜体，那边就只能逐段
    # 自己拼。两种格式的斜体规则本就不是一回事，别为了"统一"合成一个函数。
    meta: list[Run] = []
    if ref.get("source"):
        meta.append(Run(str(ref["source"]), italic=True))
    if ref.get("volume"):
        meta.append(Run(f"vol. {ref['volume']}"))
    if ref.get("issue"):
        meta.append(Run(f"no. {ref['issue']}"))
    if ref.get("year"):
        meta.append(Run(str(ref["year"])))
    pages = normalize_pages(ref.get("pages"))
    if pages:
        # 单页用 `p.`、多页用 `pp.`（MLA 9 如此；页码里有没有连字符就是判据）
        meta.append(Run(f"{'pp.' if '-' in pages else 'p.'} {pages}"))
    if meta:
        parts.append([*_runs_joined(meta, ", "), Run(".")])
    return _flatten(parts)
