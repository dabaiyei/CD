"""Chapter lists must not carry the full text of every chapter."""
from datetime import UTC, datetime
from types import SimpleNamespace

from app.domain.schemas import ChapterContent, ChapterPublic


def fake_chapter(**overrides):
    values = dict(
        id="ch-1", project_id="p-1", source_file_id="src-1", source_mode="novel",
        order_index=1, title="第一章", status="uninitialized",
        active_script_version_id=None, created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC), original_content="正文内容" * 5000,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_the_chapter_summary_omits_the_body():
    summary = ChapterPublic.from_chapter(fake_chapter())
    dumped = summary.model_dump()
    assert "original_content" not in dumped
    # The list still describes the chapter well enough to render.
    assert dumped["title"] == "第一章"
    assert dumped["order_index"] == 1


def test_the_chapter_summary_reports_size_and_presence():
    summary = ChapterPublic.from_chapter(fake_chapter())
    assert summary.has_content is True
    assert summary.content_length == len("正文内容" * 5000)
    empty = ChapterPublic.from_chapter(fake_chapter(original_content="   "))
    assert empty.has_content is False
    assert empty.content_length == 3


def test_the_summary_is_far_smaller_than_the_body():
    chapter = fake_chapter()
    summary_bytes = len(ChapterPublic.from_chapter(chapter).model_dump_json())
    assert summary_bytes < len(chapter.original_content) / 10


def test_the_content_payload_carries_only_that_chapter():
    content = ChapterContent(id="ch-1", title="第一章", original_content="正文")
    assert set(content.model_dump()) == {"id", "title", "original_content"}
