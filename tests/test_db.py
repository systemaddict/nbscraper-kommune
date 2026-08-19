from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import httpx

from nbkommune import db
from nbkommune import repositories as repo


def _target():
    return SimpleNamespace(
        key="test",
        name="Test Kommune",
        site_url="https://example.test",
        news_url="https://example.test/news",
        press_url=None,
        source_type="news",
        channel="auto",
        enabled=True,
        note=None,
    )


def test_sqlite_schema_queue_and_timestamp_decoding():
    conn = db.SQLiteConnection(":memory:")
    db.init_schema(conn)
    repo.upsert_municipality(conn, _target())
    task_id = repo.enqueue_task(
        conn,
        kind="discover",
        municipality_key="test",
        reason="manual",
        priority=repo.PRIORITY_DISCOVER,
    )
    conn.commit()

    task = repo.pop_due_task(conn)
    assert task["id"] == task_id
    assert task["status"] == "running"
    assert task["attempts"] == 1
    assert isinstance(task["run_after"], datetime)
    assert isinstance(task["lease_expires_at"], datetime)
    conn.close()


def test_sqlite_rollback_preserves_caller_controlled_transaction():
    conn = db.SQLiteConnection(":memory:")
    db.init_schema(conn)
    repo.upsert_municipality(conn, _target())
    conn.rollback()

    assert repo.get_municipality(conn, "test") is None
    conn.close()


def test_bunny_connection_uses_closed_reads_and_baton_writes():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        sql = payload["requests"][0].get("stmt", {}).get("sql")
        if sql == "SELECT id, created_at FROM item WHERE id = ?":
            return httpx.Response(200, json={
                "baton": None,
                "base_url": None,
                "results": [
                    {
                        "type": "ok",
                        "response": {
                            "type": "execute",
                            "result": {
                                "cols": [
                                    {"name": "id", "decltype": "INTEGER"},
                                    {"name": "created_at", "decltype": "TEXT"},
                                ],
                                "rows": [[
                                    {"type": "integer", "value": "7"},
                                    {"type": "text", "value": "2026-08-19T08:00:00+00:00"},
                                ]],
                                "affected_row_count": 0,
                                "last_insert_rowid": None,
                            },
                        },
                    },
                    {"type": "ok", "response": {"type": "close"}},
                ],
            })
        if "baton" not in payload:
            return httpx.Response(200, json={
                "baton": "write-baton",
                "base_url": None,
                "results": [
                    {"type": "ok", "response": {"type": "execute", "result": {}}},
                    {"type": "ok", "response": {"type": "execute", "result": {}}},
                    {
                        "type": "ok",
                        "response": {
                            "type": "execute",
                            "result": {
                                "cols": [],
                                "rows": [],
                                "affected_row_count": 1,
                                "last_insert_rowid": "7",
                            },
                        },
                    },
                ],
            })
        return httpx.Response(200, json={
            "baton": None,
            "base_url": None,
            "results": [
                {"type": "ok", "response": {"type": "execute", "result": {}}},
                {"type": "ok", "response": {"type": "close"}},
            ],
        })

    conn = db.BunnyConnection(
        "libsql://database.example/",
        "secret",
        transport=httpx.MockTransport(handler),
    )
    row = conn.execute(
        "SELECT id, created_at FROM item WHERE id = %s", (7,)
    ).fetchone()
    assert row["id"] == 7
    assert isinstance(row["created_at"], datetime)

    result = conn.execute(
        "INSERT INTO item (id, enabled) VALUES (%(id)s, %(enabled)s)",
        {"id": 7, "enabled": True},
    )
    assert result.rowcount == 1
    conn.commit()
    conn.close()

    assert payloads[0]["requests"][-1] == {"type": "close"}
    write_stmt = payloads[1]["requests"][2]["stmt"]
    assert write_stmt["sql"] == "INSERT INTO item (id, enabled) VALUES (:id, :enabled)"
    assert write_stmt["named_args"][1]["value"] == {"type": "integer", "value": "1"}
    assert payloads[2]["baton"] == "write-baton"


def test_bunny_connection_omits_unused_named_arguments():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={
            "baton": None,
            "base_url": None,
            "results": [
                {
                    "type": "ok",
                    "response": {
                        "type": "execute",
                        "result": {"cols": [], "rows": [], "affected_row_count": 0},
                    },
                },
                {"type": "ok", "response": {"type": "close"}},
            ],
        })

    conn = db.BunnyConnection(
        "libsql://database.example/",
        "secret",
        transport=httpx.MockTransport(handler),
    )
    conn.execute(
        "SELECT * FROM item ORDER BY id LIMIT %(limit)s",
        {"municipality_key": None, "limit": 10},
    )
    conn.close()

    assert payloads[0]["requests"][0]["stmt"]["named_args"] == [
        {"name": "limit", "value": {"type": "integer", "value": "10"}}
    ]
