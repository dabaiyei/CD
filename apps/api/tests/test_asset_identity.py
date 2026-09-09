from app.db.models import Asset, AssetType
from app.services.asset_identity import reusable_asset


def test_identity_preserves_existing_image_and_distinguishes_derivatives():
    empty = Asset(id="empty", name="林遥", asset_type=AssetType.CHARACTER)
    established = Asset(id="original", name="林遥", asset_type=AssetType.CHARACTER,
                        media_url="/uploads/original.png")
    derivative = Asset(id="coat", name="风衣", asset_type=AssetType.CHARACTER,
                       parent_asset_id="original")
    assets = [empty, established, derivative]
    assert reusable_asset(assets, "character", " 林遥 ", None) is established
    assert reusable_asset(assets, "character", "风衣", "original") is derivative
    assert reusable_asset(assets, "character", "风衣", "other-person") is None
    assert reusable_asset(assets, "scene", "林遥", None) is None
    assert reusable_asset(assets, "character", "林遥少年", None) is None
