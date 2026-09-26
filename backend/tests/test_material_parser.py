"""测试：作者自有研究材料的文本提取（material_parser）。

docx / xlsx 用代码现场拼出最小可用的 OOXML 包 —— 手写 zip 比引入一个构造库更可控，
也顺带证明了「不装 python-docx / openpyxl 也能读这两种格式」这个前提成立。
"""
import io
import zipfile

import pytest

from app import material_parser

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs
    )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _make_xlsx(rows: list[list]) -> bytes:
    """与 Excel 真实产物同构：字符串走 sharedStrings 索引表，数字直接进 <v>。"""
    strings: list[str] = []
    index: dict[str, int] = {}
    for row in rows:
        for cell in row:
            if isinstance(cell, str) and cell not in index:
                index[cell] = len(strings)
                strings.append(cell)

    shared = (
        f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{_S_NS}">'
        + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>"
    )
    sheet_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, cell in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(cell, str):
                cells.append(f'<c r="{ref}" t="s"><v>{index[cell]}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{cell}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{_S_NS}">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", shared)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


# ---------------------------------------------------------------
# 纯文本
# ---------------------------------------------------------------
def test_txt_utf8():
    text = material_parser.extract_text("笔记.txt", "样本量 312 份".encode("utf-8"))
    assert text == "样本量 312 份"


def test_txt_gbk_falls_back():
    """Windows 上导出的 csv 常是 GBK，直接 utf-8 解码会整段失败。"""
    text = material_parser.extract_text("数据.csv", "回收率,92%".encode("gb18030"))
    assert "回收率" in text and "92%" in text


def test_txt_utf8_bom_stripped():
    text = material_parser.extract_text("a.txt", "﻿正文".encode("utf-8"))
    assert text == "正文"


# ---------------------------------------------------------------
# Word / Excel
# ---------------------------------------------------------------
def test_docx_reads_paragraphs():
    text = material_parser.extract_text(
        "研究思路.docx", _make_docx(["研究问题：社交媒体的双刃剑", "方法：问卷 + 访谈"])
    )
    assert "研究问题：社交媒体的双刃剑" in text
    assert "方法：问卷 + 访谈" in text


def test_xlsx_reads_shared_strings_and_numbers():
    data = _make_xlsx([
        ["变量", "均值", "样本量"],
        ["使用强度", 3.82, 312],
    ])
    text = material_parser.extract_text("描述统计.xlsx", data)
    assert "使用强度" in text
    assert "3.82" in text
    assert "312" in text


def test_xlsx_skips_empty_rows():
    data = _make_xlsx([["有内容"], ["", ""], ["也有内容"]])
    text = material_parser.extract_text("a.xlsx", data)
    assert text.count("\n") == 2  # 标题行 + 两条内容行，空行被丢掉


def test_corrupt_docx_reports_reason():
    """zip 损坏要给出可读原因，而不是抛一个 BadZipFile 出去。"""
    with pytest.raises(material_parser.UnsupportedMaterial) as exc:
        material_parser.extract_text("坏文件.docx", b"this is not a zip at all")
    assert "损坏" in str(exc.value)


# ---------------------------------------------------------------
# 拒绝与兜底
# ---------------------------------------------------------------
def test_legacy_doc_is_rejected_with_reason():
    """老的二进制 .doc/.xls 不是 zip，必须明确拒绝而不是产出空文本。"""
    with pytest.raises(material_parser.UnsupportedMaterial) as exc:
        material_parser.extract_text("旧文档.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert ".doc" in str(exc.value)


def test_empty_content_is_rejected():
    with pytest.raises(material_parser.UnsupportedMaterial):
        material_parser.extract_text("空.txt", b"   \n  ")


def test_pdf_without_text_is_rejected():
    """无文本层的 PDF（扫描版）与 PDF 解析失败要落到同一个明确结果。"""
    empty_pdf = _empty_pdf()
    with pytest.raises(material_parser.UnsupportedMaterial) as exc:
        material_parser.extract_text("扫描件.pdf", empty_pdf)
    assert "未提取到任何文字" in str(exc.value)


def _empty_pdf() -> bytes:
    """一个合法但没有文字的最小 PDF。"""
    content = "BT /F1 14 Tf 72 740 Td ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        f"<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(pdf)
    pdf += f"xref\n0 {len(objs)+1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += (
        f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return pdf


def test_supported_extensions_check():
    assert material_parser.is_supported("a.pdf")
    assert material_parser.is_supported("A.DOCX")   # 大小写不敏感
    assert not material_parser.is_supported("a.doc")
    assert not material_parser.is_supported("没有扩展名")


def test_long_text_is_truncated_with_notice():
    text = material_parser.extract_text("长.txt", ("字" * (material_parser.MAX_CHARS + 500)).encode())
    assert len(text) > material_parser.MAX_CHARS   # 截断提示本身占字符
    assert "已截断" in text
    assert text.startswith("字")


def test_excerpt_truncates_and_marks():
    assert material_parser.excerpt("短文本", 10) == "短文本"
    out = material_parser.excerpt("字" * 50, 10)
    assert out.startswith("字" * 10)
    assert "仅摘录前 10 字" in out
