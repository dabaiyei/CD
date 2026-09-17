import asyncio
from types import SimpleNamespace

from app.db.seed import upgrade_managed_model_capabilities
from app.services.provider_adapters import AGNES_PROVIDER_CODE, AGNES_VIDEO_MODEL_ID


def test_agnes_duration_repair_is_scoped_and_survives_restarts():
    def model(durations):
        return SimpleNamespace(model_id=AGNES_VIDEO_MODEL_ID, capabilities={
            "duration_resolution_map": [{"durations": durations, "resolutions": ["720p"]}],
        })
    old = model(list(range(4, 16)))
    custom = model([5, 10])
    unrelated = model(list(range(4, 16)))
    rows = [(old, AGNES_PROVIDER_CODE), (custom, AGNES_PROVIDER_CODE), (unrelated, "custom")]

    class Session:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: rows)

    for _ in range(2):
        asyncio.run(upgrade_managed_model_capabilities(Session()))
        assert old.capabilities["duration_resolution_map"][0]["durations"] == list(range(4, 13))
        assert custom.capabilities["duration_resolution_map"][0]["durations"] == [5, 10]
        assert unrelated.capabilities["duration_resolution_map"][0]["durations"] == list(range(4, 16))
