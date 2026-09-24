"""Validation/storage for documents read by the runtime's MarkItDown tool."""
from pathlib import Path
from uuid import uuid4
from io import BytesIO
import zipfile

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json",
}


def supported_attachment(mime):
    return mime.startswith("image/") or mime in DOCUMENT_TYPES.values()


def save_document(data, *, filename, root, owner):
    suffix = Path(filename).suffix.lower()
    if suffix not in DOCUMENT_TYPES:
        raise ValueError("不支持此文档格式")
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("文档不能为空且不能超过32MB")
    if suffix == ".pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("PDF文件格式无效")
    if suffix in {".docx", ".pptx", ".xlsx"}:
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                prefix = {".docx": "word/", ".pptx": "ppt/", ".xlsx": "xl/"}[suffix]
                if "[Content_Types].xml" not in archive.namelist() or not any(n.startswith(prefix) for n in archive.namelist()):
                    raise ValueError("Office文档内容与扩展名不符")
                if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                    raise ValueError("文档解压内容超过100MB")
        except zipfile.BadZipFile as exc:
            raise ValueError("Office文档已损坏") from exc
    if suffix in {".txt", ".md", ".csv", ".json"}:
        data.decode("utf-8-sig")
    directory = Path(root) / "agent-documents" / owner
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{uuid4().hex}{suffix}"
    target.write_bytes(data)
    return target, DOCUMENT_TYPES[suffix]
