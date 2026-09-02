from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub

MAX_TXT_BYTES = 10 * 1024 * 1024
MAX_EPUB_BYTES = 30 * 1024 * 1024
MAX_CHAPTERS = 2000

CHAPTER_HEADING = re.compile(
    r"^\s*("
    r"第[0-9零一二三四五六七八九十百千万两]+[章节集回卷幕部篇][^\n]{0,60}"
    r"|序章[^\n]{0,60}|楔子[^\n]{0,60}|引子[^\n]{0,60}|尾声[^\n]{0,60}"
    r"|后记[^\n]{0,60}|番外[^\n]{0,60}"
    r"|(?:chapter|episode|ep)\s*[0-9ivxlcdm]+[^\n]{0,60}"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class InvalidSourceFile(ValueError):
    pass


@dataclass(slots=True)
class ParsedChapter:
    title: str
    content: str


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InvalidSourceFile("TXT 文件编码无法识别，请转换为 UTF-8 后重试")


def parse_epub(data: bytes) -> str:
    try:
        book = epub.read_epub(io.BytesIO(data), options={"ignore_ncx": True})
    except Exception as error:
        raise InvalidSourceFile("EPUB 文件无法解析或已损坏") from error
    sections: list[str] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for node in soup(["script", "style"]):
            node.decompose()
        text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        if len(text) < 20:
            continue
        title_node = soup.find(["h1", "h2", "title"])
        title = title_node.get_text(" ", strip=True) if title_node else ""
        sections.append(f"{title}\n{text}".strip())
    if not sections:
        raise InvalidSourceFile("EPUB 中没有可读取的正文内容")
    return "\n\n".join(sections)


def parse_source(data: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        if len(data) > MAX_TXT_BYTES:
            raise InvalidSourceFile("TXT 文件不能超过 10 MB")
        text = decode_text(data)
    elif suffix == ".epub":
        if len(data) > MAX_EPUB_BYTES:
            raise InvalidSourceFile("EPUB 文件不能超过 30 MB")
        text = parse_epub(data)
    else:
        raise InvalidSourceFile("仅支持 TXT 与 EPUB 文件")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise InvalidSourceFile("导入内容为空")
    return text


def extract_chapters(text: str, fallback_title: str) -> list[ParsedChapter]:
    matches = list(CHAPTER_HEADING.finditer(text))
    if not matches:
        return [ParsedChapter(title=fallback_title or "正文", content=text.strip())]

    chapters: list[ParsedChapter] = []
    preface = text[: matches[0].start()].strip()
    if preface:
        chapters.append(ParsedChapter(title="序章", content=preface))
    for index, match in enumerate(matches[:MAX_CHAPTERS]):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            chapters.append(ParsedChapter(title=match.group(1).strip(), content=content))
    if not chapters:
        return [ParsedChapter(title=fallback_title or "正文", content=text.strip())]
    return chapters[:MAX_CHAPTERS]


def resolve_stored_file(uploads_root: Path, relative_path: str) -> Path:
    root = uploads_root.resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise InvalidSourceFile("文件存储路径无效") from error
    return target
