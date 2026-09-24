"""Killable subprocess for native parsing and document conversion."""
import json
import re
from pathlib import Path
import sys
import zipfile


def main():
    operation, path, *args = sys.argv[1:]
    source = Path(path)
    if source.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("文件超过32MB")
    if operation == "ast":
        from ast_grep_py import SgRoot
        language, pattern, kind, offset = args
        rule = {**({"pattern": pattern} if pattern else {}), **({"kind": kind} if kind else {})}
        if language == "json" and pattern and not pattern.lstrip().startswith(("{", "[")) and ":" in pattern:
            field_match = re.fullmatch(r'\s*("(?:[^"\\]|\\.)*")\s*:\s*\$[A-Z_]+\s*', pattern)
            if field_match:
                rule = {"kind": "pair", "has": {"field": "key", "regex": "^" + re.escape(field_match[1]) + "$"}}
            else:
                rule["pattern"] = {"context": "{" + pattern + "}", "selector": "pair"}
        matches = SgRoot(source.read_text(encoding="utf-8"), language).root().find_all(**rule)
        rows, used = [], 0
        for match in matches[int(offset):int(offset) + 20]:
            span = match.range()
            text = match.text()
            preview = text[:min(2000, 10000 - used)]
            rows.append({"text": preview, "truncated": len(preview) < len(text),
                         "start_line": span.start.line + 1, "end_line": span.end.line + 1,
                         "start_column": span.start.column, "end_column": span.end.column})
            used += len(preview)
            if used >= 10000:
                break
        next_offset = int(offset) + len(rows)
        print(json.dumps({"matches": rows, "next_offset": next_offset if next_offset < len(matches) else None}, ensure_ascii=False))
    elif operation == "convert":
        from markitdown import MarkItDown
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                if sum(info.file_size for info in archive.infolist()) > 100 * 1024 * 1024:
                    raise ValueError("文档解压内容超过100MB")
        text = MarkItDown(enable_plugins=False).convert_local(str(source)).text_content
        if len(text) > 8_000_000:
            raise ValueError("转换文本超过800万字符，请拆分文档")
        output = Path(args[0])
        import os
        import tempfile
        handle, temporary = tempfile.mkstemp(dir=output.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, output)
        finally:
            Path(temporary).unlink(missing_ok=True)
    else:
        raise ValueError("未知检索操作")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}")
        sys.exit(1)
