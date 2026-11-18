# Feishu Codex Agent

一个通过飞书群聊触发 Codex 的轻量任务桥接服务。服务接收飞书 Webhook，将任务持久化到 SQLite，再通过受限制的 SSH 命令交给指定工作机上的 Codex 执行，并把结果发回原飞书群。

```text
飞书群 @机器人
    |
    v
FastAPI Webhook -> SQLite 队列 -> SSH -> Codex
    ^                                  |
    |                                  v
飞书结果卡片 <---------------------- 最终回复
```

## 功能

- 验证飞书回调，只处理配置的应用、群和消息类型。
- Webhook 仅在消息 `@` 到机器人时入队。示例配置默认关闭单独的消息轮询。
- `FEISHU_CHAT_IDS` 为空时接收机器人所在的所有群，非空时只接收指定群。
- 使用 SQLite 保存待处理、执行中、成功和失败状态，服务重启后可继续处理。
- 通过 SSH 标准输入传递任务，不把用户文本拼接为 shell 命令。
- 工作机使用独立的 `CODEX_HOME` 运行 `codex exec`。
- 支持把结果发送回来源群，并可通过消息 ID 去重。

## 飞书应用配置

创建企业自建应用并开启机器人能力，至少配置：

- 事件：`im.message.receive_v1`
- 权限：接收群消息、读取消息、发送机器人消息，以及读取所需群信息的权限
- 回调地址：`https://<your-domain>/feishu/codex/events`

如果飞书开启了事件加密，同时配置 `FEISHU_ENCRYPT_KEY`。

## 服务端启动

在 Linux 服务器上进入仓库目录，使用 Python 3.11 或更新版本：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
install -m 600 .env.example .env
# 填写飞书凭证、可信群 ID、SQLite 路径和 SSH 连接配置
uvicorn feishu_webhook:create_app --factory --env-file .env --host 127.0.0.1 --port 8766
```

长期运行时，按实际部署目录和服务账号调整 [systemd 模板](deploy/feishu-codex.service)。队列目录须已创建并允许服务账号写入，SSH 私钥和 `known_hosts` 须能被服务账号读取。将域名解析到 Nginx 所在服务器，准备 TLS 证书后，替换 [Nginx 模板](deploy/feishu-codex.conf) 中的域名和证书路径，检查配置并加载。飞书回调使用公网上的 HTTPS 地址，Nginx 将该路径转发到本机 `8766`。

## 工作机配置

工作机需要：

1. 用运行 Hook 的账号登录 Codex CLI，并在工作机的仓库目录建虚拟环境，安装 `requirements.txt`。
2. 将 `.env.example` 复制为权限 `600` 的 `.env.feishu-codex.local`，填写工作目录、Codex 路径和模型。要回发结果，另填写飞书应用凭证和 `FEISHU_CODEX_NOTIFY_CHAT_ID`，后者用于来源群查询失败时的回退。
3. 为服务端专用公钥配置 `authorized_keys`。用 `restrict,command="<虚拟环境 Python 绝对路径> <feishu_codex_hook.py 绝对路径>"` 固定执行入口。Hook 通过 `SSH_ORIGINAL_COMMAND` 判断 `run` 或 `check`。
4. 从服务器用专用密钥和已核验的工作机主机公钥执行 SSH `check`。返回 `{"status": "ready"}` 后，在飞书配置回调并用一条 `@机器人` 消息验证回包。

飞书 Hook 使用 `.env.feishu-codex.local`。该文件和 `.env` 均已被 `.gitignore` 排除。

## 主要环境变量

| 变量 | 说明 |
| --- | --- |
| `FEISHU_APP_ID` | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `FEISHU_BOT_OPEN_ID` | 机器人的 Open ID，用于校验 @ 对象 |
| `FEISHU_VERIFICATION_TOKEN` | 飞书事件 Verification Token |
| `FEISHU_ENCRYPT_KEY` | 飞书事件 Encrypt Key，可选 |
| `FEISHU_CHAT_IDS` | 允许接收消息的群，逗号分隔；为空表示不限群 |
| `FEISHU_CODEX_STATE_FILE` | SQLite 队列路径 |
| `FEISHU_CODEX_SSH_TARGET` | 工作机 SSH 目标 |
| `FEISHU_CODEX_SSH_KEY` | 服务端连接工作机的 SSH 私钥路径 |
| `FEISHU_CODEX_SSH_KNOWN_HOSTS` | 固定工作机主机公钥的 known_hosts 路径 |
| `FEISHU_CODEX_REPO` | 工作机上的运行目录及状态目录 |
| `FEISHU_CODEX_MODEL` | Codex 模型，例如 `gpt-6.2-sol` |
| `FEISHU_CODEX_USE_EXEC` | 设为 `1` 时使用 `codex exec` |

完整示例见 [.env.example](.env.example)。

## 安全边界

- 本服务能够远程触发 Codex，应部署在可信网络，并只向授权的飞书应用开放回调。
- 不要提交 `.env`、SSH 私钥、`known_hosts`、SQLite 数据、日志或 `CODEX_HOME`。
- 生产环境建议使用 HTTPS、飞书事件加密和固定 Host Key。
- 工作机的 SSH Key 应通过 `authorized_keys` 限制为唯一执行命令。
- README 和示例配置只使用占位值，不包含真实凭证。

## License

MIT
