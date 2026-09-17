import json
from typing import Any


def video_prompt_batches(rows: list[dict[str, Any]], *, max_chars: int = 24000) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    for row in rows:
        if len(json.dumps([row], ensure_ascii=False)) > max_chars:
            raise RuntimeError(
                f"镜头 {row.get('order_index', '')} 的视频提示词输入过长，请精简该镜头描述或资产提示词后重试"
            )
        candidate = [*batch, row]
        if batch and (len(candidate) > 4 or len(json.dumps(candidate, ensure_ascii=False)) > max_chars):
            batches.append(batch)
            batch = []
        batch.append(row)
    if batch:
        batches.append(batch)
    return batches
