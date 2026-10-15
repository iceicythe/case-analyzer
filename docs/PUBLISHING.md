# 首次发布到 GitHub

此目录就是仓库根目录，上传其中的源码、公开样例与文档即可。运行数据、个人配置和历史归档已通过 `.gitignore` 排除。

## 本地核对

```shell
python -B -X utf8 -m unittest discover -s tests -v
python scripts/check_repo.py
git status --short
git diff --cached --stat
```

本地已初始化 Git 时无需重复 init。没有初始化时运行 `git init -b main`。首次提交前可按自己的身份修改 LICENSE 中的 copyright 行，保留完整 MIT 条款。

## 提交和推送

第一次使用 Git 时，需要先设置本仓库的提交署名和邮箱，否则 commit 可能提示 Author identity unknown：

```shell
git config user.name "你的GitHub用户名"
git config user.email "你的GitHub提交邮箱"
```

替换为自己的信息。邮箱建议从 GitHub Settings → Emails 中复制提供的 noreply 隐私邮箱，或者使用已验证邮箱。这两条不带 --global，只影响当前仓库。提交身份与 GitHub 登录认证是两回事。

先在 GitHub 新建空仓库，例如 `case-analyzer`，不要再自动生成另一份 README、License 或 .gitignore。然后在当前目录执行：

```shell
git add .
git diff --cached --stat
git commit -m "Initial open-source release"
git remote add origin <你的仓库HTTPS地址>
git push -u origin main
```

将尖括号内内容替换为真实仓库地址。Git 作者身份与认证使用自己的配置；不要在远程 URL 中写入 Token。已经有 origin 时先用 `git remote -v` 核对，无需再次添加。

以上命令每行执行一条，行尾不加反斜杠。远程地址使用纯 HTTPS 地址，不要粘贴 Markdown 链接的方括号和圆括号。若 push 提示认证失败，请按 GitHub/Git Credential Manager 的登录流程认证；不要把 GitHub 账户密码当作 HTTPS Git 密码。

推送后检查 README 截图和 Mermaid 是否显示，查看 Actions 里的四组测试结果；本地成功不等于远端 CI 已运行。仓库 About 可填写：

> A local workbench for driving-log event review, evidence-based investigation, and offline reports.

可用 topics：`python`、`adas`、`autonomous-driving`、`log-analysis`、`testing-tools`、`llm`。

需要发布下载包时，运行 `python scripts/build_release.py artifacts/case-analyzer-0.1.0.zip`，再自行添加到 GitHub Release。构建脚本仅使用公开资源与空配置模板。

参考：[GitHub 已有本地代码推送说明](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)、[Python Actions 官方指南](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)。
