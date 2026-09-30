"""Start a saved proposal only when the latest user message explicitly confirms it."""
import argparse
import asyncio
from sqlalchemy import select, text
from app.db.session import SessionLocal, engine
from app.db.models import AgentChatSession, AgentChatMessage, AgentMessageRole, AITask, User, TaskStatus
from app.services.personal_media_plan import execution_confirmation, save_proposed_plan, queue_plan_step
from app.services.task_queue import enqueue_task


async def main(session_id):
    try:
        async with SessionLocal() as db:
            if db.bind.dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            chat = await db.get(AgentChatSession, session_id)
            assert chat
            messages = (await db.scalars(select(AgentChatMessage).where(
                AgentChatMessage.session_id == chat.id,
            ).order_by(AgentChatMessage.created_at.desc()).limit(2))).all()
            assert len(messages) == 2
            assistant, request = messages
            assert assistant.role == AgentMessageRole.ASSISTANT and request.role == AgentMessageRole.USER
            assert execution_confirmation(request.content), "Latest message must authorize execution"
            task = await db.get(AITask, assistant.run_id)
            assert task and task.status == TaskStatus.SUCCEEDED
            assert not (task.result_payload or {}).get("media_plan_next_task_id"), "Already submitted"
            active = await db.scalar(select(AITask.id).where(
                AITask.user_id == chat.user_id,
                AITask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING]),
                AITask.request_payload["agent_chat_session_id"].as_string() == chat.id,
            ))
            assert not active, "Session already has active work"
            plan = await save_proposed_plan(db, chat, assistant)
            assert plan and not plan.get("completed")
            user = await db.get(User, chat.user_id)
            queued = await queue_plan_step(db, task, chat, user, plan)
            assert queued
            await db.commit()
            await enqueue_task(queued[0].id)
            print("Queued first step:", queued[0].id, "auto_continue:", plan["auto_continue"])
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    asyncio.run(main(parser.parse_args().session_id))
