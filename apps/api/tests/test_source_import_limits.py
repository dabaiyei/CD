"""TXT import accepts large manuscripts without being silently truncated."""
from pathlib import Path

import pytest

from app.services.source_import import (
    MAX_EPUB_BYTES,
    MAX_TXT_BYTES,
    InvalidSourceFile,
    extract_chapters,
    parse_source,
)


def txt_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def test_txt_allows_a_hundred_megabyte_manuscript():
    assert MAX_TXT_BYTES == 100 * 1024 * 1024
    # EPUB keeps its own, smaller limit.
    assert MAX_EPUB_BYTES == 30 * 1024 * 1024


def test_a_txt_over_the_limit_is_rejected_with_the_real_number():
    payload = b"a" * (MAX_TXT_BYTES + 1)
    with pytest.raises(InvalidSourceFile, match="100 MB"):
        parse_source(payload, "novel.txt")


def test_a_txt_between_the_old_and_new_limit_is_accepted():
    # 40 MB used to be rejected outright; it must now parse.
    body = "第1章 开端\n\n" + ("正文内容。" * 20) + "\n\n第2章 转折\n\n" + ("继续推进。" * 20)
    raw = txt_bytes(body)
    sized = raw + b" " * (40 * 1024 * 1024 - len(raw))
    text = parse_source(sized, "novel.txt")
    assert "第1章 开端" in text and "第2章 转折" in text
    chapters = extract_chapters(text, "fallback")
    assert [chapter.title for chapter in chapters][:2] == ["第1章 开端", "第2章 转折"]


def test_the_route_reads_the_cap_for_the_files_own_type():
    # The route used to read MAX_EPUB_BYTES + 1 for every file, so a TXT larger
    # than 30 MB was truncated before parse_source ever saw it.
    from app.api.routes import director

    source = Path(director.__file__).read_text(encoding="utf-8")
    assert "read_cap = MAX_TXT_BYTES if" in source
    assert "file.read(read_cap + 1)" in source
    assert "file.read(MAX_EPUB_BYTES + 1)" not in source


def test_unsupported_extensions_are_still_rejected():
    with pytest.raises(InvalidSourceFile, match="仅支持 TXT 与 EPUB"):
        parse_source(b"data", "notes.pdf")
