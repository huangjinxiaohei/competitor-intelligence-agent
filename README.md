# 竞品情报循环 Agent

这是一个无前端、可移植的竞品情报 Agent。SQLite 是事实库；飞书 Base 是可重建的可视化投影；群机器人只负责首轮基线和已确认变化的提醒。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test,browser]"
Copy-Item .env.example .env
Copy-Item config/project.example.yaml config/project.yaml
```

编辑 `config/project.yaml` 中的行业主题、关键词与 `seed_urls`，运行完整三轮夹具验收：

```powershell
.\.venv\Scripts\python.exe -m competitor_agent.cli doctor --fixture
.\.venv\Scripts\python.exe -m competitor_agent.cli run --fixture
.\.venv\Scripts\python.exe -m competitor_agent.cli run --fixture
.\.venv\Scripts\python.exe -m competitor_agent.cli run --fixture
```

预期：第 1 轮建立基线并发送一次提醒；第 2 轮结构化价格、配置变化再发一次；第 3 轮无变化，群机器人 0 次调用。所有夹具命令使用 mock 适配器。

## 飞书 Base 投影

1. 在飞书开放平台创建自建应用，开通 Bitable 与协作者权限，将应用机器人加入目标群。
2. 在 `.env` 写入 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、可选 `FEISHU_OWNER_EMAIL` 和 `FEISHU_VIEWER_CHAT_ID`。凭据不写入 YAML。
3. 在 YAML 设置 `adapters.projection: feishu-base` 与 `feishu_base.enabled: true`，再运行：

```powershell
.\.venv\Scripts\python.exe -m competitor_agent.cli base-doctor --config config/project.yaml
.\.venv\Scripts\python.exe -m competitor_agent.cli base-setup --config config/project.yaml
.\.venv\Scripts\python.exe -m competitor_agent.cli run --config config/project.yaml
```

`base-doctor` 只读检查凭据和权限；`base-setup` 幂等创建并回填本地事实；`base-resync` 仅重放 SQLite 投影，不重新采集官网。

Base 包含四张表：竞品主表、套餐价格表、变化事件表、运行日志表；并创建竞品总览、竞品卡片、低置信度、价格对比、本周变化、高优先级变化、指标总览、采集异常、运行历史 9 个视图。投影是单向的：Agent 只写机器字段，保留人工关注级别、标签和备注。

每轮都先落本地快照和报告，再同步 Base。Base 失败会使运行标为 partial 并写入重试 outbox；已确认的变化仍会带官方证据提醒，不会带过期 Base 链接。

## 模型与采集

每个竞品最多采集 6 个同注册域公开页面，优先定价、产品、功能、文档和安全页。`hybrid-openai` 先使用规则提取明确数字价格，再用 OpenAI 兼容模型补齐套餐语义、配置和中文摘要。模型超时、限流或证据不匹配时自动降级为规则结果并在运行日志标注。

## 开发验收

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=competitor_agent --cov-fail-under=85
.\.venv\Scripts\python.exe -m compileall -q src tests
```

跨版本迁移：稳定竞品 ID 按注册域生成，旧 ID 映射、快照、变化与缺失计数会一起迁移。
