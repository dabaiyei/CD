"""Read-only decision evaluation against a supplied local System One endpoint."""
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from app.services.personal_routing import questions, merge_decision


CASES = [
    ("text_prompt", "帮我写东方神龙战斗的12秒视频提示词，不要生成视频", [], "output", "text"),
    ("story", "帮我生成一个故事文案", [], "output", "text"),
    ("negative", "不要生成图片，先讨论一下衣服", [], "output", "text"),
    ("image", "生成一张苹果图片", [], "output", "image"),
    ("video", "基于刚才的图片和提示词帮我生成视频", [], "output", "video"),
    ("confirm", "好了开始吧", [{"role": "assistant", "content": "已保存两步方案，第一步合成图片，第二步生成视频。"}], "plan_action", "execute"),
    ("chain", "全部开始，图片完成后自动生成视频", [], "chain", "auto"),
    ("wait", "先出图给我看，等我确认再生成视频", [], "chain", "step"),
    ("reference_only", "以后都参考第一张图", [], "output", "text"),
    ("reference_change", "用第二张图的人物替换第一张图的人物并生成图片", [], "references", "select"),
    ("continue", "继续", [{"role": "user", "content": "图片生成完后自动接着生成视频"}, {"role": "assistant", "content": "第一步图片已经完成，第二步视频待开始。"}], "plan_action", "execute"),
    ("cancel", "取消任务，不要继续生成", [], "plan_action", "cancel"),
]


async def main():
    base = os.environ["LAYA_PROBE_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["LAYA_PROBE_KEY"]}
    report = []
    requested_model = os.environ.get("LAYA_PROBE_MODEL", "multilingual")
    async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
        for name, message, history, key, expected in CASES:
            if os.environ.get("LAYA_PROBE_COMPACT") and key != "output":
                continue
            state = {"message": message, "history": history, "conversation_summary": "",
                "creation": {"type": "image", "prompt": "人物在酒吧舞台上跳舞", "options": {"aspect_ratio": "16:9"},
                    "reference_attachment_ids": ["first"], "last_generated_attachment_id": "generated"},
                "attachments": [{"id": "first", "ordinal": 1, "source": "uploaded"},
                                {"id": "second", "ordinal": 2, "source": "uploaded"}],
                "current_attachment_ids": [], "models": [], "selected_skills": False}
            if name in {"confirm", "chain", "wait", "continue", "cancel"}:
                state["media_plan"] = {"source_message_id": "draft", "index": 1 if name == "continue" else 0,
                    "status": "ready", "auto_continue": False, "steps": [
                        {"type": "image", "prompt": "人物在酒吧舞台上跳舞", "options": {}, "reference_attachment_ids": ["first"]},
                        {"type": "video", "prompt": "人物在酒吧舞台上缓慢转身并跳舞", "options": {}, "reference_attachment_ids": ["first"]}]}
            start = time.perf_counter()
            request_questions = questions(state)
            wire_state = state
            for question in request_questions.values():
                if not isinstance(question["instructions"], str):
                    question["instructions"] = json.dumps(question["instructions"], ensure_ascii=False)
            if os.environ.get("LAYA_PROBE_COMPACT"):
                wire_state = {"当前用户消息": message}
                request_questions = {"output": {"type": "choice",
                    "instructions": "判断用户本轮希望得到什么。写提示词、文案、故事和讨论属于文字；明确要求实际出图或生成视频才选对应媒体。否定生成不代表要生成。",
                    "criteria": {"text": "只回复文字或撰写提示词、故事、文案", "image": "实际生成或编辑图片", "video": "实际生成视频", "clarify": "意图不明确，需要澄清"}}}
            if os.environ.get("LAYA_PROBE_NATIVE"):
                from app.services.laya_routing import request_for
                payload = request_for(state)
                wire_state, request_questions = payload["state"], payload["questions"]
            response = await client.post(base + "/v1/systemone", headers=headers,
                json={"model": requested_model, "state": wire_state, "questions": request_questions})
            elapsed = round(time.perf_counter() - start, 3)
            if response.status_code != 200:
                print(json.dumps({"case": name, "http": response.status_code, "seconds": elapsed, "detail": response.text[:3000]}, ensure_ascii=False), flush=True)
                break
            data = response.json()
            answer = data.get("answers", {}).get(key, {})
            effective = data
            if os.environ.get("LAYA_PROBE_NATIVE"):
                from app.services.laya_routing import decisions, complete_for
                effective = decisions(state, data)
            # Test the current app contract unchanged; don't silently normalize fields.
            try:
                merged = merge_decision(state, effective)
                routed = merged.get("output")
                if os.environ.get("LAYA_PROBE_NATIVE") and not complete_for(state, effective):
                    routed = "fallback_to_jev"
            except Exception as exc:
                routed = type(exc).__name__
            item = {"case": name, "message": message, "expected": {key: expected}, "answer": answer,
                "correct": answer.get("choice") == expected, "app_output": routed,
                "seconds": elapsed, "model": data.get("model"), "usage": data.get("usage"), "response": data}
            report.append(item)
            print(json.dumps({k: v for k, v in item.items() if k != "response"}, ensure_ascii=False), flush=True)
    suffix = "-compact" if os.environ.get("LAYA_PROBE_COMPACT") else ""
    if requested_model == "auto":
        suffix += "-auto"
    if os.environ.get("LAYA_PROBE_NATIVE"):
        suffix += "-native"
    Path(f".logs/laya-routing-evaluation{suffix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Passed:", sum(item["correct"] for item in report), "/", len(report))


if __name__ == "__main__":
    asyncio.run(main())
