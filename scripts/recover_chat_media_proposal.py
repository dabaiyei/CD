"""Restore a specified saved assistant proposal; never submit media tasks."""
import argparse
import asyncio

from app.db.session import SessionLocal, engine
from app.db.models import AgentChatMessage, AgentChatSession
from app.services.personal_media_plan import save_proposed_plan


async def main(message_id, apply):
    async with SessionLocal() as db:
        message = await db.get(AgentChatMessage, message_id)
        assert message and message.role.value == "assistant"
        chat = await db.get(AgentChatSession, message.session_id)
        previous = (chat.runtime_manifest or {}).get("media_plan") or {}
        assert previous.get("status") in {None, "completed", "cancelled"}, "Do not replace pending work"
        plan = await save_proposed_plan(db, chat, message)
        assert plan and len(plan["steps"]) > 0
        print("Recovered proposal:", plan["source_message_id"], "steps:", len(plan["steps"]))
        if apply:
            await db.commit()
            print("Saved. No paid generation submitted.")
        else:
            await db.rollback()
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("message_id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.message_id, args.apply))
