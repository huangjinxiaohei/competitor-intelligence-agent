"""Official Feishu Bitable client and idempotent Base bootstrap.

The module deliberately keeps Base provisioning separate from projection writes.
SQLite stores the identifiers it creates, so a later projection can rebuild data
without treating Feishu as the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

import httpx

from .config import ProjectConfig
from .models import ProjectionReceipt
from .storage import StateStore


_AUTH_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_API_ORIGIN = "https://open.feishu.cn"
_TOKEN_REFRESH_SECONDS = 60
_MAX_RETRIES = 3


class FeishuBaseError(RuntimeError):
    """A sanitized API failure with only operation and status classifications."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "Feishu API request",
        http_status: int | None = None,
        api_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.http_status = http_status
        self.api_code = api_code


@dataclass(frozen=True, slots=True)
class BaseDoctorResult:
    ok: bool
    checks: dict[str, str]
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    type: int
    relation_to: str | None = None
    human_owned: bool = False


@dataclass(frozen=True, slots=True)
class TableSpec:
    key: str
    name: str
    primary_field: str
    fields: tuple[FieldSpec, ...]


_TEXT = 1
_NUMBER = 2
_SINGLE_SELECT = 3
_MULTI_SELECT = 4
_DATETIME = 5
_CHECKBOX = 7
_URL = 15
_DUPLEX_LINK = 21

TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        "competitors",
        "竞品主表",
        "名称",
        (
            FieldSpec("名称", _TEXT), FieldSpec("竞品 ID", _TEXT), FieldSpec("官网", _URL),
            FieldSpec("状态", _SINGLE_SELECT), FieldSpec("评分", _NUMBER), FieldSpec("摘要", _TEXT),
            FieldSpec("功能", _TEXT), FieldSpec("配置", _TEXT), FieldSpec("可用性", _TEXT),
            FieldSpec("置信度", _NUMBER), FieldSpec("证据", _TEXT), FieldSpec("最后采集时间", _DATETIME),
            FieldSpec("人工关注级别", _SINGLE_SELECT, human_owned=True),
            FieldSpec("标签", _MULTI_SELECT, human_owned=True), FieldSpec("备注", _TEXT, human_owned=True),
        ),
    ),
    TableSpec(
        "pricing",
        "套餐价格表",
        "价格键",
        (
            FieldSpec("价格键", _TEXT), FieldSpec("关联竞品", _DUPLEX_LINK, relation_to="competitors"),
            FieldSpec("套餐", _TEXT), FieldSpec("金额", _NUMBER), FieldSpec("币种", _TEXT),
            FieldSpec("周期", _TEXT), FieldSpec("单位", _TEXT), FieldSpec("限定条件", _TEXT),
            FieldSpec("有效状态", _CHECKBOX), FieldSpec("观测时间", _DATETIME), FieldSpec("证据", _TEXT),
        ),
    ),
    TableSpec(
        "changes",
        "变化事件表",
        "事件 ID",
        (
            FieldSpec("事件 ID", _TEXT), FieldSpec("关联竞品", _DUPLEX_LINK, relation_to="competitors"),
            FieldSpec("字段路径", _TEXT), FieldSpec("前值", _TEXT), FieldSpec("后值", _TEXT),
            FieldSpec("重要度", _SINGLE_SELECT), FieldSpec("检测时间", _DATETIME),
            FieldSpec("证据", _TEXT), FieldSpec("运行 ID", _TEXT),
        ),
    ),
    TableSpec(
        "runs",
        "运行日志表",
        "运行 ID",
        (
            FieldSpec("运行 ID", _TEXT), FieldSpec("状态", _SINGLE_SELECT), FieldSpec("耗时秒", _NUMBER),
            FieldSpec("候选数", _NUMBER), FieldSpec("快照数", _NUMBER), FieldSpec("变化数", _NUMBER),
            FieldSpec("失败数", _NUMBER), FieldSpec("模型降级", _CHECKBOX), FieldSpec("Base 同步", _TEXT),
            FieldSpec("消息状态", _TEXT), FieldSpec("本地报告路径", _TEXT), FieldSpec("是否最新", _CHECKBOX),
        ),
    ),
)

VIEWS: tuple[tuple[str, str, str], ...] = (
    ("competitors", "竞品总览", "grid"), ("competitors", "竞品卡片", "gallery"),
    ("competitors", "低置信度", "grid"), ("pricing", "价格对比", "grid"),
    ("changes", "本周变化", "grid"), ("changes", "高优先级变化", "kanban"),
    ("runs", "指标总览", "grid"), ("runs", "采集异常", "grid"), ("runs", "运行历史", "grid"),
)



class FeishuBaseClient:
    """Small synchronous client covering setup and future projection writes."""

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        *,
        client: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self.app_id = app_id if app_id is not None else os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret if app_secret is not None else os.getenv("FEISHU_APP_SECRET", "")
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=_API_ORIGIN, timeout=15.0)
        self._now = now
        self._sleep = sleep
        self._max_retries = max(0, max_retries)
        self._token: str | None = None
        self._token_expires_at = 0.0

    def __enter__(self) -> FeishuBaseClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close only the HTTP client created by this adapter."""
        if self._owns_client:
            self._client.close()

    @property
    def has_credentials(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def tenant_token(self) -> str:
        if not self.has_credentials:
            raise FeishuBaseError("Feishu credentials are not configured.")
        if self._token is not None and self._now() < self._token_expires_at - _TOKEN_REFRESH_SECONDS:
            return self._token
        response = self._send_with_retry(
            "POST",
            _AUTH_PATH,
            json_body={"app_id": self.app_id, "app_secret": self.app_secret},
            headers={"Content-Type": "application/json; charset=utf-8"},
            action="authentication",
        )
        payload = self._response_payload(response, "authentication")
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuBaseError("Feishu authentication response did not contain a tenant token.")
        expire = payload.get("expire", 7200)
        self._token = token
        self._token_expires_at = self._now() + max(1, int(expire))
        return token

    def _response_payload(self, response: httpx.Response, action: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        if not 200 <= response.status_code < 300 or code not in (0, None):
            suffix = f", code {code}" if code is not None else ""
            raise FeishuBaseError(
                f"Feishu {action} failed (HTTP {response.status_code}{suffix}).",
                operation=action,
                http_status=response.status_code,
                api_code=code if isinstance(code, int) else None,
            )
        if not isinstance(payload, dict):
            raise FeishuBaseError(f"Feishu {action} returned an invalid response.", operation=action)
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise FeishuBaseError(f"Feishu {action} returned an invalid data object.", operation=action)
        return data

    def _send_with_retry(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        action: str,
    ) -> httpx.Response:
        """Send either auth or API requests with the same bounded transient policy."""
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(method, path, json=json_body, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    self._sleep(min(2.0**attempt, 4.0))
                    continue
                raise FeishuBaseError(
                    f"Feishu {action} failed while contacting the API.",
                    operation=action,
                ) from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after is not None else min(2.0**attempt, 4.0)
                    except ValueError:
                        delay = min(2.0**attempt, 4.0)
                    self._sleep(max(0.0, delay))
                    continue
            return response
        raise FeishuBaseError(f"Feishu {action} retry budget exhausted.", operation=action)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an authenticated OpenAPI endpoint with bounded transient retries."""
        response = self._send_with_retry(
            method,
            path,
            json_body=json_body,
            params=params,
            headers={
                "Authorization": f"Bearer {self.tenant_token()}",
                "Content-Type": "application/json; charset=utf-8",
            },
            action="API request",
        )
        return self._response_payload(response, "API request")

    def list_paginated(self, path: str, *, item_key: str = "items", params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        query = dict(params or {})
        query.setdefault("page_size", 100)
        while True:
            data = self.request("GET", path, params=query)
            page_items = data.get(item_key, [])
            if not isinstance(page_items, list):
                raise FeishuBaseError("Feishu list response did not contain an item list.")
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                return items
            token = data.get("page_token")
            if not isinstance(token, str) or not token:
                raise FeishuBaseError("Feishu paginated response omitted page_token.")
            query["page_token"] = token

    def create_base(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/open-apis/bitable/v1/apps", json_body={"name": name})

    def list_tables(self, app_token: str) -> list[dict[str, Any]]:
        return self.list_paginated(f"/open-apis/bitable/v1/apps/{app_token}/tables")

    def create_table(self, app_token: str, name: str) -> dict[str, Any]:
        return self.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables", json_body={"table": {"name": name}})

    def rename_table(self, app_token: str, table_id: str, name: str) -> dict[str, Any]:
        """Rename the single default table created with a new Base."""
        return self.request(
            "PATCH",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}",
            json_body={"name": name},
        )

    def list_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        return self.list_paginated(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields")

    def create_field(self, app_token: str, table_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields", json_body=payload).get("field", {})

    def update_field(self, app_token: str, table_id: str, field_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update the default index field while preserving its type/property contract."""
        return self.request(
            "PUT",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}",
            json_body=payload,
        ).get("field", {})

    def list_views(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        return self.list_paginated(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/views")

    def create_view(self, app_token: str, table_id: str, name: str, view_type: str) -> dict[str, Any]:
        data = self.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/views", json_body={"view_name": name, "view_type": view_type})
        return data.get("view", {})

    def list_records(self, app_token: str, table_id: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.list_paginated(f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records", params=params)

    def create_record(self, app_token: str, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records", json_body={"fields": fields}).get("record", {})

    def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request("PUT", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}", json_body={"fields": fields}).get("record", {})

    def create_records(self, app_token: str, table_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(records) > 200:
            raise ValueError("Feishu batch create accepts at most 200 records")
        return self.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create", json_body={"records": records}).get("records", [])

    def update_records(self, app_token: str, table_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(records) > 200:
            raise ValueError("Feishu batch update accepts at most 200 records")
        return self.request("POST", f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update", json_body={"records": records}).get("records", [])

    def grant_permission(
        self,
        app_token: str,
        *,
        member_type: str,
        member_id: str,
        perm: str,
        collaborator_type: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/open-apis/drive/v1/permissions/{app_token}/members",
            params={"type": "bitable"},
            json_body={
                "member_type": member_type,
                "member_id": member_id,
                "perm": perm,
                "type": collaborator_type,
            },
        )


class FeishuBaseSetup:
    """Set up and validate the static Base projection schema without data sync."""

    def __init__(
        self,
        client: FeishuBaseClient,
        config: ProjectConfig | None = None,
        store: StateStore | None = None,
        *,
        owner_email: str | None = None,
        viewer_chat_id: str | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.store = store
        self.owner_email = owner_email if owner_email is not None else os.getenv("FEISHU_OWNER_EMAIL", "")
        self.viewer_chat_id = viewer_chat_id if viewer_chat_id is not None else os.getenv("FEISHU_VIEWER_CHAT_ID", "")

    @classmethod
    def from_environment(cls) -> FeishuBaseSetup:
        return cls(FeishuBaseClient())

    def doctor(self) -> BaseDoctorResult:
        missing = [name for name, value in (("FEISHU_APP_ID", self.client.app_id), ("FEISHU_APP_SECRET", self.client.app_secret)) if not value]
        checks: dict[str, str] = {
            "credentials": "ok" if not missing else f"missing {', '.join(missing)}",
            "owner_permission": "configured" if self.owner_email else "not configured (optional)",
            "viewer_permission": "configured" if self.viewer_chat_id else "not configured (optional)",
            "bot_group_membership": "verify the app bot is already in the viewer chat before granting access",
        }
        if missing:
            return BaseDoctorResult(ok=False, checks=checks, detail="Configure credentials before Base setup.")
        try:
            self.client.tenant_token()
        except FeishuBaseError:
            checks["authentication"] = "failed; check application credentials and Base scopes"
            return BaseDoctorResult(ok=False, checks=checks, detail="Authentication capability check failed.")
        checks["authentication"] = "ok"
        if self.store is not None:
            app_token = self.store.get_projection_resource("base")
            if app_token:
                try:
                    self.client.list_tables(app_token)
                except FeishuBaseError:
                    checks["bitable_read"] = "failed; check bitable read scope and document access"
                    return BaseDoctorResult(ok=False, checks=checks, detail="Bitable read capability check failed.")
                checks["bitable_read"] = "ok"
        return BaseDoctorResult(ok=True, checks=checks, detail="Token capability check succeeded.")

    def setup(self) -> ProjectionReceipt:
        if self.config is None or self.store is None or self.config.feishu_base is None:
            raise FeishuBaseError("Feishu Base setup requires project configuration and a StateStore.")
        settings = self.config.feishu_base
        if not settings.enabled:
            raise FeishuBaseError("Feishu Base projection is disabled in configuration.")
        app_token, base_url, existing_tables = self._ensure_base(settings.base_name)
        initial_table_id = self._capture_initial_table(app_token, existing_tables)
        table_ids = self._ensure_tables(app_token, initial_table_id=initial_table_id, existing_tables=existing_tables)
        self._ensure_fields(app_token, table_ids, allow_primary_rename=initial_table_id is not None)
        # All initial table and primary-field migrations are complete; future
        # replays must treat this Base as mature before creating views/permissions.
        self.store.set_projection_resource("setup:phase", "complete", app_token)
        view_links = self._ensure_views(app_token, table_ids, base_url)
        self._ensure_permissions(app_token)
        self._write_manifest(app_token, base_url, table_ids, view_links)
        return ProjectionReceipt(adapter="feishu-base", synced=True, base_url=base_url, resource_links=view_links, detail="Base schema is ready.")

    def _ensure_base(self, name: str) -> tuple[str, str, list[dict[str, Any]]]:
        """Validate persisted resources; only explicit Base-not-found may use manifest fallback."""
        assert self.store is not None
        sqlite_token = self.store.get_projection_resource("base")
        sqlite_url = self.store.get_projection_resource_link("base")
        phase_token = (
            self.store.get_projection_resource_link("setup:phase")
            if self.store.get_projection_resource("setup:phase") == "base-created"
            else None
        )
        manifest_token, manifest_url = self._load_manifest_base()
        candidates: list[tuple[str, str | None]] = []
        # The phase marker is written before the normal Base mapping. Treat it
        # as a recovery candidate so an interruption between those local commits
        # never creates a second Base on restart.
        for token, url in (
            (sqlite_token, sqlite_url),
            (phase_token, None),
            (manifest_token, manifest_url),
        ):
            if isinstance(token, str) and token and token not in {item[0] for item in candidates}:
                candidates.append((token, url if isinstance(url, str) else None))
        if candidates:
            for index, (app_token, base_url) in enumerate(candidates):
                try:
                    existing_tables = self.client.list_tables(app_token)
                except FeishuBaseError as exc:
                    # The documented Bitable BaseTokenNotFound code is the only
                    # deterministic signal that permits switching to a manifest.
                    if exc.api_code == 1254040 and index < len(candidates) - 1:
                        continue
                    raise
                base_url = base_url or f"https://feishu.cn/base/{app_token}"
                self.store.set_projection_resource("base", app_token, base_url)
                return app_token, base_url, existing_tables
            raise FeishuBaseError("Feishu Base recovery failed: no usable persisted Base.", operation="base recovery")
        data = self.client.create_base(name)
        app = data.get("app", data)
        app_token = app.get("app_token") if isinstance(app, dict) else None
        if not isinstance(app_token, str) or not app_token:
            raise FeishuBaseError("Feishu Base creation response did not contain app_token.", operation="Base creation")
        base_url = app.get("url") if isinstance(app, dict) else None
        base_url = base_url if isinstance(base_url, str) and base_url else f"https://feishu.cn/base/{app_token}"
        # This local write precedes the first post-create network call.  It binds
        # a recoverable creation phase to exactly this newly returned app token.
        self.store.set_projection_resource("setup:phase", "base-created", app_token)
        self.store.set_projection_resource("base", app_token, base_url)
        existing_tables = self.client.list_tables(app_token)
        self._capture_initial_table(app_token, existing_tables)
        return app_token, base_url, existing_tables

    def _setup_phase_is_initial(self, app_token: str) -> bool:
        assert self.store is not None
        return (
            self.store.get_projection_resource("setup:phase") == "base-created"
            and self.store.get_projection_resource_link("setup:phase") == app_token
        )

    def _capture_initial_table(self, app_token: str, existing_tables: list[dict[str, Any]]) -> str | None:
        """Capture only the known creation-phase default table for this exact Base."""
        assert self.store is not None
        if not self._setup_phase_is_initial(app_token):
            return None
        stored = self.store.get_projection_resource("setup:initial_table")
        stored_for = self.store.get_projection_resource_link("setup:initial_table")
        if stored and stored_for == app_token:
            if any(table.get("table_id") == stored for table in existing_tables):
                return stored
            raise FeishuBaseError("Recorded initial table is missing from the Base.", operation="Base recovery")
        if len(existing_tables) != 1:
            raise FeishuBaseError("New Feishu Base did not expose exactly one default table.", operation="Base recovery")
        table_id = existing_tables[0].get("table_id")
        if not isinstance(table_id, str) or not table_id:
            raise FeishuBaseError("New Feishu Base default table is missing an ID.", operation="Base recovery")
        self.store.set_projection_resource("setup:initial_table", table_id, app_token)
        return table_id

    def _load_manifest_base(self) -> tuple[str | None, str | None]:
        assert self.config is not None and self.config.feishu_base is not None
        path = Path(self.config.feishu_base.manifest_path)
        if not path.exists():
            return None, None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None
        base = data.get("base", {}) if isinstance(data, dict) else {}
        return (base.get("id"), base.get("url")) if isinstance(base, dict) else (None, None)

    def _ensure_tables(
        self,
        app_token: str,
        *,
        initial_table_id: str | None,
        existing_tables: list[dict[str, Any]],
    ) -> dict[str, str]:
        assert self.store is not None
        by_name = {
            str(item.get("name")): str(item.get("table_id"))
            for item in existing_tables
            if item.get("name") and item.get("table_id")
        }
        competitors = TABLES[0]
        if initial_table_id and competitors.name not in by_name and len(existing_tables) == 1:
            default = existing_tables[0]
            if default.get("table_id") == initial_table_id:
                self.client.rename_table(app_token, initial_table_id, competitors.name)
                by_name[competitors.name] = initial_table_id
        known_ids = {str(item.get("table_id")) for item in existing_tables if item.get("table_id")}
        table_ids: dict[str, str] = {}
        for spec in TABLES:
            stored = self.store.get_projection_resource(f"table:{spec.key}")
            table_id = stored if stored in known_ids else by_name.get(spec.name)
            if not table_id:
                created = self.client.create_table(app_token, spec.name)
                table_id = created.get("table_id")
            if not isinstance(table_id, str) or not table_id:
                raise FeishuBaseError(f"Feishu did not return an ID for table {spec.key}.", operation="table setup")
            table_ids[spec.key] = table_id
            self.store.set_projection_resource(
                f"table:{spec.key}",
                table_id,
                f"https://feishu.cn/base/{app_token}?table={table_id}",
            )
        return table_ids

    def _ensure_fields(
        self,
        app_token: str,
        table_ids: dict[str, str],
        *,
        allow_primary_rename: bool,
    ) -> None:
        for spec in TABLES:
            table_id = table_ids[spec.key]
            listed = self.client.list_fields(app_token, table_id)
            existing = {str(item.get("field_name")): item for item in listed if item.get("field_name")}
            primary = next((item for item in listed if item.get("is_primary") is True), None)
            if primary is not None and primary.get("field_name") != spec.primary_field and allow_primary_rename:
                field_id = primary.get("field_id")
                field_type = primary.get("type")
                if not isinstance(field_id, str) or field_type != _TEXT:
                    raise FeishuBaseError(
                        f"Feishu default primary field is incompatible for {spec.key}.",
                        operation="field setup",
                    )
                self.client.update_field(
                    app_token,
                    table_id,
                    field_id,
                    {"field_name": spec.primary_field, "type": _TEXT},
                )
                existing.pop(str(primary.get("field_name")), None)
                primary = {**primary, "field_name": spec.primary_field}
                existing[spec.primary_field] = primary
            for field in spec.fields:
                current = existing.get(field.name)
                if current is not None:
                    if current.get("type") != field.type:
                        raise FeishuBaseError(f"Feishu field type mismatch for {spec.key}.{field.name}.", operation="field setup")
                    if field.relation_to:
                        property_data = current.get("property", {})
                        expected = table_ids[field.relation_to]
                        if not isinstance(property_data, dict) or property_data.get("table_id") != expected:
                            raise FeishuBaseError(f"Feishu relation mismatch for {spec.key}.{field.name}.", operation="field setup")
                    continue
                payload: dict[str, Any] = {"field_name": field.name, "type": field.type}
                if field.relation_to:
                    payload["property"] = {
                        "table_id": table_ids[field.relation_to],
                        "multiple": True,
                        "back_field_name": f"{spec.name}\u5173\u8054",
                    }
                self.client.create_field(app_token, table_id, payload)

    def _ensure_views(self, app_token: str, table_ids: dict[str, str], base_url: str) -> dict[str, str]:
        assert self.store is not None
        links: dict[str, str] = {}
        for table_key, name, view_type in VIEWS:
            table_id = table_ids[table_key]
            existing = {str(item.get("view_name")): item for item in self.client.list_views(app_token, table_id) if item.get("view_name")}
            current = existing.get(name)
            if current is not None:
                if current.get("view_type") != view_type:
                    raise FeishuBaseError(f"Feishu view type mismatch for {name}.")
                view_id = current.get("view_id")
            else:
                view_id = self.client.create_view(app_token, table_id, name, view_type).get("view_id")
            if not isinstance(view_id, str) or not view_id:
                raise FeishuBaseError(f"Feishu did not return an ID for view {name}.")
            link = f"{base_url}?table={table_id}&view={view_id}"
            self.store.set_projection_resource(f"view:{table_key}:{name}", view_id, link)
            links[name] = link
        return links

    def _ensure_permissions(self, app_token: str) -> None:
        if self.owner_email:
            self.client.grant_permission(
                app_token,
                member_type="email",
                member_id=self.owner_email,
                perm="edit",
                collaborator_type="user",
            )
        if self.viewer_chat_id:
            self.client.grant_permission(
                app_token,
                member_type="openchat",
                member_id=self.viewer_chat_id,
                perm="view",
                collaborator_type="chat",
            )

    def _write_manifest(self, app_token: str, base_url: str, table_ids: dict[str, str], view_links: dict[str, str]) -> None:
        assert self.config is not None and self.config.feishu_base is not None
        path = Path(self.config.feishu_base.manifest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        view_ids = {name: self.store.get_projection_resource(f"view:{table_key}:{name}") for table_key, name, _ in VIEWS} if self.store else {}
        payload = {
            "schema_version": 1,
            "base": {"id": app_token, "url": base_url},
            "tables": table_ids,
            "views": {name: {"id": view_ids.get(name), "url": url} for name, url in view_links.items()},
        }
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
