"""Local, scoped retrieval tools. No shell, arbitrary flags, URLs or write rules."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

PAGE = 12_000
MAX_FILE_BYTES = 32 * 1024 * 1024


def result(value, *, error=False):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ToolChunk(content=[TextBlock(text=text)],
                     state=ToolResultState.ERROR if error else ToolResultState.SUCCESS)


class ScopedTool(ToolBase):
    is_read_only = True
    is_concurrency_safe = True

    def __init__(self, workspace: Path):
        super().__init__()
        self.workspace = workspace.resolve()

    def resolve(self, path: str, *, file=False):
        candidate = Path(path)
        target = (candidate if candidate.is_absolute() else self.workspace / candidate).resolve()
        if not target.is_relative_to(self.workspace):
            raise PermissionError("路径不属于当前任务工作区")
        if not target.exists() or (file and not target.is_file()):
            raise ValueError("指定文件或目录不存在")
        if file and target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("文件超过32MB，请先拆分文档")
        return target

    async def check_permissions(self, tool_input, context):
        self.resolve(tool_input.get("path", "."))
        return PermissionDecision(behavior=PermissionBehavior.PASSTHROUGH,
                                  message="仅检索当前任务授权文件")


async def command(args, *, timeout=20, max_bytes=256_000):
    """Bound output while the process runs, and reap it on cancellation."""
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.STDOUT,
                                                  env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                                       "PYTHONPATH": str(Path(__file__).resolve().parents[1])
                                                       + os.pathsep + os.environ.get("PYTHONPATH", "")})
    async def collect():
        output = bytearray()
        while block := await process.stdout.read(4096):
            output.extend(block)
            if len(output) > max_bytes:
                raise ValueError("检索结果过多，请缩小路径、模式或匹配范围")
        await process.wait()
        return process.returncode, output.decode("utf-8", errors="replace")
    try:
        return await asyncio.wait_for(collect(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class Ripgrep(ScopedTool):
    name = "Ripgrep"
    description = ("Use ripgrep to locate text in authorized script, storyboard and skill files. "
                   "Returns bounded path:line matches. Start narrow, then use Read on relevant lines. "
                   "No shell flags. Set files_only to find matching filenames. Long lines are previews, not complete evidence.")
    input_schema = {"type": "object", "properties": {
        "path": {"type": "string", "description": "Workspace path, default '.'"},
        "pattern": {"type": "string"}, "glob": {"type": "string"},
        "literal": {"type": "boolean"}, "files_only": {"type": "boolean"},
        "offset": {"type": "integer", "minimum": 0, "maximum": 1000},
    }, "required": ["pattern"], "additionalProperties": False}

    async def call(self, pattern: str, path=".", glob="", literal=True, files_only=False, offset=0):
        try:
            target = self.resolve(path)
            executable = shutil.which("rg")
            if not executable:
                raise ValueError("Runtime 缺少 ripgrep，请重新安装运行时依赖")
            if len(pattern) > 2000 or len(glob) > 300 or not 0 <= offset <= 1000:
                raise ValueError("检索参数超出限制")
            args = [executable, "--hidden", "--no-ignore", "--no-config", "--color", "never",
                    "--max-filesize", "32M", "--max-columns", "500", "--max-columns-preview",
                    "--glob", "!.cineforge/execution-manifest.json", "--glob", "!*.pdf",
                    "--glob", "!*.docx", "--glob", "!*.pptx", "--glob", "!*.xlsx",
                    "--files-with-matches" if files_only else "--line-number"]
            if literal:
                args.append("--fixed-strings")
            if glob:
                args.extend(["--glob", glob])
            code, text = await command([*args, "--", pattern, str(target)])
            if code not in {0, 1}:
                raise ValueError(text[:1000])
            lines = text.splitlines()
            page = "\n".join(lines[offset:offset + 30])[:PAGE]
            return result({"matches": page, "next_offset": offset + 30 if len(lines) > offset + 30 else None,
                           "note": "最多30条，长行仅预览；用 Read 读取原文。"})
        except (OSError, ValueError, PermissionError, asyncio.TimeoutError) as exc:
            return result(str(exc) or "检索超时，请缩小范围", error=True)


class AstGrep(ScopedTool):
    name = "AstGrep"
    description = ("ast-grep syntax-aware search of one authorized structured file. "
                   "For JSON use language=json, pattern such as '\"dialogue\": $VALUE' or "
                   "kind=pair; locate a shot file with Ripgrep first. Returns ranges and matches; never rewrites files.")
    input_schema = {"type": "object", "properties": {
        "path": {"type": "string"}, "pattern": {"type": "string"},
        "language": {"type": "string", "enum": ["json", "javascript", "typescript", "python", "html", "css"]},
        "kind": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
    }, "required": ["path", "language"], "additionalProperties": False}

    async def call(self, path, language, pattern="", kind="", offset=0):
        try:
            target = self.resolve(path, file=True)
            if language not in {"json", "javascript", "typescript", "python", "html", "css"}:
                raise ValueError("不支持此结构语言")
            if not (pattern or kind) or len(pattern) > 2000 or not 0 <= offset <= 1000:
                raise ValueError("请提供结构匹配 pattern 或 kind，并限制分页范围")
            # Native parsing is isolated so pathological input cannot hang the event loop.
            code, output = await command([sys.executable, "-m", "runtime.retrieval_worker", "ast",
                str(target), language, pattern, kind, str(offset)], timeout=15)
            if code:
                raise ValueError(output[:1000])
            return result(json.loads(output))
        except (OSError, ValueError, PermissionError, asyncio.TimeoutError) as exc:
            return result(str(exc) or "结构检索超时", error=True)


class MarkItDownTool(ScopedTool):
    name = "MarkItDown"
    description = ("Convert an authorized local PDF, DOCX, PPTX, XLSX, HTML or text document to Markdown "
                   "using MarkItDown. No URL fetching, plugins, OCR or external model. Returns a cached Markdown path "
                   "and a 12000-character page. Search the converted file with Ripgrep, then Read relevant passages.")
    input_schema = {"type": "object", "properties": {
        "path": {"type": "string"}, "char_offset": {"type": "integer", "minimum": 0},
    }, "required": ["path"], "additionalProperties": False}

    async def call(self, path, char_offset=0):
        try:
            target = self.resolve(path, file=True)
            if target.suffix.lower() not in {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".txt", ".md", ".csv", ".json"}:
                raise ValueError("不支持此文档格式")
            if char_offset < 0:
                raise ValueError("char_offset 不得为负数")
            digest = await asyncio.to_thread(lambda: hashlib.sha256(target.read_bytes()).hexdigest())
            cache = self.workspace / ".cineforge" / "converted"
            cache.mkdir(parents=True, exist_ok=True)
            output = cache / f"{digest}.md"
            self.resolve(cache)
            if output.is_symlink():
                raise PermissionError("转换缓存不可为符号链接")
            if not output.exists():
                code, message = await command([sys.executable, "-m", "runtime.retrieval_worker",
                    "convert", str(target), str(output)], timeout=45)
                if code:
                    raise ValueError(message[:1000])
            text = await asyncio.to_thread(output.read_text, encoding="utf-8")
            end = min(char_offset + PAGE, len(text))
            return result({"path": str(output), "text": text[char_offset:end],
                           "characters": len(text), "next_char_offset": end if end < len(text) else None})
        except (OSError, ValueError, PermissionError, asyncio.TimeoutError) as exc:
            return result(str(exc) or "文档转换超时", error=True)
