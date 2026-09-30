"""Small live control probes; no creative models, media or project mutations."""

import asyncio
import json
import time

from app.db.models import User, UserRole
from app.db.session import SessionLocal, engine
from app.services import jev_control
from app.services.jev_control import (
    chat_intent,
    controller,
    local_review,
    modules,
    repair_fields,
)
from sqlalchemy import select


async def main():
    original_post = jev_control.post

    async def traced_post(config, payload):
        reply = await original_post(config, payload)
        print(
            json.dumps(
                {"stage": payload["state"]["stage"], "answers": reply.get("answers")},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return reply

    jev_control.post = traced_post
    async with SessionLocal() as db:
        user = await db.scalar(
            select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        )
        tenant = user.tenant_id
    control = await controller(tenant)
    assert control is not None
    cases = [
        ("只讨论如何生成视频，不要创建任何任务", "discuss"),
        ("请在当前章节创建分镜", "create"),
        ("请把当前章节第3镜的衣服改成蓝色，其它不变", "revise"),
    ]
    for message, expected in cases:
        t = time.monotonic()
        actual = await chat_intent(control, {"message": message, "history": []})
        print(
            json.dumps(
                {
                    "kind": "intent",
                    "expected": expected,
                    "actual": actual,
                    "seconds": round(time.monotonic() - t, 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert actual == expected
    for scene, action, conflict in [
        (
            "同一女子全程穿白衣，服装始终不变。",
            "同一女子全程穿红衣，服装始终不变。",
            True,
        ),
        ("女子入镜时坐在椅子上。", "女子先坐着，随后站起身，连续走到门口。", False),
        ("阿青右手持剑。", "阿青右手持剑，左手扶住门框。", False),
        ("阿青右手持剑。", "阿青先右手持剑，随后把剑交到左手。", False),
    ]:
        actual = await local_review(
            control,
            {
                "order_index": 3,
                "scene_description": scene,
                "action_description": action,
            },
        )
        print(
            json.dumps(
                {
                    "kind": "local_review",
                    "expected_conflict": conflict,
                    "actual_conflict": bool(actual),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert bool(actual) == conflict
    multi = await local_review(
        control,
        {
            "order_index": 4,
            "scene_description": "同一女子全程穿白衣，站在门左侧。",
            "action_description": "白衣女子站在门左侧，抬起右手。",
            "image_prompt": "同一白衣女子站在门左侧，右手自然下垂，作为抬手前的首帧。",
            "frame_layout": "女子位于门左侧。",
        },
    )
    print(
        json.dumps(
            {"kind": "multi_pair_review", "findings": multi}, ensure_ascii=False
        ),
        flush=True,
    )
    assert not multi
    for action, combat in [
        ("两位剑客实力相当，高速连续挥剑、格挡、反击。", "exchange"),
        ("两个人坐着聊天，谈论昨日的打斗，当前没有交手。", "none"),
        ("空荡荡的山谷，没有人物。瀑布落入深潭，没有旁白。", "none"),
    ]:
        actual = await modules(control, {"action_description": action, "dialogue": ""})
        print(
            json.dumps(
                {"kind": "modules", "expected_combat": combat, "actual": actual},
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert actual["combat"] == combat
    fields = await repair_fields(
        control,
        {
            "order_index": 3,
            "scene_description": "白衣女子",
            "image_prompt": "红衣女子",
            "action_description": "白衣女子走到门口",
            "dialogue": "早上好",
        },
        [{"issue": "image_prompt服装应与scene_description白衣一致"}],
        ["image_prompt", "scene_description", "action_description", "dialogue"],
        ["image_prompt"],
    )
    print(
        json.dumps({"kind": "repair_scope", "fields": fields}, ensure_ascii=False),
        flush=True,
    )
    assert fields == ["image_prompt"]
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
