"""从常见文档中提取纯文本，供模型阅读。"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_EXTRACT_CHARS = 200_000
MAX_PDF_PAGES = 100
MAX_SHEET_ROWS = 1000

TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".log",
    ".xml",
    ".yaml",
    ".yml",
    ".ini",
    ".conf",
    ".html",
    ".htm",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".c",
    ".cpp",
    ".go",
    ".rs",
    ".sql",
}
OLD_OFFICE_SUFFIXES = {".doc", ".xls", ".ppt"}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class DocumentError(Exception):
    """文档无法解析。"""


def _decode_plain(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError("文件不是有效的 Office 文档") from exc
    total = sum(item.file_size for item in archive.infolist())
    if total > MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise DocumentError("文档解压后过大")
    return archive


def _docx_paragraph_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        local = node.tag.rsplit("}", 1)[-1]
        if local == "t":
            parts.append(node.text or "")
        elif local == "tab":
            parts.append("\t")
        elif local in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _extract_docx(data: bytes) -> str:
    with _open_zip(data) as archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise DocumentError("docx 缺少正文内容") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentError("docx 内容不是有效的 XML") from exc

    body = root.find(W + "body")
    if body is None:
        return ""

    lines: list[str] = []
    for child in body:
        local = child.tag.rsplit("}", 1)[-1]
        if local == "p":
            lines.append(_docx_paragraph_text(child))
        elif local == "tbl":
            for row in child.iter(W + "tr"):
                cells: list[str] = []
                for cell in row.findall(W + "tc"):
                    paragraphs = [
                        _docx_paragraph_text(item)
                        for item in cell.iter(W + "p")
                    ]
                    cells.append("\n".join(paragraphs).strip())
                lines.append("\t".join(cells))
    return "\n".join(lines)


def _extract_pptx(data: bytes) -> str:
    with _open_zip(data) as archive:
        slides = [
            name
            for name in archive.namelist()
            if re.match(r"ppt/slides/slide\d+\.xml$", name)
        ]
        slides.sort(key=lambda name: int(re.search(r"(\d+)", name).group(1)))
        parts: list[str] = []
        for index, name in enumerate(slides, start=1):
            try:
                root = ET.fromstring(archive.read(name))
            except (KeyError, ET.ParseError) as exc:
                raise DocumentError("pptx 幻灯片内容无效") from exc
            blocks: list[str] = []
            for paragraph in root.iter(A + "p"):
                text = "".join(node.text or "" for node in paragraph.iter(A + "t"))
                if text.strip():
                    blocks.append(text)
            if blocks:
                parts.append(f"## 幻灯片 {index}\n" + "\n".join(blocks))
    return "\n\n".join(parts)


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    if not letters:
        return 0
    index = 0
    for char in letters.group(0).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def _extract_xlsx(data: bytes) -> str:
    with _open_zip(data) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.findall(S + "si"):
                    shared.append(
                        "".join(node.text or "" for node in item.iter(S + "t"))
                    )
            except ET.ParseError as exc:
                raise DocumentError("xlsx 共享字符串无效") from exc

        sheets = [
            name
            for name in archive.namelist()
            if re.match(r"xl/worksheets/sheet\d+\.xml$", name)
        ]
        sheets.sort(key=lambda name: int(re.search(r"(\d+)", name).group(1)))

        parts: list[str] = []
        for index, name in enumerate(sheets, start=1):
            try:
                root = ET.fromstring(archive.read(name))
            except (KeyError, ET.ParseError) as exc:
                raise DocumentError("xlsx 工作表内容无效") from exc
            lines: list[str] = []
            for row in root.iter(S + "row"):
                if len(lines) >= MAX_SHEET_ROWS:
                    lines.append("…（工作表行数过多，已截断）")
                    break
                values: list[str] = []
                for cell in row.findall(S + "c"):
                    column = _column_index(cell.get("r", ""))
                    while len(values) < column:
                        values.append("")
                    cell_type = cell.get("t")
                    value = ""
                    if cell_type == "s":
                        raw = cell.find(S + "v")
                        idx = int(raw.text) if raw is not None and raw.text else -1
                        if 0 <= idx < len(shared):
                            value = shared[idx]
                    elif cell_type == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter(S + "t"))
                    else:
                        raw = cell.find(S + "v")
                        value = raw.text if raw is not None and raw.text else ""
                    values.append(value)
                lines.append("\t".join(values).rstrip())
            parts.append(f"## 工作表 {index}\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentError("服务器未安装 PDF 解析依赖 pypdf") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("PDF 已加密，无法读取")
        parts: list[str] = []
        for index, page in enumerate(reader.pages):
            if index >= MAX_PDF_PAGES:
                parts.append("…（页数过多，已截断）")
                break
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                parts.append(text)
        return "\n\n".join(parts)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"PDF 解析失败：{exc}") from exc


def extract_text(filename: str, data: bytes, content_type: str = "") -> str:
    """按扩展名提取文本；失败抛出 DocumentError。"""
    suffix = Path(filename or "").suffix.lower()
    mime = (content_type or "").split(";")[0].strip().lower()

    if suffix == ".pdf" or mime == "application/pdf":
        text = _extract_pdf(data)
    elif suffix == ".docx" or mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        text = _extract_docx(data)
    elif suffix == ".xlsx" or mime == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ):
        text = _extract_xlsx(data)
    elif suffix == ".pptx" or mime == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ):
        text = _extract_pptx(data)
    elif suffix in OLD_OFFICE_SUFFIXES:
        raise DocumentError("暂不支持旧版 Office 格式，请另存为 docx/xlsx/pptx")
    elif suffix in TEXT_SUFFIXES or mime.startswith("text/") or mime in {
        "application/json",
        "application/xml",
    }:
        text = _decode_plain(data)
    elif not suffix:
        text = _decode_plain(data)
    else:
        raise DocumentError(f"不支持的文档格式：{suffix}")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_EXTRACT_CHARS:
        text = text[:MAX_EXTRACT_CHARS] + "\n…（内容过长，已截断）"
    return text.strip()
