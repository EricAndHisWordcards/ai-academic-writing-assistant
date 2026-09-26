"""PDF 解析：提取文本并按页建立索引。

产物是**每页一条**的记录（页码 + 该页文本），页码随文本一起带出来供引用调度与
确定性标注使用；落库时只留页码与片段，见 routers/projects.py 里的 stored_pages。
"""
from __future__ import annotations

import io
import logging
from typing import Any

from pypdf import PdfReader

logger = logging.getLogger(__name__)


def extract_pdf_text(data: bytes) -> list[dict[str, Any]]:
    """从 PDF 字节流提取每页文本。

    返回：[{"page": 1, "text": "...", "failed": False}, ...]

    **单页失败不丢整篇**：扫描版混排、字体缺表、结构损坏都可能让某几页的
    extract_text 抛异常。为一页作废整篇的代价太大（其余页都是好的），所以那一页
    文本留空、`failed` 置真，并写一条 warning **点名页码与异常**。

    `failed` 这个键是给调用方**数**的。此前这里是 `except Exception: text = ""`，
    一声不吭，而调用方只检查「所有页都空」—— 于是「10 页里 3 页解析失败」与「完全
    成功」在它看来一模一样，缺的那几页要到引用调度那一步才露马脚，那时已经查不回
    原因了。
    """
    reader = PdfReader(io.BytesIO(data))
    pages: list[dict[str, Any]] = []
    for idx, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
            failed = False
        except Exception as e:  # noqa: BLE001
            text, failed = "", True
            logger.warning(
                "PDF 第 %d 页文本提取失败已跳过：%s: %s", idx, type(e).__name__, e
            )
        pages.append({"page": idx, "text": text.strip(), "failed": failed})
    return pages
