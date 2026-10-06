"""知网 CAJ 家族格式 → PDF 字节流转换。

知网下载的文件后缀同为 .caj，但内部格式完全不同。本模块只支持「有文字层」的三种：

1. **%PDF 直头**：文件本身就是 PDF，只是套了 .caj 后缀 —— 原样透传。
2. **CAJ 容器**：老版知网私有封装，内嵌 PDF 对象流（可能乱序、缺 catalog/xref）。
   按固定偏移读取内嵌流，补 catalog/pages 后交给 pypdf 非严格模式修复 xref。
3. **KDH**：前 254 字节为头部，之后内容以口令 ``FZHMEI`` 循环 XOR 加密的 PDF。

**不支持**（直接抛 UnsupportedCaj）：
- ``HN``/``C8`` 头：私有分页图片容器，即便重建 PDF 也无文字层，系统没有 OCR。
- ``TEB``：工具书格式，无公开解析器。

参考 caj2pdf（GLWT 许可证）的二进制结构解析参数，仅复用偏移/口令等事实性常量。
"""
from __future__ import annotations

import io
import re
import struct
from typing import Any

from pypdf import PdfReader, PdfWriter

# 上传入口共享的扩展名白名单（文献 + 材料共用同一份判据）。
CAJ_DOC_EXTS = {".pdf", ".caj", ".kdh"}

# KDH 加解密口令（caj2pdf 公开参数）。
_KDH_PASSPHRASE = b"FZHMEI"
# KDH 头部固定长度。
_KDH_HEADER_LEN = 254


class UnsupportedCaj(Exception):
    """CAJ 格式为私有/扫描/损坏类型，无法提取文字层。"""


def _fmt_of(data: bytes) -> str:
    """按文件头识别 CAJ 家族格式。"""
    head = data[:4]
    if head[:1] == b"\xc8":
        return "C8"
    if head[:2] == b"HN" and data[2:4] == b"\xc8\x00":
        return "HN"
    fmt = head.replace(b"\x00", b"").decode("gb18030", errors="replace")
    if fmt == "CAJ":
        return "CAJ"
    if fmt == "HN":
        return "HN"
    if fmt == "%PDF":
        return "PDF"
    if fmt == "KDH ":
        return "KDH"
    if fmt == "TEB":
        return "TEB"
    return "unknown"


def unwrap_to_pdf(data: bytes) -> bytes:
    """把知网文件字节流转换成标准 PDF 字节流。

    :raises UnsupportedCaj: 私有扫描格式（HN/C8/TEB）或结构损坏。
    """
    fmt = _fmt_of(data)
    if fmt == "PDF":
        return data
    if fmt == "CAJ":
        return _unwrap_caj(data)
    if fmt == "KDH":
        return _unwrap_kdh(data)
    if fmt in ("HN", "C8"):
        raise UnsupportedCaj(
            "这是知网私有扫描格式（HN/C8），没有文字层。请到知网下载页改下 PDF 版本，"
            "或用 CAJViewer 打印为 PDF 后上传。"
        )
    raise UnsupportedCaj(
        "无法识别的知网文件格式（可能已损坏或版本过旧）。请改下 PDF 版本后上传。"
    )


def _unwrap_caj(data: bytes) -> bytes:
    """从 CAJ 容器中提取并重建内嵌 PDF。

    结构（参考 caj2pdf）：
      - 0x10: int32 页数
      - 0x14: int32 一级指针（指向二级起始偏移）
      - 一级指针处: int32 二级起始偏移（PDF 对象区起点）
      - 对象区终点: 最后一个 ``endobj`` 之后

    内嵌对象流常缺 catalog/xref，补 catalog + pages + ``%%EOF`` 后由 pypdf 非严格
    模式重建 xref。
    """
    try:
        page_num = struct.unpack_from("<i", data, 0x10)[0]
        level1 = struct.unpack_from("<i", data, 0x14)[0]
        pdf_start = struct.unpack_from("<i", data, level1)[0]
        pdf_end = data.rfind(b"endobj") + len(b"endobj")
        if page_num <= 0 or pdf_start >= pdf_end or pdf_start > len(data):
            raise ValueError("CAJ 容器内嵌流边界异常")
        pdf_data = data[pdf_start:pdf_end]
    except (struct.error, ValueError, IndexError) as e:
        raise UnsupportedCaj(f"CAJ 容器结构损坏或版本不兼容：{e}") from e

    # 对象区可能缺 catalog/pages/xref。用 pypdf 先读一遍，能读就直接返回；
    # 读不了则补 catalog 后重试（非严格模式会重建 xref）。
    try:
        reader = PdfReader(io.BytesIO(pdf_data), strict=False)
        if len(reader.pages) == page_num:
            return pdf_data
    except Exception:  # noqa: BLE001
        pass

    # 对象区缺 catalog：从对象编号推断 root pages，补最简 catalog。
    return _rebuild_caj_pdf(pdf_data, page_num)


def _rebuild_caj_pdf(pdf_data: bytes, page_num: int) -> bytes:
    """给缺 catalog 的 CAJ 内嵌流补 catalog/pages 对象。

    扫描全部 ``N 0 obj`` 与 ``/Parent N 0 R``，推断 root pages：
    - 有 Parent 指向但对象缺失 → 该编号即缺失的 root pages；
    - 全部 Parent 都有对象 → 没有被任何对象 Parent 指向的 pages 对象即 root。

    补 catalog + 末尾追加 ``%%EOF`` 与 ``startxref``（pypdf 5.x 非严格模式
    需要 startxref 行触发对象扫描重建 xref）。
    """
    obj_nums: set[int] = set()
    for m in re.finditer(rb"(\d+)\s+0\s+obj", pdf_data):
        obj_nums.add(int(m.group(1)))

    parents: set[int] = set()
    for m in re.finditer(rb"/Parent\s+(\d+)\s+0\s+R", pdf_data):
        parents.add(int(m.group(1)))

    missing = parents - obj_nums
    if missing:
        # Parent 指向了不存在的对象：那就是缺失的 root pages。
        root_pages_no = min(missing)
    else:
        # Parent 都有对象：catalog 缺了但 pages 完整，root 是 Parent 指向的 pages 对象。
        root_pages_no = min(parents) if parents else max(obj_nums, default=0) + 1

    catalog_no = max(obj_nums | {root_pages_no}) + 1
    catalog_obj = (
        f"{catalog_no} 0 obj\n<</Type /Catalog /Pages {root_pages_no} 0 R>>\nendobj\n"
    ).encode()

    # pypdf 非严格模式要求 startxref 行存在才能触发对象扫描，且值不能为 0
    #（否则会 seek 到 -1）。trailer 提供 /Root 供 pypdf 定位 catalog。
    trailer = (
        f"\ntrailer\n<< /Size {catalog_no + 1} /Root {catalog_no} 0 R >>\n"
        f"startxref\n10\n%%EOF\n"
    ).encode()
    # 确保 catalog 前有一个换行，避免与前一对象的 endobj 粘连。
    rebuilt = pdf_data + b"\n" + catalog_obj + trailer

    # 用 pypdf 读一遍验证，同时重写为合法 PDF（修复 xref）。
    try:
        reader = PdfReader(io.BytesIO(rebuilt), strict=False)
        if len(reader.pages) != page_num:
            raise ValueError(f"重建后页数不符：期望 {page_num}，实际 {len(reader.pages)}")
        writer = PdfWriter()
        writer.append(reader)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception as e:  # noqa: BLE001
        raise UnsupportedCaj(f"CAJ 内嵌 PDF 重建失败：{e}") from e


def _unwrap_kdh(data: bytes) -> bytes:
    """解密 KDH 并截取到 ``%%EOF``。

    结构：前 254 字节头部 + 之后内容以 6 字节口令循环 XOR。
    """
    if len(data) <= _KDH_HEADER_LEN:
        raise UnsupportedCaj("KDH 文件过短，可能已损坏")
    payload = data[_KDH_HEADER_LEN:]
    key_len = len(_KDH_PASSPHRASE)
    decrypted = bytes(
        payload[i] ^ _KDH_PASSPHRASE[i % key_len] for i in range(len(payload))
    )
    eof = decrypted.rfind(b"%%EOF")
    if eof < 0:
        raise UnsupportedCaj("KDH 解密后找不到 PDF 结束标记，可能不是 KDH 或已损坏")
    # 保留 %%EOF 及其后的换行（如果有），与 caj2pdf 行为一致。
    end = eof + len(b"%%EOF")
    if end < len(decrypted) and decrypted[end] == 0x0A:
        end += 1
    elif end < len(decrypted) and decrypted[end] == 0x0D:
        end += 1
        if end < len(decrypted) and decrypted[end] == 0x0A:
            end += 1
    return decrypted[:end]
