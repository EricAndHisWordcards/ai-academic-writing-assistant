"""测试：知网 CAJ 家族格式转换（caj_support）。

锁住三件事：
- %PDF 直头 .caj 原样透传；
- CAJ 容器剥离后能重建出 pypdf 可读的 PDF；
- HN/C8/TEB 与损坏文件明确拒绝，消息里带「PDF」指引。
"""
import io
import struct

import pytest
from pypdf import PdfReader

from app import caj_support


def _make_pdf_payload(text: str = "Hello") -> bytes:
    """构造一个最小合法 PDF（含 xref/trailer/startxref，可直接读）。"""
    content = f"BT /F1 14 Tf 72 740 Td 20 TL ({text}) Tj T* ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
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
    pdf += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return pdf


def _make_caj_container(pdf_payload: bytes, page_num: int = 1) -> bytes:
    """把 PDF 对象流包进 CAJ 容器（CAJ\x00 头 + 页数偏移 0x10 + 一级指针 0x14）。"""
    header_len = 0x20
    level1_ptr = header_len
    pdf_start = header_len + 4
    header = bytearray(header_len)
    header[0:4] = b"CAJ\x00"
    struct.pack_into("<i", header, 0x10, page_num)
    struct.pack_into("<i", header, 0x14, level1_ptr)
    return bytes(header) + struct.pack("<i", pdf_start) + pdf_payload


def _make_kdh(pdf_payload: bytes) -> bytes:
    """构造 KDH：254 字节前缀 + XOR 密文 + 尾部垃圾。"""
    header = b"\x00" * caj_support._KDH_HEADER_LEN
    key = caj_support._KDH_PASSPHRASE
    encrypted = bytes(
        pdf_payload[i] ^ key[i % len(key)] for i in range(len(pdf_payload))
    )
    return b"KDH " + header[4:] + encrypted + b"GARBAGE-TAIL"


# ---------------------------------------------------------------
# 直头透传
# ---------------------------------------------------------------
def test_pdf_header_passthrough():
    """%PDF 头的 .caj 就是 PDF，原样返回，一个字节都不动。"""
    payload = _make_pdf_payload()
    assert caj_support.unwrap_to_pdf(payload) is payload


# ---------------------------------------------------------------
# CAJ 容器
# ---------------------------------------------------------------
def test_caj_container_extracts_pdf():
    """CAJ 容器剥离内嵌流，pypdf 能读，文本完整。"""
    payload = _make_pdf_payload("CAJ container test")
    caj = _make_caj_container(payload, page_num=1)
    out = caj_support.unwrap_to_pdf(caj)
    reader = PdfReader(io.BytesIO(out), strict=False)
    assert len(reader.pages) == 1
    assert "CAJ container test" in reader.pages[0].extract_text()


def test_caj_container_rebuilds_missing_catalog():
    """内嵌流缺 catalog/pages 时，补重建后 pypdf 仍能读。"""
    # 只放 page 对象，缺 catalog/pages：CAJ 头型存在的理由。
    content = "BT /F1 14 Tf 72 740 Td 20 TL (orphan) Tj T* ET"
    orphan = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 3 0 R /Resources << /Font << /F1 4 0 R >> >> >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [1 0 R] /Count 1 >>\nendobj\n"
        + f"3 0 obj\n<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream\nendobj\n".encode()
        + b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    )
    caj = _make_caj_container(orphan, page_num=1)
    out = caj_support.unwrap_to_pdf(caj)
    reader = PdfReader(io.BytesIO(out), strict=False)
    assert len(reader.pages) == 1
    assert "orphan" in reader.pages[0].extract_text()


# ---------------------------------------------------------------
# KDH 解密
# ---------------------------------------------------------------
def test_kdh_decryption():
    """KDH 解密后还原为完整 PDF，尾部垃圾被截掉。"""
    payload = _make_pdf_payload("KDH test")
    kdh = _make_kdh(payload)
    out = caj_support.unwrap_to_pdf(kdh)
    assert out == payload
    reader = PdfReader(io.BytesIO(out), strict=False)
    assert "KDH test" in reader.pages[0].extract_text()


def test_kdh_too_short():
    """KDH 短于头部长度直接拒绝。"""
    with pytest.raises(caj_support.UnsupportedCaj, match="过短|损坏"):
        caj_support.unwrap_to_pdf(b"KDH " + b"\x00" * 10)


def test_kdh_no_eof():
    """解密后找不到 %%EOF 说明不是 KDH 或已损坏。"""
    # 故意构造一段无 %%EOF 的内容（不能复用 _make_pdf_payload，它自带 %%EOF）。
    no_eof = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n"
    kdh = _make_kdh(no_eof)
    with pytest.raises(caj_support.UnsupportedCaj, match="结束标记|不是 KDH"):
        caj_support.unwrap_to_pdf(kdh)


# ---------------------------------------------------------------
# 私有/扫描格式拒绝
# ---------------------------------------------------------------
@pytest.mark.parametrize(
    "head,expected",
    [
        (b"HN\xc8\x00", "HN/C8"),
        (b"\xc8\x00\x00\x00", "HN/C8"),
        (b"TEB\x00", "无法识别"),
        (b"XXXX", "无法识别"),
    ],
)
def test_unsupported_formats(head, expected):
    """HN/C8/TEB/未知头全部抛 UnsupportedCaj，消息含 PDF 指引。"""
    with pytest.raises(caj_support.UnsupportedCaj, match=expected):
        caj_support.unwrap_to_pdf(head + b"\x00" * 100)


# ---------------------------------------------------------------
# 结构损坏
# ---------------------------------------------------------------
def test_caj_container_truncated():
    """CAJ 头存在但指针越界，收敛成 UnsupportedCaj 而不是 struct 异常。"""
    bad = b"CAJ\x00" + b"\x00" * 0x10 + struct.pack("<i", 9999) + b"\x00" * 4
    with pytest.raises(caj_support.UnsupportedCaj, match="结构损坏|不兼容"):
        caj_support.unwrap_to_pdf(bad)
