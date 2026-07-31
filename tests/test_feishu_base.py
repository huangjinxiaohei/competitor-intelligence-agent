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
    TABLES,
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



def test_authentication_retries_429_and_5xx_then_exhausts() -> None:
    attempts = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url.path.endswith("tenant_access_token/internal")
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.4"})
        if attempts == 2:
            return httpx.Response(503)
        return token_response()

    assert client(handler, sleep=waits.append).tenant_token() == "tenant-secret-token"
    assert attempts == 3
    assert waits == [0.4, 2.0]

    exhausted = 0

    def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal exhausted
        exhausted += 1
        return httpx.Response(503)

    with pytest.raises(FeishuBaseError, match="authentication failed"):
        client(unavailable, sleep=lambda _: None).tenant_token()
    assert exhausted == 4


def test_owned_client_closes_without_closing_injected_client() -> None:
    with FeishuBaseClient(app_id="id", app_secret="secret") as owned:
        assert not owned._client.is_closed
    assert owned._client.is_closed

    injected = httpx.Client(transport=httpx.MockTransport(lambda _: token_response()), base_url=API)
    FeishuBaseClient(app_id="id", app_secret="secret", client=injected).close()
    assert not injected.is_closed
    injected.close()

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
            tables.append({"table_id": "tbl-default", "name": "Default blank table"})
            fields["tbl-default"] = [{"field_id": "fld-default", "field_name": "Default field", "type": 1, "is_primary": True}]
            views["tbl-default"] = []
            return response({"app": {"app_token": "app-test", "url": "https://feishu.cn/base/app-test"}})
        if path == "/open-apis/bitable/v1/apps/app-test/tables" and request.method == "GET":
            return response({"items": tables, "has_more": False})
        if path == "/open-apis/bitable/v1/apps/app-test/tables" and request.method == "POST":
            name = body["table"]["name"]
            table_id = f"tbl{len(tables) + 1}"
            tables.append({"table_id": table_id, "name": name})
            fields[table_id] = [{"field_id": f"fld-{table_id}", "field_name": "Default field", "type": 1, "is_primary": True}]
            views[table_id] = []
            return response({"table_id": table_id})
        if "/fields/" in path and request.method == "PUT":
            table_id, field_id = path.split("/")[-3], path.split("/")[-1]
            field = next(item for item in fields[table_id] if item["field_id"] == field_id)
            assert body == {"field_name": next(spec.primary_field for spec in TABLES if spec.primary_field == body["field_name"]), "type": 1}
            field["field_name"] = body["field_name"]
            return response({"field": field})
        if path == "/open-apis/bitable/v1/apps/app-test/tables/tbl-default" and request.method == "PATCH":
            assert body == {"name": TABLES[0].name}
            tables[0]["name"] = body["name"]
            return response({"table": tables[0]})
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
    assert sum(method == "POST" and path.endswith("/tables") for method, path, _ in created) == 3
    price_table = next(table["table_id"] for table in tables if table["name"] == "套餐价格表")
    link = next(field for field in fields[price_table] if field["field_name"] == "关联竞品")
    assert link["type"] == 21
    assert link["property"]["table_id"] == store.get_projection_resource("table:competitors")
    all_views = {view["view_name"]: view["view_type"] for rows in views.values() for view in rows}
    assert all_views["竞品卡片"] == "gallery"
    assert all_views["高优先级变化"] == "kanban"
    primary_names = {table_id: next(field["field_name"] for field in table_fields if field.get("is_primary")) for table_id, table_fields in fields.items()}
    assert set(primary_names.values()) == {spec.primary_field for spec in TABLES}
    assert all(field["field_name"] != "Default field" for table_fields in fields.values() for field in table_fields)
    for spec in TABLES:
        table_id = next(table["table_id"] for table in tables if table["name"] == spec.name)
        assert {field["field_name"] for field in fields[table_id]} == {field.name for field in spec.fields}
    permission_payloads = [body for _, path, body in created if "/permissions/app-test/members" in path]
    assert permission_payloads == [
        {"member_type": "email", "member_id": "owner@example.com", "perm": "edit", "type": "user"},
        {"member_type": "openchat", "member_id": "oc_chat", "perm": "view", "type": "chat"},
    ]
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8").startswith("{")
    assert store.get_projection_resource("base") == "app-test"
    field_count = sum(len(rows) for rows in fields.values())
    view_count = sum(len(rows) for rows in views.values())
    setup.setup()
    assert sum(len(rows) for rows in fields.values()) == field_count
    assert sum(len(rows) for rows in views.values()) == view_count
    assert sum(method == "POST" and path == "/open-apis/bitable/v1/apps" for method, path, _ in created) == 1
    assert sum(method == "POST" and path.endswith("/tables") for method, path, _ in created) == 3
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



def test_stale_sqlite_base_recovers_from_valid_manifest(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.url.path.endswith("/apps/app-stale/tables"):
            return httpx.Response(404, json={"code": 1254040, "msg": "missing"})
        if request.url.path.endswith("/apps/app-manifest/tables"):
            return response({"items": [], "has_more": False})
        raise AssertionError(request.url.path)

    settings = config(tmp_path)
    Path(settings.feishu_base.manifest_path).write_text(
        json.dumps({"base": {"id": "app-manifest", "url": "https://feishu.cn/base/app-manifest"}}),
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.set_projection_resource("base", "app-stale", "https://feishu.cn/base/app-stale")
    app_token, base_url, tables = FeishuBaseSetup(client(handler), settings, store)._ensure_base("ignored")
    assert (app_token, base_url, tables) == (
        "app-manifest", "https://feishu.cn/base/app-manifest", []
    )
    assert store.get_projection_resource("base") == "app-manifest"
    store.close()



def test_permission_or_transient_error_never_switches_to_manifest_base(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.url.path.endswith("/apps/app-current/tables"):
            return httpx.Response(403, json={"code": 1254302, "msg": "forbidden"})
        if request.url.path.endswith("/apps/app-old/tables"):
            return response({"items": [], "has_more": False})
        raise AssertionError(request.url.path)

    settings = config(tmp_path)
    Path(settings.feishu_base.manifest_path).write_text(
        json.dumps({"base": {"id": "app-old", "url": "https://feishu.cn/base/app-old"}}),
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.set_projection_resource("base", "app-current", "https://feishu.cn/base/app-current")
    with pytest.raises(FeishuBaseError) as caught:
        FeishuBaseSetup(client(handler), settings, store)._ensure_base("ignored")
    assert caught.value.operation == "API request"
    assert caught.value.http_status == 403
    assert caught.value.api_code == 1254302
    assert store.get_projection_resource("base") == "app-current"
    assert not any("app-old" in call for call in calls)
    store.close()


def test_restart_after_base_creation_resumes_marked_default_table(tmp_path: Path) -> None:
    tables = [{"table_id": "tbl-default", "name": "Default blank table"}]
    fields: dict[str, list[dict]] = {
        "tbl-default": [{"field_id": "fld-default", "field_name": "Default field", "type": 1, "is_primary": True}]
    }
    views: dict[str, list[dict]] = {"tbl-default": []}
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        calls.append((request.method, path))
        if path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "GET" and path.endswith("/tables"):
            return response({"items": tables, "has_more": False})
        if request.method == "PATCH" and path.endswith("/tables/tbl-default"):
            tables[0]["name"] = body["name"]
            return response({"table": tables[0]})
        if request.method == "POST" and path.endswith("/tables"):
            table_id = f"tbl-{len(tables) + 1}"
            tables.append({"table_id": table_id, "name": body["table"]["name"]})
            fields[table_id] = [{"field_id": f"fld-{table_id}", "field_name": "Default field", "type": 1, "is_primary": True}]
            views[table_id] = []
            return response({"table_id": table_id})
        if request.method == "GET" and path.endswith("/fields"):
            return response({"items": fields[path.split("/")[-2]], "has_more": False})
        if request.method == "PUT" and "/fields/" in path:
            table_id, field_id = path.split("/")[-3], path.split("/")[-1]
            field = next(row for row in fields[table_id] if row["field_id"] == field_id)
            field["field_name"] = body["field_name"]
            return response({"field": field})
        if request.method == "POST" and path.endswith("/fields"):
            table_id = path.split("/")[-2]
            field = {"field_id": f"fld-{len(fields[table_id]) + 1}", **body}
            fields[table_id].append(field)
            return response({"field": field})
        if request.method == "GET" and path.endswith("/views"):
            return response({"items": views[path.split("/")[-2]], "has_more": False})
        if request.method == "POST" and path.endswith("/views"):
            table_id = path.split("/")[-2]
            view = {"view_id": f"vew-{len(views[table_id]) + 1}", **body}
            views[table_id].append(view)
            return response({"view": view})
        raise AssertionError(f"{request.method} {path}")

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.set_projection_resource("base", "app-restarted", "https://feishu.cn/base/app-restarted")
    store.set_projection_resource("setup:phase", "base-created", "app-restarted")
    receipt = FeishuBaseSetup(client(handler), config(tmp_path), store, owner_email="", viewer_chat_id="").setup()
    assert receipt.synced
    assert len(tables) == 4
    assert set(table["name"] for table in tables) == {spec.name for spec in TABLES}
    assert not any(method == "POST" and path == "/open-apis/bitable/v1/apps" for method, path in calls)
    assert store.get_projection_resource("setup:phase") == "complete"
    assert all(field["field_name"] != "Default field" for table_fields in fields.values() for field in table_fields)
    for spec in TABLES:
        table_id = next(table["table_id"] for table in tables if table["name"] == spec.name)
        assert {field["field_name"] for field in fields[table_id]} == {field.name for field in spec.fields}
    store.close()


def test_create_then_first_list_failure_reuses_marked_base_on_retry(tmp_path: Path) -> None:
    tables = [{"table_id": "tbl-default", "name": "Default blank table"}]
    fields: dict[str, list[dict]] = {
        "tbl-default": [{"field_id": "fld-default", "field_name": "Default field", "type": 1, "is_primary": True}]
    }
    views: dict[str, list[dict]] = {"tbl-default": []}
    calls: list[tuple[str, str]] = []
    fail_first_list = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        calls.append((request.method, path))
        if path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "POST" and path == "/open-apis/bitable/v1/apps":
            return response({"app": {"app_token": "app-crash", "url": "https://feishu.cn/base/app-crash"}})
        if request.method == "GET" and path.endswith("/apps/app-crash/tables"):
            if fail_first_list[0]:
                return httpx.Response(503)
            return response({"items": tables, "has_more": False})
        if request.method == "PATCH" and path.endswith("/tables/tbl-default"):
            tables[0]["name"] = body["name"]
            return response({"table": tables[0]})
        if request.method == "POST" and path.endswith("/tables"):
            table_id = f"tbl-{len(tables) + 1}"
            tables.append({"table_id": table_id, "name": body["table"]["name"]})
            fields[table_id] = [{"field_id": f"fld-{table_id}", "field_name": "Default field", "type": 1, "is_primary": True}]
            views[table_id] = []
            return response({"table_id": table_id})
        if request.method == "GET" and path.endswith("/fields"):
            return response({"items": fields[path.split("/")[-2]], "has_more": False})
        if request.method == "PUT" and "/fields/" in path:
            table_id, field_id = path.split("/")[-3], path.split("/")[-1]
            field = next(row for row in fields[table_id] if row["field_id"] == field_id)
            field["field_name"] = body["field_name"]
            return response({"field": field})
        if request.method == "POST" and path.endswith("/fields"):
            table_id = path.split("/")[-2]
            field = {"field_id": f"fld-{len(fields[table_id]) + 1}", **body}
            fields[table_id].append(field)
            return response({"field": field})
        if request.method == "GET" and path.endswith("/views"):
            return response({"items": views[path.split("/")[-2]], "has_more": False})
        if request.method == "POST" and path.endswith("/views"):
            table_id = path.split("/")[-2]
            view = {"view_id": f"vew-{len(views[table_id]) + 1}", **body}
            views[table_id].append(view)
            return response({"view": view})
        raise AssertionError(f"{request.method} {path}")

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    first_client = FeishuBaseClient(
        app_id="app-id",
        app_secret="app-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url=API),
        max_retries=0,
    )
    with pytest.raises(FeishuBaseError):
        FeishuBaseSetup(first_client, config(tmp_path), store, owner_email="", viewer_chat_id="").setup()
    assert store.get_projection_resource("base") == "app-crash"
    assert store.get_projection_resource("setup:phase") == "base-created"
    assert store.get_projection_resource_link("setup:phase") == "app-crash"
    assert store.get_projection_resource("setup:initial_table") is None

    fail_first_list[0] = False
    receipt = FeishuBaseSetup(client(handler), config(tmp_path), store, owner_email="", viewer_chat_id="").setup()
    assert receipt.synced
    assert len(tables) == 4
    assert sum(method == "POST" and path == "/open-apis/bitable/v1/apps" for method, path in calls) == 1
    assert store.get_projection_resource("setup:phase") == "complete"
    assert store.get_projection_resource_link("setup:phase") == "app-crash"
    store.close()


def test_restart_between_phase_and_base_mapping_recovers_created_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = [{"table_id": "tbl-default", "name": "Default blank table"}]
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        calls.append((request.method, path))
        if path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "POST" and path == "/open-apis/bitable/v1/apps":
            return response({"app": {"app_token": "app-created", "url": "https://feishu.cn/base/app-created"}})
        if request.method == "GET" and path.endswith("/apps/app-created/tables"):
            return response({"items": tables, "has_more": False})
        if request.method == "PATCH" and path.endswith("/tables/tbl-default"):
            tables[0]["name"] = body["name"]
            return response({"table": tables[0]})
        if request.method == "POST" and path.endswith("/tables"):
            table_id = f"tbl-{len(tables) + 1}"
            tables.append({"table_id": table_id, "name": body["table"]["name"]})
            return response({"table_id": table_id})
        raise AssertionError(f"Unexpected {request.method} {path}")

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    setup = FeishuBaseSetup(client(handler), config(tmp_path), store)
    original_set_resource = store.set_projection_resource

    def crash_before_base_mapping(resource_key: str, resource_id: str, link: str | None = None) -> None:
        if resource_key == "base":
            raise RuntimeError("simulated process interruption")
        original_set_resource(resource_key, resource_id, link)

    monkeypatch.setattr(store, "set_projection_resource", crash_before_base_mapping)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        setup._ensure_base("Competitor Base")
    assert store.get_projection_resource("base") is None
    assert store.get_projection_resource("setup:phase") == "base-created"
    assert store.get_projection_resource_link("setup:phase") == "app-created"

    monkeypatch.setattr(store, "set_projection_resource", original_set_resource)
    recovered = FeishuBaseSetup(client(handler), config(tmp_path), store)
    app_token, _, existing = recovered._ensure_base("Competitor Base")
    initial_table_id = recovered._capture_initial_table(app_token, existing)
    table_ids = recovered._ensure_tables(
        app_token,
        initial_table_id=initial_table_id,
        existing_tables=existing,
    )

    assert app_token == "app-created"
    assert len(table_ids) == 4
    assert len(tables) == 4
    assert {table["name"] for table in tables} == {spec.name for spec in TABLES}
    assert sum(method == "POST" and path == "/open-apis/bitable/v1/apps" for method, path in calls) == 1
    store.close()

def test_manifest_fallback_ignores_phase_metadata_for_another_base(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.url.path.endswith("/apps/app-stale/tables"):
            return httpx.Response(404, json={"code": 1254040, "msg": "missing"})
        if request.url.path.endswith("/apps/app-other/tables"):
            return response({"items": [{"table_id": "tbl-other", "name": "Mature table"}], "has_more": False})
        raise AssertionError(request.url.path)

    settings = config(tmp_path)
    Path(settings.feishu_base.manifest_path).write_text(
        json.dumps({"base": {"id": "app-other", "url": "https://feishu.cn/base/app-other"}}),
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.db")
    store.initialize()
    store.set_projection_resource("base", "app-stale")
    store.set_projection_resource("setup:phase", "base-created", "app-stale")
    store.set_projection_resource("setup:initial_table", "tbl-stale", "app-stale")
    setup = FeishuBaseSetup(client(handler), settings, store)
    app_token, _, tables = setup._ensure_base("ignored")
    assert app_token == "app-other"
    assert setup._capture_initial_table(app_token, tables) is None
    assert store.get_projection_resource("base") == "app-other"
    assert store.get_projection_resource_link("setup:phase") == "app-stale"
    store.close()

def test_replay_never_renames_an_arbitrary_existing_table(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("tenant_access_token/internal"):
            return token_response()
        if request.method == "POST" and request.url.path.endswith("/tables"):
            return response({"table_id": f"tbl-{len(calls)}"})
        raise AssertionError(request.url.path)

    store = StateStore(tmp_path / "state.db")
    store.initialize()
    table_ids = FeishuBaseSetup(client(handler), config(tmp_path), store)._ensure_tables(
        "app-existing",
        initial_table_id=None,
        existing_tables=[{"table_id": "tbl-arbitrary", "name": "Notes"}],
    )
    assert set(table_ids) == {"competitors", "pricing", "changes", "runs"}
    assert not any(call.startswith("PATCH ") for call in calls)
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
            return response({"items": [{"field_id": "fld-name", "field_name": "名称", "type": 1, "is_primary": True}, {"field_name": "竞品 ID", "type": 2}], "has_more": False})
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
