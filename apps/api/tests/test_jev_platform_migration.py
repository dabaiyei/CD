import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.migration_guard import migration_script_directory
from app.db.models import JevConfiguration


@pytest.mark.parametrize("legacy_exists", [False, True])
def test_platform_migration_preserves_legacy_and_supports_fresh_install(legacy_exists):
    engine = sa.create_engine("sqlite://")
    revision = migration_script_directory().get_revision("d3f5a7b9c120").module
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE tenants (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO tenants VALUES ('tenant')"))
        if legacy_exists:
            JevConfiguration.__table__.create(connection)
            connection.execute(
                JevConfiguration.__table__.insert().values(
                    tenant_id="tenant",
                    enabled=True,
                    encrypted_api_key="encrypted-original",
                    model="jev-latest",
                )
            )
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.upgrade()  # Development create_all plus migration remains safe.
        assert {"jev_configurations", "jev_platform_settings"} <= set(
            sa.inspect(connection).get_table_names()
        )
        if legacy_exists:
            assert (
                connection.execute(sa.text("SELECT encrypted_api_key FROM jev_configurations")).scalar_one()
                == "encrypted-original"
            )
    engine.dispose()
