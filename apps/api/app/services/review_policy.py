"""Live project policy for optional creative-content review."""

from app.db.models import Project


def review_enabled(project) -> bool:
    # Missing values in old snapshots/test objects retain the existing behavior.
    return project is None or getattr(project, "review_enabled", True) is not False


async def workflow_review_enabled(session, workflow) -> bool:
    # Older workflow fakes and resumed unit-test fixtures do not always carry
    # project_id. Preserve the historical default in that case; real workflow
    # rows always have a project and therefore still read the persisted switch.
    project_id = getattr(workflow, "project_id", None)
    if session is None or not project_id or not hasattr(session, "get"):
        return True
    return review_enabled(await session.get(Project, project_id))
