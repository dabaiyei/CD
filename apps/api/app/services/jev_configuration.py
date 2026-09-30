from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.security import SecretBox
from app.db.models import JevConfiguration, JevPlatformSettings

JEV_PLATFORMS = {
    "typesafe": {
        "name": "Typesafe",
        "endpoint": "https://api.typesafe.ai/v1/systemone",
        "default_model": "jev-1.13.0",
    },
    "opencode_zen": {
        "name": "OpenCode Zen",
        "endpoint": "https://opencode.ai/zen/v1/systemone",
        "default_model": "jev-1.13-free",
    },
}


@dataclass
class JevSettings:
    enabled: bool
    api_key: str = field(repr=False)
    model: str = "jev-1.13.0"
    timeout_seconds: float = 8
    route_confidence: float = 0.65
    source: str = "database"
    provider: str = "typesafe"

    @property
    def endpoint(self):
        return JEV_PLATFORMS[self.provider]["endpoint"]


def environment_configuration():
    settings = get_settings()
    key = settings.typesafe_api_key.get_secret_value()
    return JevSettings(
        bool(key),
        key,
        settings.typesafe_model,
        settings.typesafe_timeout_seconds,
        settings.typesafe_route_confidence,
        "environment",
    )


async def get_jev_configuration(session, tenant_id, *, provider=None):
    row = await session.get(JevConfiguration, tenant_id)
    config = (
        environment_configuration()
        if row is None
        else JevSettings(
            row.enabled,
            SecretBox().decrypt(row.encrypted_api_key) or "",
            row.model,
            row.timeout_seconds,
            row.route_confidence,
        )
    )
    platforms = await session.get(JevPlatformSettings, tenant_id)
    selected = provider or (platforms.provider if platforms else "typesafe")
    if selected not in JEV_PLATFORMS:
        raise ValueError("Unsupported JEV platform")
    profile = (platforms.profiles or {}).get(selected) if platforms else None
    if profile is not None:
        config.api_key = SecretBox().decrypt(profile.get("encrypted_api_key")) or ""
        config.model = profile.get("model") or JEV_PLATFORMS[selected]["default_model"]
        config.source = "database"
    elif selected != "typesafe":
        config.api_key = ""
        config.model = JEV_PLATFORMS[selected]["default_model"]
        config.source = "database"
    config.provider = selected
    return config
