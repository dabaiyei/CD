from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import (
    AgentKind,
    AgentProfile,
    AIModel,
    CreditAccount,
    Handbook,
    HandbookType,
    ImageResolutionModelRoute,
    ModelType,
    Project,
    PromptTemplate,
    Provider,
    ProviderType,
    Tenant,
    User,
    UserRole,
)
from app.db.session import SessionLocal
from app.services.billing import ensure_pricing_rules
from app.services.managed_skills import (
    SYSTEM_PROMPT_BASELINE_VERSIONS,
    SYSTEM_PROMPT_CODES,
    SYSTEM_PROMPTS,
    default_system_prompt_content,
    ensure_handbook_package,
    ensure_prompt_file,
    write_prompt_file,
)
from app.services.provider_adapters import (
    AGNES_PROVIDER_CODE,
    AGNES_VIDEO_MODEL_ID,
    AUTODL_MINIMAX_H3_PROVIDER_CODE,
    agnes_video_capabilities,
    autodl_minimax_h3_capabilities,
)


async def seed_missing_prompts(session, tenant_id: str) -> None:
    existing_codes = set(
        (
            await session.scalars(select(PromptTemplate.code).where(PromptTemplate.tenant_id == tenant_id))
        ).all()
    )
    session.add_all(
        [
            PromptTemplate(
                tenant_id=tenant_id,
                code=code,
                name=name,
                description=description,
                content=default_system_prompt_content(code),
            )
            for code, name, description in SYSTEM_PROMPTS
            if code not in existing_codes
        ]
    )
    await session.flush()
    prompts = (
        await session.scalars(select(PromptTemplate).where(PromptTemplate.tenant_id == tenant_id))
    ).all()
    for prompt in prompts:
        if prompt.code not in SYSTEM_PROMPT_CODES:
            continue
        baseline_version = SYSTEM_PROMPT_BASELINE_VERSIONS.get(prompt.code)
        if baseline_version is not None and prompt.version < baseline_version:
            prompt.content = default_system_prompt_content(prompt.code)
            prompt.version = baseline_version
            write_prompt_file(prompt)
        else:
            ensure_prompt_file(prompt)


async def upgrade_managed_model_capabilities(session) -> None:
    rows = (
        await session.execute(
            select(AIModel, Provider.code)
            .join(Provider, Provider.id == AIModel.provider_id)
            .where(AIModel.model_type == ModelType.VIDEO)
        )
    ).all()
    old_durations = [float(value) for value in range(4, 13)]
    upgraded_capabilities = agnes_video_capabilities()
    for model, provider_code in rows:
        if provider_code == AGNES_PROVIDER_CODE:
            defaults = agnes_video_capabilities()
        elif provider_code == AUTODL_MINIMAX_H3_PROVIDER_CODE:
            defaults = autodl_minimax_h3_capabilities()
        else:
            continue
        capabilities = dict(model.capabilities or {})
        duration_map = capabilities.get("duration_resolution_map")
        if (
            model.model_id == AGNES_VIDEO_MODEL_ID
            and isinstance(duration_map, list)
            and len(duration_map) == 1
        ):
            durations = (
                duration_map[0].get("durations") if isinstance(duration_map[0], dict) else None
            )
            if durations == old_durations or durations == list(range(4, 13)):
                capabilities["duration_resolution_map"] = upgraded_capabilities[
                    "duration_resolution_map"
                ]
        reference_limits = dict(capabilities.get("reference_limits") or {})
        for media_type, default_limit in defaults["reference_limits"].items():
            current_limit = dict(reference_limits.get(media_type) or {})
            if "accepted_mime_types" not in current_limit:
                current_limit["accepted_mime_types"] = default_limit.get(
                    "accepted_mime_types", []
                )
            reference_limits[media_type] = current_limit
        capabilities["reference_limits"] = reference_limits
        for field in ("preferred_prompt_language", "video_prompt_protocol"):
            if field in defaults and field not in capabilities:
                capabilities[field] = defaults[field]
        model.capabilities = capabilities


async def ensure_managed_skills(session, tenant_id: str) -> None:
    handbooks = (
        await session.scalars(select(Handbook).where(Handbook.tenant_id == tenant_id))
    ).all()
    for handbook in handbooks:
        ensure_handbook_package(handbook)
    await seed_missing_prompts(session, tenant_id)
    await upgrade_managed_model_capabilities(session)


async def seed_demo_data() -> None:
    async with SessionLocal() as session:
        existing = await session.scalar(select(Tenant).where(Tenant.slug == "demo"))
        if existing is not None:
            await ensure_managed_skills(session, existing.id)
            await ensure_pricing_rules(session, existing.id)
            await session.commit()
            return

        tenant = Tenant(name="光场影业", slug="demo")
        session.add(tenant)
        await session.flush()

        admin = User(
            tenant_id=tenant.id,
            email="admin@cineforge.local",
            display_name="平台管理员",
            password_hash=hash_password("Admin123!"),
            role=UserRole.ADMIN,
        )
        creator = User(
            tenant_id=tenant.id,
            email="creator@cineforge.local",
            display_name="林默",
            password_hash=hash_password("Creator123!"),
            role=UserRole.USER,
        )
        session.add_all([admin, creator])
        await session.flush()

        provider = Provider(
            tenant_id=tenant.id,
            code="studio-gateway",
            name="Studio Gateway",
            provider_type=ProviderType.OPENAI_COMPATIBLE,
            base_url="https://api.example.invalid/v1",
            enabled=True,
        )
        session.add(provider)
        await session.flush()

        text_model = AIModel(
            tenant_id=tenant.id,
            provider_id=provider.id,
            model_id="story-pro",
            name="Story Pro",
            model_type=ModelType.TEXT,
            capabilities={"context_window": 128000, "reasoning": True},
            is_default=True,
        )
        image_model = AIModel(
            tenant_id=tenant.id,
            provider_id=provider.id,
            model_id="frame-image-5",
            name="Frame Image 5",
            model_type=ModelType.IMAGE,
            capabilities={"resolutions": ["1K", "2K", "4K"]},
            is_default=True,
        )
        video_model = AIModel(
            tenant_id=tenant.id,
            provider_id=provider.id,
            model_id="motion-video-2",
            name="Motion Video 2",
            model_type=ModelType.VIDEO,
            capabilities={"resolutions": ["480p", "720p", "1080p"], "durations": [5, 10]},
            is_default=True,
        )
        tts_model = AIModel(
            tenant_id=tenant.id,
            provider_id=provider.id,
            model_id="voice-studio",
            name="Voice Studio",
            model_type=ModelType.TTS,
            capabilities={"languages": ["zh-CN", "en-US"]},
            is_default=False,
        )
        session.add_all([text_model, image_model, video_model, tts_model])
        await session.flush()
        session.add_all(
            [
                ImageResolutionModelRoute(
                    tenant_id=tenant.id,
                    resolution=resolution,
                    model_id=image_model.id,
                )
                for resolution in ("1K", "2K", "4K")
            ]
        )

        noir = Handbook(
            tenant_id=tenant.id,
            handbook_type=HandbookType.VISUAL,
            name="冷峻电影感",
            description="克制低饱和色彩、硬朗轮廓光与真实镜头质感。",
            skill_path="visual/noir-cinematic",
            cover_url="/covers/login-studio.jpg",
        )
        oriental = Handbook(
            tenant_id=tenant.id,
            handbook_type=HandbookType.VISUAL,
            name="东方动画美学",
            description="东方色彩秩序、绘制感材质与富有呼吸感的空间层次。",
            skill_path="visual/oriental-animation",
            cover_url="/covers/changan-night.jpg",
        )
        drama = Handbook(
            tenant_id=tenant.id,
            handbook_type=HandbookType.DIRECTOR,
            name="类型剧情导演",
            description="强调人物动机、事件因果和情绪递进的经典叙事方法。",
            skill_path="director/genre-drama",
        )
        fast = Handbook(
            tenant_id=tenant.id,
            handbook_type=HandbookType.DIRECTOR,
            name="高密度短剧节奏",
            description="面向竖屏短剧的高信息密度、强钩子与快速场景推进。",
            skill_path="director/fast-paced",
        )
        session.add_all([noir, oriental, drama, fast])
        await session.flush()
        for handbook in (noir, oriental, drama, fast):
            ensure_handbook_package(handbook)

        projects = [
            Project(
                tenant_id=tenant.id,
                owner_id=creator.id,
                name="雾港来信",
                description="失踪多年的记者寄回一封来自未来港口的信，牵出旧城改造背后的秘密。",
                cover_url="/covers/mist-harbor.jpg",
                video_model_id=video_model.id,
                video_resolution="1080p",
                aspect_ratio="16:9",
                image_model_id=image_model.id,
                image_resolution="2K",
                visual_handbook_id=noir.id,
                director_handbook_id=drama.id,
            ),
            Project(
                tenant_id=tenant.id,
                owner_id=creator.id,
                name="长安夜行录",
                description="巡夜人追查坊间异象，在一夜之内穿过繁华与暗流并存的长安。",
                cover_url="/covers/changan-night.jpg",
                video_model_id=video_model.id,
                video_resolution="720p",
                aspect_ratio="9:16",
                image_model_id=image_model.id,
                image_resolution="2K",
                visual_handbook_id=oriental.id,
                director_handbook_id=fast.id,
            ),
            Project(
                tenant_id=tenant.id,
                owner_id=creator.id,
                name="第二次告别",
                description="一位记忆修复师在客户的回忆中，再次遇见已经离开的恋人。",
                cover_url="/covers/second-farewell.jpg",
                video_model_id=video_model.id,
                video_resolution="1080p",
                aspect_ratio="16:9",
                image_model_id=image_model.id,
                image_resolution="1K",
                visual_handbook_id=noir.id,
                director_handbook_id=drama.id,
            ),
        ]
        session.add_all(projects)

        session.add_all(
            [
                CreditAccount(tenant_id=tenant.id, user_id=creator.id, balance=Decimal("1280.00")),
                CreditAccount(tenant_id=tenant.id, user_id=admin.id, balance=Decimal("9999.00")),
                AgentProfile(
                    tenant_id=tenant.id,
                    kind=AgentKind.SCREENPLAY,
                    name="剧本改编导演",
                    description="读取原文并形成故事骨架、改编策略和剧集规划。",
                    system_prompt="你是一名短剧改编导演。所有结论必须基于输入原文，并以结构化结果输出。",
                    text_model_id=text_model.id,
                    memory_enabled=True,
                ),
                AgentProfile(
                    tenant_id=tenant.id,
                    kind=AgentKind.GENERAL,
                    name="制片通用助手",
                    description="负责事件、资产、台词和提示词等辅助提取工作。",
                    system_prompt="你是短剧制作流程中的通用 AI 助手，优先保证信息准确、可追溯。",
                    text_model_id=text_model.id,
                    memory_enabled=True,
                ),
            ]
        )

        session.add_all(
            [
                PromptTemplate(
                    tenant_id=tenant.id,
                    code=code,
                    name=name,
                    description=description,
                    content=default_system_prompt_content(code),
                )
                for code, name, description in SYSTEM_PROMPTS
            ]
        )
        await session.flush()
        prompts = (
            await session.scalars(select(PromptTemplate).where(PromptTemplate.tenant_id == tenant.id))
        ).all()
        for prompt in prompts:
            ensure_prompt_file(prompt)
        await ensure_pricing_rules(session, tenant.id)
        await session.commit()
