# API 配置

配置位置：仓库根目录下的 `configs/deepseek.local.json`。首次使用可从 `configs/deepseek.example.json` 复制；Windows 安装脚本会在文件不存在时自动创建空白配置。

打开文件，在 `"api_key": ""` 的空双引号中填写自己的 DeepSeek API Key，保存即可。其他参数可以先保持默认。JSON 不支持注释，最后一个字段后不要增加逗号。

| 字段 | 用途 |
| --- | --- |
| api_key | API 密钥，由使用者填写 |
| model | 模型名称，默认 deepseek-flash |
| base_url | 服务地址，默认 https://api.deepseek.com |
| reasoning_effort | 业务复核的思考强度，默认 low；支持 low、high、max |
| max_tokens | 业务调用输出预算，默认 8192 |
| timeout_seconds | 业务调用超时，默认 180 秒 |
| max_retries | 业务调用遇到可重试错误时的额外重试次数，默认 2 |

模型名称与思考参数已按 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/) 核对。当前适配器使用 DeepSeek 的 Chat Completions 接口。

## 使用顺序

1. 填写配置并保存。
2. 双击 `check-api.cmd`，看到“API 连接成功”。其他系统使用当前 Python 环境执行 `python -m app --check-api`。该操作调用真实服务，发送一次短 JSON 请求，不读取数据或启动队列；输出上限 128 Token，关闭思考模式，超时 30 秒，不自动重试。
3. 停止已运行的工作台，重新双击 `start.cmd`，使自动队列读取新配置。
4. 导入合成 Case，打开事件工作台观察自动确认、排除及转人工结果。

事件复核与排查建议使用同一份配置。事件复核使用配置的思考强度；排查检查项排序单独关闭思考模式，不发送 reasoning_effort。连接检查只证明服务能响应，业务输出还需通过程序校验和案例复核。

## 连接失败

缺少密钥或文件格式错误时不会发请求。真实服务错误按 [官方错误码](https://api-docs.deepseek.com/quick_start/error_codes/) 提示：401 检查密钥，402 检查余额，429 稍后重试，400/422 检查参数；其他连接问题检查网络、服务地址和服务状态。检查入口不输出密钥、服务商原始异常或完整响应。

本地密钥文件已加入开发目录的忽略规则。打包或分享项目时，不携带填写过的个人配置。正式交付包从模板生成 api_key 为空的 `deepseek.local.json`，方便接收者自行填写。

安装独立环境后，`start.cmd` 和 `check-api.cmd` 优先使用本目录 `.venv` 中的 Python；未安装时使用系统 Python。
