# GitHub 交付 Agent

在同一个入口完成提交前检查、提交和推送、PR 审查，以及合并后的部署。适用于 GitHub 的 `main` 分支，操作命令从本仓库根目录运行，在 macOS 或 Linux 上需要 Python 3.11+、Git 和已登录的 GitHub CLI。生成审查报告还需要已登录的 Codex CLI。常驻进程使用独立的可信 checkout。

## 提交并创建 PR

先按项目规则暂存本次要提交的文件，再从功能分支运行。

```sh
python -m delivery_agent submit --repo OWNER/REPO -m "describe the change"
```

Agent 检查语法和暂存 diff，提交已暂存的文件，推送经过检查的提交，然后创建或复用指向 `main` 的 PR。已有本地提交时可以省略 `-m`。GitHub 的 [Delivery CI](../.github/workflows/ci.yml) 在 PR 更新和 `main` 推送时运行 Python 语法检查。可把该检查设为合并条件。

## 审查 PR

```sh
python -m delivery_agent review --repo OWNER/REPO 123
python -m delivery_agent watch --repo OWNER/REPO --publish
```

`review` 默认把报告输出到终端。加上 `--publish` 后，使用当前 GitHub CLI 身份提交 `COMMENT` 类型的 review。常驻 `watch` 默认每 60 秒检查待审的 PR，也可以用 `--once` 单次运行。审查只读取 PR diff，Codex 使用临时目录，关闭 Shell、浏览器和 Web 搜索。发布前会再次核对提交版本，已回写的相同 PR 提交会被跳过。

建议让 Bot 账号只访问指定仓库。它需要 PR 读权限，自动发布时另需 PR 写权限。启用部署时也需要 Contents 和 Actions 的读权限。

## 部署

人合并 PR 后，Agent 检查该 PR 的合并提交是否仍是远端 `main` 的最新提交，以及这个确切提交的推送 CI 是否成功。执行脚本由运行者维护，并用 `DELIVERY_REPO`、`DELIVERY_PR` 和 `DELIVERY_SHA` 接收部署目标。脚本应发布 `DELIVERY_SHA` 指定的版本，健康检查成功后以状态码 0 退出。

```sh
python -m delivery_agent deploy --repo OWNER/REPO 123 --deploy-script /absolute/path/to/deploy.sh

python -m delivery_agent watch --repo OWNER/REPO --publish \
  --deploy-script /absolute/path/to/deploy.sh
```

提供 `--deploy-script` 才会启用自动部署。启动时会检查当前 `main`，已有符合条件的合并提交时也会处理。每个仓库用一个 Agent 实例，默认状态目录为 `~/.local/state/feishu-delivery/`。成功的部署按提交去重，失败或中断后先检查环境，再运行 `deploy --retry`。
