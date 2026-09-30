"""Compare Laya auto and configured Jev on a translated English intent set."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import time

import httpx
from sqlalchemy import select
from app.db.models import JevConfiguration
from app.db.session import SessionLocal, engine
from app.services.jev_configuration import environment_configuration, get_jev_configuration
from app.services.laya_routing import request_for, GATES
from app.services.personal_routing import evaluate as evaluate_jev

OUTPUT = [
    ("Write a video prompt for a heroine chasing someone at night; don't render the video yet.", "text"),
    ("Expand this movie premise into a 300-word synopsis.", "text"),
    ("Analyze the visual style in this image; no image generation.", "text"),
    ("What does the image model cost?", "text"),
    ("Did the video generation fail? Check its progress.", "text"),
    ("Translate this image prompt for me.", "text"),
    ("How many people are in this photo?", "text"),
    ("Use the third photo as a character reference from now on.", "text"),
    ("How can I turn an image into a video?", "text"),
    ("List a few themes suitable for image generation.", "text"),
    ("Don't make an image; just write the prompt.", "text"),
    ("Hi, what can you do?", "text"),
    ("Generate a 4K image of sunrise over snowy mountains.", "image"),
    ("Draw a watercolor cottage beside a lake.", "image"),
    ("Make a clean product image of white sneakers.", "image"),
    ("Create a 16:9 sci-fi city poster.", "image"),
    ("Generate a photo of a puppy wearing sunglasses.", "image"),
    ("Please generate an image of a wooden bridge in a forest.", "image"),
    ("Generate an 8-second video of an astronaut walking.", "video"),
    ("Make a 12-second video of a waterfall.", "video"),
    ("Generate a video of a small boat gliding across a lake.", "video"),
    ("Create a short clip of fireworks bursting in the sky.", "video"),
    ("Generate a video of two people exchanging rapid sword strikes.", "video"),
    ("Make a 5-second video of a cat stretching.", "video"),
    ("Write the prompt and generate an image of a city in the rain.", "image"),
    ("Design the action, then generate a martial arts video.", "video"),
    ("If I asked for an image, which model would you use?", "text"),
    ("Don't generate an image; instead, make a video of ocean waves.", "video"),
    ("I'm not asking for a story; generate an image of a fox.", "image"),
    ("Only analyze the video you made earlier for visual glitches.", "text"),
]
PLANS = [
    ("Run every step in the plan.", "execute", "image"),
    ("Okay, start now.", "execute", "image"),
    ("Continue with the next step.", "execute", "video"),
    ("Cancel the current task.", "cancel", "text"),
    ("Cancel the task; don't keep generating.", "cancel", "text"),
    ("Don't start yet; I need to change the plan.", "wait", "text"),
    ("What does cancel mean? Explain it first.", "wait", "text"),
    ("Execute every step in the plan.", "execute", "image"),
    ("Don't auto-continue; wait for my confirmation.", "wait", "text"),
    ("Just write the video prompt for now.", "wait", "text"),
]


def state_for(message, *, plan=False, video=False):
    state = {"message": message, "history": [], "creation": {}, "attachments": [],
        "current_attachment_ids": [], "models": [], "selected_skills": False}
    if plan:
        state["media_plan"] = {"source_message_id": "draft", "status": "ready", "index": int(video),
            "auto_continue": False, "steps": [
                {"type": kind, "prompt": "A person walks along a beach, maintaining identity and clothing.",
                 "options": {}, "reference_attachment_ids": []} for kind in ("image", "video")]}
    return state


async def main():
    async with SessionLocal() as db:
        stored = await db.scalar(select(JevConfiguration).where(JevConfiguration.enabled.is_(True)).limit(1))
        jev = await get_jev_configuration(db, stored.tenant_id) if stored else environment_configuration()
    if not jev.enabled or not jev.api_key:
        raise RuntimeError("No enabled Jev configuration is available")
    print("Comparing Laya model=auto with Jev", jev.model, flush=True)
    rows = []
    samples = [("output", message, label, label, state_for(message)) for message, label in OUTPUT]
    samples += [("plan_action", message, action, out, state_for(message, plan=True, video=out == "video"))
                for message, action, out in PLANS]
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        for key, message, expected_choice, expected_output, state in samples:
            payload = request_for(state, language="en")
            start = time.perf_counter()
            response = await client.post(os.environ["LAYA_PROBE_URL"].rstrip("/") + "/v1/systemone",
                headers={"Authorization": "Bearer " + os.environ["LAYA_PROBE_KEY"]}, json=payload)
            response.raise_for_status()
            laya = response.json()
            laya_seconds = time.perf_counter() - start
            laya_answer = laya.get("answers", {}).get(key, {})
            probs = laya_answer.get("probabilities", {})
            value = laya_answer.get("choice")
            floor, margin = GATES[key]
            accepted_laya = (isinstance(probs.get(value), (int, float))
                and probs[value] >= floor
                and probs[value] - max((v for k, v in probs.items() if k != value), default=1) >= margin)

            start = time.perf_counter()
            jev_raw = await evaluate_jev(state, config=jev, question_language="en")
            jev_seconds = time.perf_counter() - start
            jev_answer = jev_raw.get("answers", {}).get(key, {})
            rows.append({"message": message, "key": key, "expected": expected_choice,
                "expected_output": expected_output, "laya_choice": value,
                "laya_correct": value == expected_choice, "laya_accepted": accepted_laya,
                "laya_accepted_correct": accepted_laya and value == expected_choice,
                "laya_probabilities": probs, "laya_model": laya.get("model"),
                "laya_seconds": round(laya_seconds, 3), "laya_usage": laya.get("usage"),
                "jev_choice": jev_answer.get("choice"), "jev_correct": jev_answer.get("choice") == expected_choice,
                "jev_confidence": jev_answer.get("confidence"), "jev_output": jev_raw.get("model"),
                "jev_seconds": round(jev_seconds, 3)})

    summary = {"n": len(rows), "laya_raw_correct": sum(r["laya_correct"] for r in rows),
        "laya_accepted": sum(r["laya_accepted"] for r in rows),
        "laya_accepted_correct": sum(r["laya_accepted_correct"] for r in rows),
        "laya_mean_seconds": round(statistics.mean(r["laya_seconds"] for r in rows), 3),
        "laya_max_seconds": max(r["laya_seconds"] for r in rows),
        "jev_raw_correct": sum(r["jev_correct"] for r in rows),
        "jev_mean_seconds": round(statistics.mean(r["jev_seconds"] for r in rows), 3),
        "jev_max_seconds": max(r["jev_seconds"] for r in rows)}
    for key in ("output", "plan_action"):
        subset = [r for r in rows if r["key"] == key]
        summary[key] = {"n": len(subset), "laya_correct": sum(r["laya_correct"] for r in subset),
            "laya_accepted": sum(r["laya_accepted"] for r in subset),
            "jev_correct": sum(r["jev_correct"] for r in subset)}
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "language": "English",
        "jev_model": jev.model, "laya_gates": GATES, "summary": summary, "rows": rows,
        "note": "Same 40 translated intent cases; model classification only, no media tasks submitted."}
    Path(".logs/laya-english-comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    for row in rows:
        if row["laya_accepted"] and not row["laya_correct"]:
            print("LAYA ACCEPTED WRONG:", row["message"], row["expected"], row["laya_choice"], row["laya_probabilities"])
        if not row["jev_correct"]:
            print("JEV WRONG:", row["message"], row["expected"], row["jev_choice"], row["jev_confidence"])
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
