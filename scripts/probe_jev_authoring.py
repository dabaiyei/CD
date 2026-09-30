"""Live authoring decisions only; no media or project writes."""

import asyncio

from app.db.models import User, UserRole
from app.db.session import SessionLocal, engine
from app.services import jev_control
from app.services.personal_creation_guidance import controlled_guidance
from sqlalchemy import select


async def main():
    async with SessionLocal() as db:
        tenant = await db.scalar(
            select(User.tenant_id).where(
                User.role == UserRole.ADMIN, User.is_active.is_(True)
            )
        )
    original = jev_control.post

    async def traced(config, payload):
        reply = await original(config, payload)
        print(
            {
                k: (v.get("choice"), v.get("confidence"))
                for k, v in reply.get("answers", {}).items()
            },
            flush=True,
        )
        return reply

    jev_control.post = traced
    cases = [
        ("今天星期几", [], False, False),
        (
            "写一个视频提示词：两位剑客高速交锋，连续格挡反击，不要实际生成视频",
            [],
            True,
            True,
        ),
        ("写视频提示词：两个人坐着回忆昨天的战斗，现在不交手", [], True, False),
        ("原样提交以下视频提示词，不要修改：两位剑客交锋", [], False, False),
        (
            "继续优化这个方案",
            [{"role": "user", "content": "帮我写两位剑客高速追逐交锋的视频提示词"}],
            True,
            True,
        ),
        (
            "写视频提示词：空荡荡的山谷，没有人物，瀑布落入深潭，没有旁白",
            [],
            True,
            False,
        ),
    ]
    failures = []
    try:
        for message, history, expected_rules, expected_combat in cases:
            try:
                rules = await controlled_guidance(
                    tenant, message, recent_messages=history
                )
                actual = (bool(rules), "ACT视角" in rules)
                print(message, actual, flush=True)
                if actual != (expected_rules, expected_combat):
                    failures.append(message)
            except jev_control.JevDecisionPending as exc:
                print(str(exc), flush=True)
                failures.append(message)
        assert not failures, failures
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
