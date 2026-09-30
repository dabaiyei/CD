"""Compare the fixed Laya adapter and configured Jev on identical intent cases."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import time

import httpx
from app.services.laya_routing import request_for, decisions, complete_for, GATES
from app.services.personal_routing import merge_decision
from app.services.personal_routing import evaluate as evaluate_jev
from app.services.jev_configuration import environment_configuration, get_jev_configuration
from app.db.session import SessionLocal, engine
from app.db.models import JevConfiguration
from sqlalchemy import select


CASES = [
    ("请写一段女侠雨夜追逐的视频提示词，先别出视频", "text"),
    ("把这个电影故事扩写成三百字文案", "text"),
    ("仅分析这张图的画风，不需要生图", "text"),
    ("生图模型的计费方式是什么？", "text"),
    ("生成视频失败了吗，查一下进度", "text"),
    ("帮我翻译这段图片提示词", "text"),
    ("这张照片里有几个人？", "text"),
    ("以后以第三张照片为人物参考", "text"),
    ("怎么才能让图片变成视频？", "text"),
    ("给我列几个适合生图的主题", "text"),
    ("不要出图，只要写提示词", "text"),
    ("你好，介绍一下你的功能", "text"),
    ("生成一张雪山日出的图片，4K", "image"),
    ("画一幅水彩风格的湖边小屋", "image"),
    ("帮我出一张白底运动鞋产品图", "image"),
    ("做一张16:9的科幻城市海报", "image"),
    ("生成一张小狗戴着墨镜的照片", "image"),
    ("请生成一张森林里的木桥图片", "image"),
    ("帮我生成一段宇航员漫步的视频，8秒", "video"),
    ("制作一个12秒的瀑布视频", "video"),
    ("生成视频：小船缓缓驶过湖面", "video"),
    ("出一段烟花绽放的短片", "video"),
    ("生成一个两个人连续挥剑交锋的视频", "video"),
    ("帮我生成猫咪伸懒腰的5秒视频", "video"),
    ("写提示词并生成一张雨中的城市图片", "image"),
    ("先设计动作再生成一个武打视频", "video"),
    ("如果我说生成图片，你会使用哪个模型？", "text"),
    ("不要生成图片了，改为生成一个海浪视频", "video"),
    ("不是让你写故事，是让你生成一张狐狸图片", "image"),
    ("只分析刚才生成的视频有没有穿模", "text"),
]
PLAN_CASES = [
    ("全部执行吧", "execute", "image"),
    ("好了开始吧", "execute", "image"),
    ("继续下一步", "execute", "video"),
    ("请取消当前任务", "cancel", "text"),
    ("取消任务，不要继续生成", "cancel", "text"),
    ("先别开始，我还要改一下方案", "wait", "text"),
    ("取消是什么意思？先给我解释一下", "wait", "text"),
    ("按方案执行完全部步骤", "execute", "image"),
    ("暂时不要自动续接，等我确认", "wait", "text"),
    ("先帮我写视频提示词", "wait", "text"),
]


def make_state(message, plan=False, next_video=False):
    value = {"message": message, "history": [], "creation": {}, "attachments": [],
        "current_attachment_ids": [], "models": [], "selected_skills": False}
    if plan:
        value["media_plan"] = {"source_message_id": "draft", "status": "ready", "index": int(next_video),
            "auto_continue": False, "steps": [
                {"type": kind, "prompt": "海边人物缓慢行走，保持身份与服装一致", "options": {},
                 "reference_attachment_ids": [], "rewrite": False} for kind in ["image", "video"]]}
    return value


async def main():
    rows = []
    async with SessionLocal() as db:
        stored = await db.scalar(select(JevConfiguration).where(JevConfiguration.enabled.is_(True)).limit(1))
        jev_config = await get_jev_configuration(db, stored.tenant_id) if stored else environment_configuration()
    if not jev_config.enabled or not jev_config.api_key:
        raise RuntimeError("当前环境未配置可用的 JEV Key")
    print("JEV model:", jev_config.model, "; route threshold:", jev_config.route_confidence, flush=True)
    samples = [("output", m, target, target, make_state(m)) for m, target in CASES]
    samples += [("plan_action", m, target, output, make_state(m, True, output == "video"))
                for m, target, output in PLAN_CASES]
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        for key, message, expected_raw, expected_output, state in samples:
            payload = request_for(state)
            start = time.perf_counter()
            response = await client.post(os.environ["LAYA_PROBE_URL"].rstrip("/") + "/v1/systemone",
                headers={"Authorization": "Bearer " + os.environ["LAYA_PROBE_KEY"]}, json=payload)
            response.raise_for_status()
            raw = response.json()
            elapsed = time.perf_counter() - start
            routed = decisions(state, raw)
            handled = complete_for(state, routed)
            merged = merge_decision(state, routed) if handled else None
            # Check cancellation really cancels, not merely returning text.
            correct = bool(merged and merged.get("output") == expected_output)
            if correct and expected_raw == "cancel":
                correct = merged.get("media_plan", {}).get("status") == "cancelled"
            actual_raw = raw.get("answers", {}).get(key, {}).get("choice")
            jev_start = time.perf_counter()
            jev_raw = await evaluate_jev(state, config=jev_config)
            jev_elapsed = time.perf_counter() - jev_start
            jev_merged = merge_decision(state, jev_raw, threshold=jev_config.route_confidence)
            jev_correct = jev_merged.get("output") == expected_output
            if jev_correct and expected_raw == "cancel":
                jev_correct = jev_merged.get("media_plan", {}).get("status") == "cancelled"
            jev_answer = jev_raw.get("answers", {}).get(key, {})
            rows.append({"message": message, "question": key, "expected_raw": expected_raw,
                "expected_output": expected_output, "raw_correct": actual_raw == expected_raw,
                "local_handled": handled, "local_correct": correct,
                "actual_output": merged.get("output") if merged else "fallback_to_jev",
                "laya_seconds": round(elapsed, 3), "payload": payload, "raw": raw,
                "jev_raw_correct": jev_answer.get("choice") == expected_raw,
                "jev_correct": jev_correct, "jev_output": jev_merged.get("output"),
                "jev_seconds": round(jev_elapsed, 3), "jev_raw": jev_raw})
    summary = {"samples": len(rows), "laya_raw_correct": sum(r["raw_correct"] for r in rows),
        "local_handled": sum(r["local_handled"] for r in rows),
        "local_correct": sum(r["local_correct"] for r in rows),
        "local_wrong": sum(r["local_handled"] and not r["local_correct"] for r in rows),
        "fallback": sum(not r["local_handled"] for r in rows),
        "laya_mean_seconds": round(statistics.mean(r["laya_seconds"] for r in rows), 3),
        "laya_max_seconds": max(r["laya_seconds"] for r in rows),
        "jev_raw_correct": sum(r["jev_raw_correct"] for r in rows),
        "jev_routed_correct": sum(r["jev_correct"] for r in rows),
        "jev_mean_seconds": round(statistics.mean(r["jev_seconds"] for r in rows), 3),
        "jev_max_seconds": max(r["jev_seconds"] for r in rows)}
    for key in ["output", "plan_action"]:
        subset = [r for r in rows if r["question"] == key]
        summary[key] = {"samples": len(subset), "laya_raw_correct": sum(r["raw_correct"] for r in subset),
            "jev_raw_correct": sum(r["jev_raw_correct"] for r in subset),
            "laya_routed_correct": sum(r["local_correct"] for r in subset),
            "jev_routed_correct": sum(r["jev_correct"] for r in subset)}
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "gates": deepcopy(GATES),
        "summary": summary, "rows": rows, "jev_model": jev_config.model,
        "note": "Same cases evaluated independently against Laya and Jev; no media tasks submitted."}
    Path(".logs/laya-latest-holdout.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    for row in rows:
        if row["local_handled"] and not row["local_correct"]:
            print("WRONG", row["message"], "expected", row["expected_output"], "actual", row["actual_output"])
        if not row["jev_correct"]:
            print("JEV WRONG", row["message"], "expected", row["expected_output"], "actual", row["jev_output"])
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
