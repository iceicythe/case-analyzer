# Case Analyzer

**面向智能驾驶测试日志的事件复核与排查工作台。**

把 CSV 日志变成可检查、可追溯、可交付的 Case：规则筛选减速候选，AI 或人工完成事件复核，再结合车辆、目标、规划和控制证据生成初步排查报告。

`Python 3.11+` · `原生 JavaScript` · `本地单人运行` · `MIT`

![Case Analyzer 事件工作台](docs/images/workbench.png)

*截图使用仓库内公式生成的演示数据。*

[快速开始](#快速开始) · [体验样例](#体验样例) · [数据格式](docs/DATA_FORMAT.md) · [工作原理](docs/ARCHITECTURE.md) · [参与开发](CONTRIBUTING.md)

## 为什么做这个项目

测试日志中的一次车速下降，往往需要经历“找到片段 → 确认事件 → 对齐各模块日志 → 整理证据 → 提出检查方向”。这些步骤容易反复切换文件，也容易把观测现象直接当作故障原因。

Case Analyzer 把这条流程放到同一个工作台中：保留原始引用，明确证据缺口，让模型在受约束的范围内参与复核和检查排序，让人工处理冲突与不确定结果。

## 能做什么

| 能力 | 具体行为 |
|---|---|
| 数据接入 | 校验 CSV、时间和车速，记录来源与导入哈希，拒绝覆盖同名 Case |
| 候选与复核 | 规则筛选候选；AI 可用时进入复核队列，不确定或不合规结果转人工 |
| 证据查看 | 查看车速、加速度及模块信号，定位原始 CSV 行；说明缺失与匹配歧义 |
| 初步排查 | 整理规划原因、目标变化、请求与响应；给出检查步骤、待补材料和判断分支 |
| 记录与恢复 | 保存人工确认、检查状态和备注；处理版本冲突、输入变化及任务中断 |
| 报告交付 | 导出 Markdown、JSON 和可离线打开的 HTML 报告包 |

**不配置 API Key 也能完成导入、人工确认、本地排查和报告导出。**

## 快速开始

克隆或下载本仓库，在仓库根目录执行。需要 Python 3.11 或更新版本，首次安装依赖需要联网。

### Windows

```powershell
.\setup.cmd
.\start.cmd --no-auto-review
```

也可以依次双击 `setup.cmd` 和 `start.cmd`。安装脚本会创建本目录专用的 `.venv`，启动脚本优先使用该环境。

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app --no-auto-review
```

浏览器打开 **http://127.0.0.1:8770/**。首次启动是空工作空间，数据保存在自动创建的 `runtime/` 中。按 `Ctrl+C` 停止，重启保留已经保存的记录。

端口占用时加 `--port 8771`；不自动打开浏览器时加 `--no-open`；使用另一份数据目录时加 `--runtime <目录>`。

## 体验样例

仓库自带三个不依赖外部数据的合成样例：正常减速、多目标歧义、无减速事件。保持服务运行，在另一个终端执行：

```shell
python scripts/import_demo.py
```

然后：

1. 在任务总览打开 `demo_deceleration`，核对车速、加速度及候选区间。
2. 进入人工确认，核对整段数据和来源，保存事件区间，再保存候选与事件的关联结论。
3. 生成本地排查报告，查看证据、检查步骤和待补材料。
4. 保存检查备注，在报告中心导出离线报告包。

也可直接在“数据接入”选择 `examples/demo_deceleration/` 中的全部 CSV。详细操作和样例设计见 [演示指南](docs/DEMO.md) 与 [样例说明](examples/README.md)。

## 接入 AI（可选）

当前提供 DeepSeek 适配器。先复制配置模板，再在本机填写自己的 Key：

```powershell
Copy-Item configs/deepseek.example.json configs/deepseek.local.json
```

Linux / macOS 使用 `cp configs/deepseek.example.json configs/deepseek.local.json`。如果运行过 Windows 安装脚本，空白本地配置已经生成，直接编辑即可。

```json
{
  "api_key": "",
  "model": "deepseek-flash",
  "base_url": "https://api.deepseek.com",
  "reasoning_effort": "low",
  "max_tokens": 8192,
  "timeout_seconds": 180,
  "max_retries": 2
}
```

用当前 Python 环境运行 `python -m app --check-api` 检查连接，再重启工作台并去掉 `--no-auto-review`。Windows 可双击 `check-api.cmd` 检查、`start.cmd` 启动。连接检查本身会发出一次短请求。

也支持 `DEEPSEEK_API_KEY` 环境变量；本地配置中的非空 Key 优先。`deepseek.local.json` 已被 Git 忽略。启用模型后，当前步骤的日志片段或证据摘要会发送至所配置的服务，调用可能产生费用。参数说明见 [API 配置](docs/API_CONFIG.md)。

## 工作原理

```mermaid
flowchart LR
    A[CSV 与来源说明] --> B[格式与质量检查]
    B --> C[规则筛选候选]
    C --> D[AI 或人工复核]
    D -->|不确定或冲突| E[人工核查]
    E --> D
    D -->|确认区间| F[多模块证据整理]
    F --> G[本地检查清单 / AI 排序]
    G --> H[保存核查记录并导出]
```

- **规则**确定候选、数值口径和证据约束，数据质量不足时保留待核查状态。
- **AI**参与候选复核，并从已定义的检查项中选择排序；输出要经过结构、引用和区间校验。
- **人工**核对不确定结果，确认事件、执行检查并记录结果。模型不能替人工确认软件根因。

排查模型只接收程序计算的摘要和检查条件；完整证据留在本地。事实与具体操作由程序组装，模型不能改写为无证据的故障结论。详细数据流见 [架构说明](docs/ARCHITECTURE.md)。

## 数据要求与使用范围

支持以下归一化 CSV。车辆日志必需，其他模块可以缺省，缺失会明确写入报告。

| 文件 | 主要内容 |
|---|---|
| `vehicle_state.csv` | 时间、车速，可附加加速度与制动反馈 |
| `object_state.csv` | 目标 ID、距离、相对速度、置信度 |
| `planning_state.csv` | 目标车速、规划加速度、决策原因 |
| `control_state.csv` | 制动/油门请求、请求加速度 |

时间使用秒，车速使用 m/s，车辆采样按当前规则要求为 0.1 秒。当前事件定义为项目自定的减速数值口径，具体阈值与合并条件见 [数据与事件约定](docs/DATA_FORMAT.md)。

减速事件可能属于正常驾驶行为。日志里的 `decision_reason`、近邻时间匹配、目标 ID 变化和 TTC 参考均需结合其他证据解释，不能单独证明故障原因。

当前定位为本地单人分析工具；未提供多用户鉴权或团队协作服务。仓库样例用于演示，未发布独立真实道路检测成绩。评测时应使用独立标签与固定测试集，参考 [评测方法](docs/EVALUATION.md)。

## 开发与测试

```shell
python -m pip install -r requirements.txt
python -B -X utf8 -m unittest discover -s tests -v
python scripts/check_repo.py
```

默认测试使用临时目录、公式样例和模拟模型响应，无需 Key，不调用真实 API。覆盖导入、回滚、并发、人工确认、队列恢复、保存冲突、报告导出、事件匹配及排查约束。浏览器测试另行安装，见 [贡献指南](CONTRIBUTING.md)。

已配置 GitHub Actions 的 Windows / Ubuntu、Python 3.11 / 3.12 测试矩阵；实际执行状态以仓库 Actions 为准。

```text
case-analyzer/
├── app/          后端、复核队列、证据与报告
├── static/       HTML、CSS、JavaScript
├── configs/      规则与公开模型配置模板
├── knowledge/    排查知识目录与手册
├── examples/     可再生成的合成 CSV 样例
├── scripts/      样例导入、仓库检查与打包
├── tests/        自包含测试，无历史数据依赖
├── docs/         数据约定、架构、演示与评测方法
└── runtime/      本机运行时创建，Git 忽略
```

后续重点是独立数据评测、批量与长期运行验证、报告去重及更多日志格式适配。欢迎提交最小可复现问题与改进，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

维护者首次发布可参考 [GitHub 发布步骤](docs/PUBLISHING.md)。

## License

源码与随仓库提供的合成样例采用 [MIT License](LICENSE)。依赖库保留各自许可证；使用者自行导入的数据不属于本仓库发布内容。
