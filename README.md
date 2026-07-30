# 竞品资料循环 Agent

这是一个无前端、可移植的同行竞品资料 Agent。它按照固定管线完成候选发现、官网采集、字段抽取、证据校验、历史快照、差异检测、报告导出和协作平台消息推送。

默认配置面向“Agent 工具与平台”示例行业，不硬编码真实竞品。没有外部资料或凭据时，可以先使用内置网页夹具完成三轮闭环验证。

## 核心流程

```text
发现 → 官网归一化 → 竞品评分 → 页面采集 → 字段抽取 → 证据校验
     → 快照入库 → 差异检测 → 摘要生成 → 推送
```

- 搜索摘要只用于发现候选，正式结论来自官网、定价页、官方文档或官方文本型 PDF。
- SQLite 保存候选、来源、快照、变化、运行日志和推送回执。
- 每轮运行导出 Markdown、JSON 和 CSV 报告。
- 首轮建立基线并生成全景摘要；后续仅在价格、可用性、关键参数或配置发生有效变化时推送。
- 字段连续两轮缺失才确认删除；单个来源采集失败时保留上次有效快照。
- 运行锁阻止同一项目并发执行。

## 安装

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test,browser]"
python -m playwright install chromium
Copy-Item .env.example .env
```

`.env` 只用于本地 CLI 和适配器凭据，已被 Git 忽略。凭据不会写入 YAML 或代码。

## 常用命令

安装项目后可以使用命令入口：

```powershell
competitor-agent doctor --fixture
competitor-agent discover --fixture --dry-run
competitor-agent run --fixture
competitor-agent report RUN_ID
competitor-agent push-test --fixture
```

也可以直接通过 Python 模块运行：

```powershell
.\.venv\Scripts\python.exe -m competitor_agent.cli doctor --fixture
.\.venv\Scripts\python.exe -m competitor_agent.cli run --fixture
```

- `doctor`：检查配置、夹具和消息适配器是否就绪。
- `discover`：只执行候选发现和评分。
- `run`：执行完整采集、分析、存储、报告与推送管线。
- `report RUN_ID`：读取指定运行的 Markdown 报告。
- `push-test`：强制使用内存模拟适配器验证消息发布链路。
- `--dry-run`：执行分析但跳过消息发布；非夹具完整运行时只返回发现结果。

## 三轮夹具闭环

`fixtures/manifest.json` 定义了三个连续轮次：

1. `round_01_baseline`：建立价格和配置基线。
2. `round_02_changed`：包含价格、功能、配置及纯文案变化，只报告有效字段变化。
3. `round_03_changed`：结构化字段与第二轮相同，用于验证排版和文案变化不会误报。

在同一工作区连续执行：

```powershell
competitor-agent run --fixture
competitor-agent run --fixture
competitor-agent run --fixture
```

SQLite 数据库默认写入 `state/competitive_intel.db`，Markdown、JSON 和 CSV 报告默认写入 `reports/YYYY-MM-DD/`。

若想从第一轮重新开始，请在确认不再需要历史数据后删除本地 `state/` 和 `reports/` 运行产物；这两个目录默认不会提交到 Git。

## 接入宿主搜索 Agent

先在 `config/project.yaml` 中设置行业主题、关键词和可选的 `seed_urls`。具备联网搜索能力的宿主 Agent 将结果写入：

```text
state/host_search_results.json
```

支持简单 JSON 数组：

```json
[
  {
    "title": "Candidate",
    "url": "https://candidate.example",
    "snippet": "agent platform pricing"
  }
]
```

也支持按关键词分组：

```json
{
  "agent platform": [
    {
      "title": "Candidate",
      "url": "https://candidate.example",
      "snippet": "agent platform pricing"
    }
  ]
}
```

随后执行 `competitor-agent discover` 或 `competitor-agent run`。正式采集、证据校验、快照、差异、报告和发布仍由确定性 Python 管线完成。

## 接入外部模型

默认 `adapters.analyzer: heuristic` 使用本地启发式抽取。若要使用兼容 JSON 的模型服务，将 YAML 配置改为 `http-json`，并在 `.env` 中设置：

```dotenv
MODEL_API_URL=https://model-endpoint.example/v1/extract
MODEL_API_KEY=...
MODEL_NAME=...
```

模型响应必须符合 `schemas/ProductSnapshot.schema.json`。确定性管线会校验结构、来源 URL 和 Evidence，拒绝缺少官方证据的字段。

## 接入协作平台机器人

在 `config/project.yaml` 中将 `adapters.delivery` 设置为 `webhook`，或通过环境变量覆盖：

```dotenv
DELIVERY_ADAPTER=webhook
DELIVERY_WEBHOOK_URL=https://collaboration-webhook.example/...
```

发布结果会保存为 `DeliveryReceipt`。增量消息最多展示 5 条重点变化，其余内容保存在完整报告中。

## 定时运行

安装脚本默认只展示计划，显式传入 `-Apply` 才会注册或删除 Windows 定时任务：

```powershell
.\scripts\install_schedule.ps1
.\scripts\install_schedule.ps1 -Apply
.\scripts\uninstall_schedule.ps1
.\scripts\uninstall_schedule.ps1 -Apply
```

默认每天 09:00 运行。Windows 任务计划程序使用计算机当前时区；若要按北京时间执行，请将系统时区设置为 `Asia/Shanghai` 对应时区。

## 测试与质量检查

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=competitor_agent --cov-report=term-missing
.\.venv\Scripts\python.exe -m compileall -q src tests
```

项目要求核心模块覆盖率不低于 85%。跨宿主数据契约位于 `schemas/`，宿主任务约定见 `agent/contract.md`。

## 飞书竞品概览推送

飞书卡片最多展示 5 个竞品概览。每个概览包含最多 5 项功能、3 条价格、3 项配置、置信度以及 2 个证据链接。缺失的抽取字段显示为“暂未识别”。除非新增托管适配器，完整报告仅保留在本地。
