"""Resume a failed video-prompt workflow through the normal chapter service."""
import asyncio
import argparse
import json

from app.db.models import Chapter, User, DirectorWorkflowRun
from app.db.session import SessionLocal, engine
from app.services.director_orchestration import start_automatic_workflow_from_progress


async def main(identity):
    try:
        async with SessionLocal() as session:
            old = await session.get(DirectorWorkflowRun, identity)
            if old is None or str(old.status) != "failed" or old.stop_requested:
                raise ValueError("Expected a failed, non-cancelled workflow")
            chapter = await session.get(Chapter, old.chapter_id)
            user = await session.get(User, old.user_id)
            if chapter is None or user is None or chapter.user_id != user.id:
                raise ValueError("Chapter owner mismatch")
            workflow = await start_automatic_workflow_from_progress(session,
                chapter=chapter, user=user, instruction="修复旧版输入长度拦截后，从当前已保存进度继续全自动制作，仅生成缺失视频提示词。",
                chat_session_id=old.chat_session_id)
            print(json.dumps({"workflow": workflow.id, "status": str(workflow.status), "stage": str(workflow.stage)}, ensure_ascii=False))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_id")
    asyncio.run(main(parser.parse_args().workflow_id))
