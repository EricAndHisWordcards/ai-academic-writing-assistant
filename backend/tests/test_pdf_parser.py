"""测试：PDF 解析（pdf_parser）。

这个模块此前在 tests 里**零命中**，而它有一处吞异常的 except（逐页
`except Exception: text = ""`）。吞掉这件事本身是对的 —— 一页坏了不该作废整篇 ——
问题在**一声不吭**：调用方拿不到任何「有几页没读到」的痕迹，只检查「所有页都空」，
于是「10 页里 3 页失败」和「完全成功」长得一模一样，缺的那几页要到引用调度那步才
露马脚，那时已经查不回原因了。

这里锁住三件事：**不抛、把失败标出来、记一笔带页码的日志**；外加一条容易搞混的
边界 —— **空页不是坏页**。
"""
import logging

import pytest

from app import pdf_parser


class _Page:
    """一页 PDF：给定文本，或给定一个要抛的异常。"""

    def __init__(self, text=None, exc=None):
        self._text, self._exc = text, exc

    def extract_text(self):
        if self._exc is not None:
            raise self._exc
        return self._text


class _Reader:
    def __init__(self, pages):
        self.pages = pages


def _fake_reader(monkeypatch, pages):
    monkeypatch.setattr(pdf_parser, "PdfReader", lambda _buf: _Reader(pages))


def test_pages_carry_text_and_a_failed_flag(monkeypatch):
    """正常页：文本原样带出，failed 为假，页码从 1 起算。"""
    _fake_reader(monkeypatch, [_Page("第一页"), _Page("第二页")])

    pages = pdf_parser.extract_pdf_text(b"%PDF-1.4")

    assert pages == [
        {"page": 1, "text": "第一页", "failed": False},
        {"page": 2, "text": "第二页", "failed": False},
    ]


def test_one_bad_page_does_not_lose_the_others(monkeypatch, caplog):
    """一页提取失败：那一页留空并标出来，其余页照常，整篇不抛。"""
    _fake_reader(monkeypatch, [
        _Page("第一页"), _Page(exc=RuntimeError("坏页")), _Page("第三页"),
    ])

    with caplog.at_level(logging.WARNING, logger="app.pdf_parser"):
        pages = pdf_parser.extract_pdf_text(b"%PDF-1.4")

    assert [p["text"] for p in pages] == ["第一页", "", "第三页"]
    assert [p["failed"] for p in pages] == [False, True, False]
    # 日志必须**点名页码与异常**：只说「某篇 PDF 出过问题」，拿到手也没法定位
    assert "第 2 页" in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_blank_page_is_not_a_failed_page(monkeypatch, caplog):
    """空页 ≠ 坏页。

    真实 PDF 里有空白页（章节之间的隔页），它的文本本来就是空串；extract_text()
    返回 None 也是同一回事（不是异常）。把这两种记成解析失败，界面会在每一份带
    隔页的文献上报一条假警告 —— 而假警告会让人学会忽略真警告。
    """
    _fake_reader(monkeypatch, [_Page(""), _Page(None)])

    with caplog.at_level(logging.WARNING, logger="app.pdf_parser"):
        pages = pdf_parser.extract_pdf_text(b"%PDF-1.4")

    assert [p["text"] for p in pages] == ["", ""]
    assert [p["failed"] for p in pages] == [False, False]
    assert not caplog.records


def test_every_page_failing_is_still_not_an_exception(monkeypatch):
    """每一页都失败也不抛 —— 由调用方去判「整篇没文本」（它分扫描版与解析失败）。"""
    _fake_reader(monkeypatch, [_Page(exc=ValueError("a")), _Page(exc=ValueError("b"))])

    pages = pdf_parser.extract_pdf_text(b"%PDF-1.4")

    assert [p["failed"] for p in pages] == [True, True]
    assert all(p["text"] == "" for p in pages)
