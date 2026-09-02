from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.routes import (
    admin,
    agent_chat,
    assets,
    auth,
    director,
    director_workflows,
    dubbing,
    finishing,
    memories,
    notifications,
    pricing,
    projects,
    skills,
    storyboards,
    tasks,
    user_skills,
)
from app.core.config import get_settings
from app.db.migration_guard import assert_database_current
from app.db.seed import seed_demo_data
from app.db.session import init_db
from app.services.object_storage import (
    close_object_storage,
    materialize_media_file,
    object_storage,
    valid_media_signature,
    validate_object_key,
)
from app.services.task_queue import close_redis

settings = get_settings()
mimetypes.add_type("image/webp", ".webp")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings.uploads_root.mkdir(parents=True, exist_ok=True)
    await object_storage().ensure_ready()
    if settings.app_env == "production":
        await assert_database_current()
    else:
        await init_db()
    if settings.seed_demo_data:
        await seed_demo_data()
    yield
    await close_object_storage()
    await close_redis()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(projects.router, prefix=settings.api_prefix)
app.include_router(director.router, prefix=settings.api_prefix)
app.include_router(director_workflows.router, prefix=settings.api_prefix)
app.include_router(assets.router, prefix=settings.api_prefix)
app.include_router(storyboards.router, prefix=settings.api_prefix)
app.include_router(dubbing.router, prefix=settings.api_prefix)
app.include_router(finishing.router, prefix=settings.api_prefix)
app.include_router(agent_chat.router, prefix=settings.api_prefix)
app.include_router(agent_chat.personal_router, prefix=settings.api_prefix)
app.include_router(memories.router, prefix=settings.api_prefix)
app.include_router(tasks.router, prefix=settings.api_prefix)
app.include_router(notifications.router, prefix=settings.api_prefix)
app.include_router(pricing.router, prefix=settings.api_prefix)
app.include_router(user_skills.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)
app.include_router(skills.router, prefix=settings.api_prefix)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "cineforge-api", "version": "0.1.0"}


@app.get("/uploads/{object_key:path}", include_in_schema=False)
async def serve_media(object_key: str, signature: str | None = None) -> FileResponse:
    try:
        key = validate_object_key(object_key)
        if settings.require_signed_media_urls and not valid_media_signature(key, signature):
            raise HTTPException(status_code=403, detail="媒体访问签名无效")
        target = await materialize_media_file(key)
    except HTTPException:
        raise
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=404, detail="媒体文件不存在") from error
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)
