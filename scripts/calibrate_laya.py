"""Local-only labelled evaluation; never submits generation tasks or stores keys."""
import asyncio
import json
import os
import time
from pathlib import Path
import httpx
from app.services.laya_routing import request_for, decisions
from app.services.personal_routing import accepted

# First group selects gates; the second group is held out from threshold selection.
DATA = {
    "calibration": [
        ("帮我写打斗视频提示词，不要生成视频", "text"), ("帮我生成一个故事文案", "text"),
        ("不要生成图片，先讨论一下衣服", "text"), ("生成一张苹果图片", "image"),
        ("基于刚才的图片帮我生成视频", "video"), ("以后都参考第一张图", "text"),
        ("生成一张16:9的海边日落图片", "image"), ("生成一个小猫跑步的视频", "video"),
        ("解释一下图里是什么", "text"), ("视频为什么还没生成好", "text"),
        ("把图片中人物的衣服改成红色，生成图片", "image"), ("我想先了解一下视频模型价格", "text"),
        ("设计提示词并生成图片", "image"), ("只复制这段提示词", "text"),
        ("将上一张图片做成5秒视频", "video"), ("你好", "text"),
    ],
    "holdout": [
        ("写一份人物奔跑的视频提示词", "text"), ("生成一篇关于星空的故事", "text"),
        ("暂时别生成视频，我要先修改剧情", "text"), ("画一只趴在窗边的橘猫", "image"),
        ("制作一段海浪拍岸的视频", "video"), ("后续都参考第二张图片", "text"),
        ("帮我生成一张2K森林图片", "image"), ("你能看见我上传的照片吗", "text"),
        ("给我写个短视频文案", "text"), ("按刚才的图生成12秒视频", "video"),
        ("生成图片之前需要哪些参数", "text"), ("帮我优化提示词然后生成图片", "image"),
    ],
}


async def main():
    rows = []
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        for split, cases in DATA.items():
            for message, expected in cases:
                state = {"message": message, "history": [], "attachments": [], "creation": {}}
                start = time.perf_counter()
                response = await client.post(os.environ["LAYA_PROBE_URL"].rstrip("/") + "/v1/systemone",
                    headers={"Authorization": "Bearer " + os.environ["LAYA_PROBE_KEY"]}, json=request_for(state))
                response.raise_for_status()
                raw = response.json()
                row = {"split": split, "message": message, "expected": expected, "raw": raw,
                    "seconds": round(time.perf_counter() - start, 3)}
                rows.append(row)
    # Select probability AND runner-up separation by selective accuracy, not
    # Laya's entropy confidence, which has a different scale from Jev's.
    candidates = []
    for floor in (.65, .7, .75, .8, .85, .9, .95):
        for margin in (.2, .3, .4, .5):
            taken = []
            for row in rows:
                if row["split"] != "calibration":
                    continue
                answer = row["raw"]["answers"]["output"]
                probs = answer["probabilities"]
                value = answer["choice"]
                if probs[value] >= floor and probs[value] - max(v for k, v in probs.items() if k != value) >= margin:
                    taken.append(row)
            errors = sum(r["raw"]["answers"]["output"]["choice"] != r["expected"] for r in taken)
            if not errors:
                candidates.append((len(taken), floor, margin))
    best = max(candidates, key=lambda x: (x[0], x[1], x[2])) if candidates else None
    from app.services import laya_routing
    if best:
        laya_routing.GATES["output"] = best[1:]
    for row in rows:
        state = {"message": row["message"], "history": [], "attachments": []}
        result = decisions(state, row["raw"])
        row["decision"] = accepted(result["answers"], "output", {"text", "image", "video", "clarify"}) or "abstain"
    report = {"selected_output_gate": best, "rows": rows}
    Path(".logs/laya-calibration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("gate", best)
    for split in DATA:
        selected = [r for r in rows if r["split"] == split]
        print(split, "raw correct", sum(r['raw']['answers']['output']['choice']==r['expected'] for r in selected),
            "adapter correct", sum(r['decision']==r['expected'] for r in selected), "total", len(selected))
        for r in selected:
            if r['decision'] != r['expected']:
                print(r['message'], 'expected', r['expected'], 'decision', r['decision'], r['raw']['answers']['output'])


asyncio.run(main())
