"""Repair only a confirmed failed session's duplicate-name image-model binding."""

import json
import sqlite3

SESSION = "6e82f79b-2ba0-423c-bb12-14e1c74614eb"
OLD = "d37b459c-9b0c-4d5e-90fd-d80b1258cf68"


def replace(value, new):
    if isinstance(value, dict):
        return {
            key: new
            if key in {"model_id", "media_model_id"} and item == OLD
            else replace(item, new)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace(item, new) for item in value]
    return value


def main():
    with sqlite3.connect("cineforge.db", timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        chat = db.execute(
            "select * from agent_chat_sessions where id=?", (SESSION,)
        ).fetchone()
        old = db.execute("select * from ai_models where id=?", (OLD,)).fetchone()
        models = db.execute(
            "select m.id from ai_models m join providers p on p.id=m.provider_id "
            "where m.tenant_id=? and m.model_id=? and m.model_type=? and m.enabled=1 "
            "and m.is_default=1 and p.enabled=1 and p.tenant_id=m.tenant_id",
            (chat["tenant_id"], old["model_id"], old["model_type"]),
        ).fetchall()
        assert len(models) == 1 and models[0]["id"] != OLD
        new = models[0]["id"]
        tasks = db.execute(
            "select id,request_payload from ai_tasks where tenant_id=? and user_id=? "
            "and model_id=? and status=? and error_message like ? "
            "and json_extract(request_payload,'$.agent_chat_session_id')=?",
            (
                chat["tenant_id"],
                chat["user_id"],
                OLD,
                "FAILED",
                "%Image generation is not enabled%",
                SESSION,
            ),
        ).fetchall()
        for task in tasks:
            payload = replace(json.loads(task["request_payload"]), new)
            payload["model_binding_repair"] = "duplicate_name_default_provider"
            db.execute(
                "update ai_tasks set model_id=?,request_payload=? where id=?",
                (new, json.dumps(payload, ensure_ascii=False), task["id"]),
            )
        manifest = replace(json.loads(chat["runtime_manifest"] or "{}"), new)
        db.execute(
            "update agent_chat_sessions set runtime_manifest=? where id=?",
            (json.dumps(manifest, ensure_ascii=False), SESSION),
        )
        db.commit()
        print(
            "Repaired failed task bindings:",
            len(tasks),
            "; session plan updated; no tasks started",
        )


if __name__ == "__main__":
    main()
