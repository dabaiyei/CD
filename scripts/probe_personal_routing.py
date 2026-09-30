"""Small paid Jev-only evaluation; never generates images/videos or prints secrets."""

import asyncio
import json
import time

from app.services.personal_routing import evaluate, merge_decision


async def main():
    previous = {
        "type": "image",
        "model_id": "image-model",
        "options": {"aspect_ratio": "16:9", "resolution": "2K"},
        "reference_attachment_ids": ["first"],
        "prompt": "参考第一张图生成一个穿蓝色衣服的人物",
        "rewrite": False,
    }
    cases = [
        ("帮我生成故事文案", {}, "text"),
        ("给我写一个8秒视频的提示词，不要生成视频", {}, "text"),
        ("你能生成图片吗？", {}, "text"),
        ("看看第一张图里是什么", {}, "text"),
        ("参考第一张图生成16:9的2K图片", {}, "image"),
        ("衣服换成红色，其他不变", previous, "image"),
        ("让它动起来，8秒，16:9", previous, "video"),
        ("帮我优化提示词并生成图片", previous, "image"),
        ("以后都参考第一张图", {}, "text"),
        ("写一个女孩穿红衣服的故事", previous, "text"),
    ]
    semaphore = asyncio.Semaphore(2)

    async def check(message, creation, expected):
        state = {
            "message": message,
            "creation": creation,
            "history": [],
            "attachments": [
                {"id": "first", "ordinal": 1, "source": "uploaded"},
                {"id": "second", "ordinal": 2, "source": "uploaded"},
            ],
            "current_attachment_ids": [],
            "models": [
                {
                    "id": "image-model",
                    "name": "图片模型",
                    "model_id": "image-v1",
                    "type": "image",
                }
            ],
        }
        async with semaphore:
            start = time.monotonic()
            raw = await evaluate(state)
            result = merge_decision(state, raw)
        print(
            json.dumps(
                {
                    "message": message,
                    "expected": expected,
                    "output": result["output"],
                    "seconds": round(time.monotonic() - start, 2),
                    "creation": result["creation"],
                    "answers": raw["answers"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result["output"] == expected

    results = await asyncio.gather(*(check(*case) for case in cases))
    print(f"Passed {sum(results)}/{len(results)}")
    if not all(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
