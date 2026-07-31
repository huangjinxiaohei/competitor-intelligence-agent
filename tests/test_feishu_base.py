from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from competitor_agent.config import FeishuBaseSettings, ProjectConfig
from competitor_agent.feishu_base import (
    FeishuBaseClient,
    FeishuBaseError,
    FeishuBaseSetup,
)
from competitor_agent.storage import StateStore


API = "https://open.feishu.cn"


def response(data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"code": 0, "msg": "ok", "data": data})


def token_response(token: str = "tenant-secret-token", expire: int = 7200) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": expire})


def config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project": {"id": "test", "topic": "SaaS", "keywords": ["saas"]},
            "feishu_base": {
                "enabled": True,
                "base_name": "竞品情报",
                "manifest_path": str(tmp_path / "manifest.json"),
            },
        }
    )


def client(handler, *, now=lambda: 1000.0, sleep=lambda _: None) -> FeishuBaseClient:
    transport = httpx.MockTransport(handler)
    return FeishuBaseClient(
        app_id="app-id",
        app_secret="app-secret",
        client=httpx.Client(transport=transport, base_url=API),
        now=now,
        sleep=sleep,
    )


def test_token_cache_and_refresh_before_expiry() -> None:
    calls: list[str] = []
    clock = [1000.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return token_response(expire=120) if len(calls) == 1 else token_response("tenant-second", 7200)

    service = client(handler, now=lambda: clock[0])
    assert service.tenant_token() == "tenant-secret-token"
    clock[0] += 50
    assert service.tenant_token() == "tenant-secret-token"
    clock[0] += 50
    assert service.tenant_token() == "tenant-second"
    assert calls == ["/open-apis/auth/v3/tenant_access_token/internal"] * 2


def test_request_retries_rate_limit_and_redacts_failures() -> None:
    requests = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        if len([item for item in requests if item.url.path.endswith("/tables")]) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.2"}, json={"code": 999, "msg": "token=tenant-secret-token"})
        return response({"items": [], "has_more": False})

    service = client(handler, sleep=waits.append)
    assert service.list_tables("app-token") == []
    assert waits == [0.2]

    def failures(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        return httpx.Response(500, text="tenant-secret-token https://private.example/path")

    with pytest.raises(FeishuBaseError) as caught:
        client(failures, sleep=lambda _: None).list_tables("app-token")
    assert "tenant-secret-token" not in str(caught.value)
    assert "private.example" not in str(caught.value)


def test_paginated_lists_and_record_helpers() -> None:
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "GET" and request.url.path.endswith("/records"):
            pages.append(request.url.params.get("page_token"))
            if len(pages) == 1:
                return response({"items": [{"record_id": "rec1"}], "has_more": True, "page_token": "next"})
            return response({"items": [{"record_id": "rec2"}], "has_more": False})
        if request.method == "POST" and request.url.path.endswith("/records"):
            return response({"record": {"record_id": "rec3"}})
        if request.method == "PUT":
            return response({"record": {"record_id": "rec3"}})
        raise AssertionError(f"Unexpected {request.method} {request.url}")

    service = client(handler)
    assert [row["record_id"] for row in service.list_records("app", "tbl")] == ["rec1", "rec2"]
    assert pages == [None, "next"]
    assert service.create_record("app", "tbl", {"ID": "one"})["record_id"] == "rec3"
    assert service.update_record("app", "tbl", "rec3", {"ID": "one"})["record_id"] == "rec3"


def test_doctor_reports_missing_env_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    result = FeishuBaseSetup.from_environment().doctor()
    assert not result.ok
    assert result.checks["credentials"] == "missing FEISHU_APP_ID, FEISHU_APP_SECRET"


def test_setup_creates_resources_relations_views_permissions_and_manifest(tmp_path: Path) -> None:
    created: list[tuple[str, str, dict]] = []
    fields: dict[str, list[dict]] = {}
    tables: list[dict] = []
    views: dict[str, list[dict]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        created.append((request.method, path, body))
        if path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "POST" and path == "/open-apis/bitable/v1/apps":
            return response({"app": {"app_token": "app-test", "url": "https://feishu.cn/base/app-test"}})
        if path == "/open-apis/bitable/v1/apps/app-test/tables" and request.method == "GET":
            return response({"items": tables, "has_more": False})
        if path == "/open-apis/bitable/v1/apps/app-test/tables" and request.method == "POST":
            name = body["table"]["name"]
            table_id = f"tbl{len(tables) + 1}"
            tables.append({"table_id": table_id, "name": name})
            fields[table_id] = []
            views[table_id] = []
            return response({"table_id": table_id})
        if path.endswith("/fields") and request.method == "GET":
            return response({"items": fields[path.split("/")[-2]], "has_more": False})
        if path.endswith("/fields") and request.method == "POST":
            table_id = path.split("/")[-2]
            field = {"field_id": f"fld{len(fields[table_id]) + 1}", **body}
            fields[table_id].append(field)
            return response({"field": field})
        if path.endswith("/views") and request.method == "GET":
            return response({"items": views[path.split("/")[-2]], "has_more": False})
        if path.endswith("/views") and request.method == "POST":
            table_id = path.split("/")[-2]
            view = {"view_id": f"vew{len(views[table_id]) + 1}", **body}
            views[table_id].append(view)
            return response({"view": view})
        if path.startswith("/open-apis/drive/v1/permissions/app-test/members"):
            return response({"member": body})
        raise AssertionError(f"Unexpected {request.method} {path}: {body}")

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    setup = FeishuBaseSetup(client(handler), config(tmp_path), store, owner_email="owner@example.com", viewer_chat_id="oc_chat")
    receipt = setup.setup()
    assert receipt.synced
    assert receipt.base_url == "https://feishu.cn/base/app-test"
    assert set(receipt.resource_links) >= {"竞品总览", "本周变化"}
    assert len(tables) == 4
    assert {item["name"] for item in tables} == {"竞品主表", "套餐价格表", "变化事件表", "运行日志表"}
    price_table = next(table["table_id"] for table in tables if table["name"] == "套餐价格表")
    link = next(field for field in fields[price_table] if field["field_name"] == "关联竞品")
    assert link["type"] == 21
    assert link["property"]["table_id"] == store.get_projection_resource("table:competitors")
    all_views = {view["view_name"]: view["view_type"] for rows in views.values() for view in rows}
    assert all_views["竞品卡片"] == "gallery"
    assert all_views["高优先级变化"] == "kanban"
    permission_payloads = [body for _, path, body in created if "/permissions/app-test/members" in path]
    assert {body["member_type"] for body in permission_payloads} == {"email", "chat_id"}
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8").startswith("{")
    assert store.get_projection_resource("base") == "app-test"
    field_count = sum(len(rows) for rows in fields.values())
    view_count = sum(len(rows) for rows in views.values())
    setup.setup()
    assert sum(len(rows) for rows in fields.values()) == field_count
    assert sum(len(rows) for rows in views.values()) == view_count
    assert sum(method == "POST" and path == "/open-apis/bitable/v1/apps" for method, path, _ in created) == 1
    assert sum(method == "POST" and path.endswith("/tables") for method, path, _ in created) == 4
    assert not (tmp_path / "manifest.json").read_bytes().startswith(b"\xef\xbb\xbf")
    store.close()


def test_doctor_reports_live_bitable_scope_failure(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        return httpx.Response(403, json={"code": 1254302, "msg": "forbidden"})

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.set_projection_resource("base", "app-test")
    result = FeishuBaseSetup(client(handler), config(tmp_path), store).doctor()
    assert not result.ok
    assert result.checks["authentication"] == "ok"
    assert "scope" in result.checks["bitable_read"]
    store.close()


def test_setup_replay_and_partial_recovery_reuses_persisted_resources(tmp_path: Path) -> None:
    # Stored Base and existing tables must be reused; only a missing table is created.
    calls: list[tuple[str, str]] = []
    tables = [
        {"table_id": "tbl-comp", "name": "竞品主表"},
        {"table_id": "tbl-price", "name": "套餐价格表"},
        {"table_id": "tbl-change", "name": "变化事件表"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "GET" and path.endswith("/tables"):
            return response({"items": tables, "has_more": False})
        if request.method == "POST" and path.endswith("/tables"):
            tables.append({"table_id": "tbl-run", "name": "运行日志表"})
            return response({"table_id": "tbl-run"})
        if request.method == "GET" and path.endswith("/fields"):
            return response({"items": [], "has_more": False})
        if request.method == "POST" and path.endswith("/fields"):
            return response({"field": {"field_id": "fld", **json.loads(request.content)}})
        if request.method == "GET" and path.endswith("/views"):
            return response({"items": [], "has_more": False})
        if request.method == "POST" and path.endswith("/views"):
            body = json.loads(request.content)
            return response({"view": {"view_id": "vew", **body}})
        if request.method == "POST" and "/permissions/" in path:
            return response({"member": {}})
        raise AssertionError(path)

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.set_projection_resource("base", "app-existing", "https://feishu.cn/base/app-existing")
    receipt = FeishuBaseSetup(client(handler), config(tmp_path), store).setup()
    assert receipt.base_url == "https://feishu.cn/base/app-existing"
    assert ("POST", "/open-apis/bitable/v1/apps") not in calls
    assert sum(method == "POST" and path.endswith("/tables") for method, path in calls) == 1
    store.close()


def test_setup_rejects_existing_field_with_wrong_type(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "GET" and path.endswith("/tables"):
            return response({"items": [{"table_id": "tbl-comp", "name": "竞品主表"}, {"table_id": "tbl-price", "name": "套餐价格表"}, {"table_id": "tbl-change", "name": "变化事件表"}, {"table_id": "tbl-run", "name": "运行日志表"}], "has_more": False})
        if request.method == "GET" and path.endswith("/fields"):
            return response({"items": [{"field_name": "竞品 ID", "type": 2}], "has_more": False})
        if request.method == "GET" and path.endswith("/views"):
            return response({"items": [], "has_more": False})
        raise AssertionError(path)

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.set_projection_resource("base", "app-existing")
    # Existing table mapping avoids table creation and isolates schema validation.
    store.set_projection_resource("table:competitors", "tbl-comp")
    with pytest.raises(FeishuBaseError, match="field type mismatch"):
        FeishuBaseSetup(client(handler), config(tmp_path), store).setup()
    store.close()
