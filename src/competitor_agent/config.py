from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSettings(ConfigModel):
    id: str
    topic: str
    keywords: list[str] = Field(min_length=1)
    seed_urls: list[str] = Field(default_factory=list)
    output_language: str = "zh-CN"


class DiscoverySettings(ConfigModel):
    auto_monitor_threshold: float = Field(default=0.75, ge=0, le=1)
    pending_threshold: float = Field(default=0.45, ge=0, le=1)
    max_candidates: int = Field(default=20, ge=1)
    max_monitored: int = Field(default=10, ge=1)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "feature_overlap": 0.40,
            "audience_overlap": 0.25,
            "business_model_overlap": 0.20,
            "keyword_match": 0.15,
        }
    )

    @model_validator(mode="after")
    def validate_thresholds_and_weights(self) -> DiscoverySettings:
        if self.pending_threshold >= self.auto_monitor_threshold:
            raise ValueError(
                "pending_threshold must be lower than auto_monitor_threshold"
            )
        if abs(sum(self.weights.values()) - 1.0) > 1e-9:
            raise ValueError("discovery weights must sum to 1.0")
        return self


class CollectionSettings(ConfigModel):
    timeout_seconds: float = Field(default=20, gt=0)
    retries: int = Field(default=3, ge=0)
    per_domain_delay_seconds: float = Field(default=1.0, ge=0)
    max_pdf_megabytes: int = Field(default=20, ge=1)
    browser_fallback: bool = True
    max_pages_per_candidate: int = Field(default=6, ge=1, le=100)


class FeishuBaseSettings(ConfigModel):
    enabled: bool = False
    base_name: str = Field(default="Competitor Intelligence", min_length=1)
    manifest_path: str = Field(default="state/feishu_base_manifest.json", min_length=1)
    sync_every_run: bool = True

class ScheduleSettings(ConfigModel):
    timezone: str = "Asia/Shanghai"
    daily_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AdapterSettings(ConfigModel):
    search: str = "host"
    analyzer: str = "heuristic"
    delivery: str = "mock"
    projection: str = "mock"


class StorageSettings(ConfigModel):
    database: str = "state/competitive_intel.db"
    reports_dir: str = "reports"


class ProjectConfig(ConfigModel):
    project: ProjectSettings
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    collection: CollectionSettings = Field(default_factory=CollectionSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    adapters: AdapterSettings = Field(default_factory=AdapterSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    feishu_base: FeishuBaseSettings | None = None


def load_config(path: str | Path) -> ProjectConfig:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return ProjectConfig.model_validate(raw)

