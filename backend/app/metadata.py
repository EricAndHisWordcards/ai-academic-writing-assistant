"""文献元数据的「缺失值」判定：什么算没写。

模型在文献首屏里找不到作者/年份/来源时，往往不会留空，而是写一个占位词
（`未提及`、`Not specified`、`未知`…）。这些字符串是**非空的**，所以
`setdefault(field, "")` 一类的兜底拦不住它们，会被当成真实值一路用到用户可见处：

- 文末参考文献列表印出「[1] 未提及. 题名[J]. 来源, 未提及.」；
- 拟大纲时把「未提及 · 未提及」当元数据喂给模型；
- 前端文献列表直接显示「未提及 · 未提及 ·」。

本模块是「什么算缺失」的**单一事实来源**，写入侧（`agents.parse_agent`）与
存量数据迁移侧（`db._migrate_document_meta`）、显示侧（`citation_format`）
共用一份判定，避免三处各自维护一份清单而漂移。

为什么是**显式列举**而不是正则/模糊匹配：`db._migrate_paper_types` 已立下纪律
——要改的目标集必须是确定的。`无`、`-`、`n/a` 这类短值若用通配去猜，迟早会误伤
真实的作者名或来源名。特别地，**`佚名` 不算缺失**：它在 GB/T 7714 里是合法的
正式写法，不该被抹掉。
"""
from __future__ import annotations

from typing import Any

# 元数据字段：这三项会进入参考文献列表与提示词，都需要判缺失。
# title 不在其中——题名缺失由 citation_format 用「（未命名文献）」兜底，
# summary 也不在其中——它只作为提示词上下文，且本来就允许写「无摘要」类表述。
META_FIELDS = ("authors", "year", "source")

# 只服务于**文末参考文献著录**的字段：期刊著录四项 + 题名的英译（APA 7 的非英语文献
# 要印成「原文题名 [英译]」）+ 非期刊类型的三项（出版地 / 版本项 / 报纸出版日期，
# GB/T 7714-2015 里 [M]/[D]/[C]/[N] 各需一项）。它们同样会被直接拼进文末列表，
# 所以占位词必须拦掉 —— 否则会印成「教育研究, 2023, 未提及(未提及): 未提及.」
# 或「题名 [未提及].」或「未提及: 未提及, 2023.」这种一眼假的条目。
#
# **刻意与 META_FIELDS 分开列举**，两边的消费方不同：META_FIELDS 那三项还要进提示词、
# 上前端文献列表，所以 db._migrate_document_meta 只需要清它们；这几项只在文末列表里
# 露面（题名英译与新增那三项还会出现在编辑表单里，那是它们的写入侧）。但两组都过同一个
# clean_meta_value —— 判定点只有一处，分开的只是清单。
#
# 名字说的是这组字段**为谁而存在**，不是其中每一项的形态：写成 JOURNAL_FIELDS
# 就装不下 title_en 与 place，而它们需要的清洗与那四项分毫不差。
#
# 新增一项时要记得：这里只负责**清洗**，落库与快照还有另外三份手写清单要跟着改
# （db.DOC_META_COLUMNS / add_document 的 INSERT / db._SNAPSHOT_META_FIELDS）。
REFERENCE_FIELDS = (
    "volume", "issue", "page_range", "source_type", "title_en",
    "place", "edition", "publish_date",
)

# 占位词清单。全部按**小写**存放：比对时统一 lower()，于是 "Not Specified"
# 也能命中，而中文不受 lower() 影响。加入新词前先想清楚它会不会是真实值。
MISSING_META_VALUES = frozenset({
    # 中文占位词
    "未提及", "未提供", "未说明", "未标注", "未给出", "未注明", "未知", "不详",
    "无", "空", "暂无", "缺失",
    # 英文占位词
    "not specified", "not given", "not stated", "not provided", "unspecified",
    "unknown", "n/a", "na", "none", "null", "nil",
    # 符号占位词
    "-", "--", "—", "－",
})


def clean_meta_value(value: Any) -> str:
    """归一化单个文献元数据字段：占位词与空白统一成空字符串，真实值原样保留。

    返回的永远是 `str`，调用方不必再判 `None`。只做「是不是占位词」这一个判断，
    不做任何值改写（真实值的首尾空白也只在比对时忽略，返回时保留其 strip 结果）。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.lower() in MISSING_META_VALUES:
        return ""
    return text


def clean_meta_fields(data: dict) -> dict:
    """就地清洗 `data` 里的 `META_FIELDS` 三个字段并返回同一个 dict。"""
    for field in META_FIELDS:
        data[field] = clean_meta_value(data.get(field))
    return data


def clean_reference_fields(data: dict) -> dict:
    """就地清洗 `data` 里的 `REFERENCE_FIELDS` 这几个文末著录字段并返回同一个 dict。"""
    for field in REFERENCE_FIELDS:
        data[field] = clean_meta_value(data.get(field))
    return data
