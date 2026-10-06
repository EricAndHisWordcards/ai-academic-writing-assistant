"""研究材料解析：把作者自有的数据、结果与笔记转成纯文本。

与文献解析（pdf_parser）分开：文献要保留**逐页**索引以便引用定位，作者材料只需要
一整段文本供模型理解，不需要页码，也不需要进引用调度。

支持的格式与实现方式：
- .pdf                      → pypdf（复用 pdf_parser）
- .txt/.md/.csv/.json/.log  → 直接解码（utf-8 → gb18030 → latin-1 逐级回退）
- .docx                     → docx 本质是个 zip，正文在 word/document.xml
- .xlsx                     → 同为 zip，字符串在 xl/sharedStrings.xml，单元格在 sheet xml

刻意**不引入** python-docx / openpyxl：桌面版用 PyInstaller 打包，新增依赖意味着改
spec 的 hiddenimports 并重新验证整条打包链路，而这两种格式的文本提取只需读 zip 内的
XML，标准库足够。代价是只覆盖 .docx/.xlsx（OOXML），老的二进制 .doc/.xls 不支持 ——
那种情况会给出明确的失败原因，而不是静默产出空文本。
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from app import caj_support
from app.pdf_parser import extract_pdf_text

# 单份材料最多保留的字符数。作者可能贴进来几十万字的原始记录，全量塞进提示词既
# 昂贵又会挤掉真正需要模型的指令；截断并在文本里留痕，让模型知道被截过。
MAX_CHARS = 200_000

# 送入单节生成提示词时，每份材料最多摘录的字符数
EXCERPT_CHARS = 2000

SUPPORTED_EXTENSIONS = {
    ".pdf", ".caj", ".kdh", ".txt", ".md", ".markdown", ".csv", ".json", ".log",
    ".docx", ".xlsx",
}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class UnsupportedMaterial(Exception):
    """材料格式不受支持，或虽是支持格式但内容无法提取。"""


def is_supported(filename: str) -> bool:
    return _ext(filename) in SUPPORTED_EXTENSIONS


def extract_text(filename: str, data: bytes) -> str:
    """从材料字节流提取纯文本。

    提取不到任何文字时抛 UnsupportedMaterial —— 调用方据此把文件列进 failed，
    而不是存一份空材料进库。
    """
    ext = _ext(filename)
    if ext in (".pdf", ".caj", ".kdh"):
        try:
            pdf_bytes = caj_support.unwrap_to_pdf(data)
        except caj_support.UnsupportedCaj as e:
            raise UnsupportedMaterial(str(e)) from e
        pages = extract_pdf_text(pdf_bytes)
        text = "\n".join(p["text"] for p in pages if p["text"])
    elif ext in (".docx", ".xlsx"):
        try:
            text = _extract_docx(data) if ext == ".docx" else _extract_xlsx(data)
        except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
            raise UnsupportedMaterial(f"文件损坏或不是有效的 {ext} 文件：{e}") from e
    elif ext in SUPPORTED_EXTENSIONS:
        text = _decode(data)
    else:
        raise UnsupportedMaterial(
            f"暂不支持 {ext or '该'} 格式（支持：PDF / Word(.docx) / Excel(.xlsx) / "
            f"txt / md / csv / json）"
        )

    text = text.strip()
    if not text:
        raise UnsupportedMaterial("未提取到任何文字（扫描版 PDF 或无内容的表格）")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n…（材料过长，已截断至前 {MAX_CHARS} 字）"
    return text


def _ext(filename: str) -> str:
    name = (filename or "").lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def _decode(data: bytes) -> str:
    """按常见编码逐级回退解码。

    Windows 上导出的 csv/txt 常是 GBK，直接 utf-8 解码会整段失败 —— 值得多试一次，
    比让用户自己转码友好得多。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _extract_docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    # 逐个段落取 w:t（文本 run），表格单元格里也是 w:p，所以表格会按行展开
    lines = []
    for para in root.iter(f"{_W}p"):
        lines.append("".join(t.text or "" for t in para.iter(f"{_W}t")))
    return "\n".join(lines)


def _extract_xlsx(data: bytes) -> str:
    """把工作簿每个 sheet 渲染成制表符分隔的文本（近似 CSV）。

    字符串值走 sharedStrings 索引表，数字/日期直接取 <v>。公式取缓存值 —— 与 Excel
    打开时看到的显示值一致。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sroot.iter(f"{_S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_S}t")))

        sheets = sorted(
            n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
        )
        out: list[str] = []
        for sheet in sheets:
            root = ET.fromstring(zf.read(sheet))
            out.append(f"--- {sheet.split('/')[-1]} ---")
            for row in root.iter(f"{_S}row"):
                cells = [_cell_value(c, shared) for c in row.iter(f"{_S}c")]
                line = "\t".join(cells).strip()
                if line:
                    out.append(line)
        return "\n".join(out)


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_S}t"))
    v = cell.find(f"{_S}v")
    text = v.text if v is not None and v.text else ""
    if cell.get("t") == "s" and text.isdigit() and int(text) < len(shared):
        return shared[int(text)]
    return text


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """按上限摘录材料文本，用于单节生成的提示词。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（材料过长，此处仅摘录前 {limit} 字）"
