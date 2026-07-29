from pathlib import Path

import pytest
from pydantic import ValidationError

from competitor_agent.config import ProjectConfig, load_config


def test_config_has_locked_mvp_defaults() -> None:
    config = ProjectConfig(
        project={"id": "demo", "topic": "Agent tools", "keywords": ["agent"]},
    )

    assert config.discovery.auto_monitor_threshold == 0.75
    assert config.discovery.pending_threshold == 0.45
    assert config.discovery.max_candidates == 20
    assert config.discovery.max_monitored == 10
    assert config.schedule.timezone == "Asia/Shanghai"
    assert config.schedule.daily_time == "09:00"


def test_config_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValidationError, match="pending_threshold"):
        ProjectConfig(
            project={"id": "demo", "topic": "Agent tools", "keywords": ["agent"]},
            discovery={"auto_monitor_threshold": 0.4, "pending_threshold": 0.5},
        )


def test_load_config_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "project.yaml"
    path.write_text(
        """
project:
  id: demo
  topic: Agent tools
  keywords: [agent, automation]
schedule:
  daily_time: "18:00"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.project.keywords == ["agent", "automation"]
    assert config.schedule.daily_time == "18:00"
