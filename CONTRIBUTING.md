# 参与开发

欢迎改进日志接入、证据展示、可执行的检查步骤和可复现评测。提交问题时请提供版本、操作步骤、预期/实际行为及最小合成或脱敏样例，不附带密钥、个人日志和未经授权的数据。

## 开发环境

Python 3.11+。安装 `requirements.txt` 后，在仓库根目录运行：

```shell
python -B -X utf8 -m unittest discover -s tests -v
python scripts/check_repo.py
```

默认测试不调用真实模型。新增测试应自己生成数据或使用公开合成样例，不引用个人机器路径、历史运行目录或私有数据。

可选浏览器测试需要额外安装：

```shell
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -B -X utf8 -m unittest discover -s tests -p browser_daily.py -v
```

`BROWSER_CHANNEL=msedge` 可改用已安装的 Edge。浏览器测试模拟模型响应；真实 API 检查由使用者按需执行。

## 修改原则

- 修改事件阈值或合并规则时，同步更新配置、数据约定和能说明行为变化的测试。
- 保留缺失、歧义与未知，不把零值自动解释为“没有请求”。
- AI 输出需要通过现有校验；日志文本作为证据，不当作执行指令。
- 保存路径或指纹变化需要考虑旧记录失效、并发冲突和导出行为。
- 不把模拟响应、脚本填写或 AI 审阅结果标成人工评分。

PR 说明改动解决的问题、具体行为和验证方式。版本历史见 [CHANGELOG.md](CHANGELOG.md)。
