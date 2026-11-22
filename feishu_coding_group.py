import argparse
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import lark_oapi as lark
from dotenv import dotenv_values

from codex_runner import CodexRunner
from feishu_task_queue import FeishuTaskQueue, TaskStatusEnum


logger = logging.getLogger(__name__)
CONFIG_ROOT = Path.home() / ".config" / "feishu-agent"
PERMISSION_PROFILE = "feishu-coding-group"
INSTRUCTIONS = (
    "你在飞书中协助当前工作区开发。用户明确要求修改代码时实际编辑，"
    "根据需要读取 AGENTS.md，遵循项目规范。仅在当前工作区工作，不提交、不推送、不部署。"
    "用中文回复本轮结论和实际改动，避免输出配置值、凭据或整段私有代码。"
)


class CodingGroup:
    def __init__(self, client, chat_id: str, owner_id: str, state_dir: Path):
        self.client = client
        self.chat_id = chat_id
        self.owner_id = owner_id
        self.cursor = state_dir / "message-cursor"
        self.session = state_dir / "session-id"

    def check_membership(self) -> None:
        request = lark.im.v1.GetChatRequest.builder().chat_id(self.chat_id).user_id_type("open_id").build()
        result = self.client.im.v1.chat.get(request)
        chat = result.data
        if not result.success() or not chat or (
            chat.owner_id != self.owner_id or chat.owner_id_type != "open_id"
            or str(chat.user_count) != "1" or str(chat.bot_count) != "1" or chat.external is not False
        ):
            raise RuntimeError(f"专属群成员核对失败，暂停处理，飞书状态码 {result.code}")

    def prompt(self, message) -> str:
        sender = message.sender
        if (
            message.chat_id != self.chat_id or message.msg_type != "text"
            or message.deleted or message.updated or not message.body or message.mentions
            or not sender or sender.sender_type != "user" or sender.id_type != "open_id"
            or sender.id != self.owner_id
        ):
            return ""
        return json.loads(message.body.content).get("text", "").strip()

    def collect(self, queue: FeishuTaskQueue) -> None:
        start = int(self.cursor.read_text())
        end = int(time.time())
        latest = start
        builder = lark.im.v1.ListMessageRequest.builder().container_id_type("chat").container_id(
            self.chat_id
        ).start_time(str(start)).end_time(str(end)).sort_type("ByCreateTimeAsc").page_size(50)
        while True:
            result = self.client.im.v1.message.list(builder.build())
            if not result.success():
                raise RuntimeError(f"读取专属群消息失败，飞书状态码 {result.code}")
            for message in result.data.items or ():
                prompt = self.prompt(message)
                if prompt:
                    queue.enqueue(message.message_id, prompt)
                latest = max(latest, int(message.create_time) // 1000)
            if not result.data.has_more:
                break
            builder.page_token(result.data.page_token)
        if latest > start:
            temporary = self.cursor.with_suffix(".tmp")
            temporary.write_text(str(latest))
            temporary.replace(self.cursor)

    def reply(self, message_id: str, answer: str) -> None:
        self.check_membership()
        body = lark.im.v1.ReplyMessageRequestBody.builder().msg_type("text").content(
            json.dumps({"text": answer}, ensure_ascii=False)
        ).build()
        request = lark.im.v1.ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        result = self.client.im.v1.message.reply(request)
        if not result.success():
            raise RuntimeError(f"回复专属群失败，飞书状态码 {result.code}")

    def process(self, queue: FeishuTaskQueue, runner: CodexRunner, repo: Path) -> None:
        task = queue.get_next_task()
        if not task:
            return
        task_id, message_id, prompt = task
        session_id = self.session.read_text().strip() if self.session.exists() else ""
        try:
            result = runner.run(f"{INSTRUCTIONS}\n\n飞书消息：\n{prompt}", repo, session_id)
            if result.session_id:
                self.session.write_text(result.session_id)
            status, answer = TaskStatusEnum.SUCCEEDED, result.answer
        except Exception as exc:
            logger.error("Codex task %s failed (%s)", task_id, type(exc).__name__)
            status, answer = TaskStatusEnum.FAILED, "Codex 本轮执行失败。若请求涉及改动，请先检查工作区。"
        queue.finish_task(task_id, status, answer)
        self.reply(message_id, answer)


def prepare_instance(state_dir: Path, repo: Path, codex_bin: Path, app_id: str,
                     chat_id: str, owner_id: str) -> Path:
    if state_dir.is_relative_to(repo) or CONFIG_ROOT.resolve().is_relative_to(repo):
        raise ValueError("实例配置和运行状态必须放在工作区外")
    if Path(__file__).resolve().is_relative_to(repo):
        raise ValueError("工作区必须与飞书 Agent 程序目录分开")
    git_info = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
        text=True,
    ).splitlines()
    git_root, git_dir = map(Path, git_info)
    if git_root.resolve() != repo:
        raise ValueError("FEISHU_CODING_REPO 必须指向 Git 工作区根目录")

    binding_file = state_dir / "binding.json"
    binding = {"repo": str(repo), "app_id": app_id, "chat_id": chat_id, "owner_id": owner_id}
    if binding_file.exists() and json.loads(binding_file.read_text()) != binding:
        raise ValueError("实例已绑定其他工作区或飞书会话，请为新绑定使用独立的配置目录")

    codex_home = state_dir / "codex-home"
    if codex_home.is_symlink():
        raise ValueError("实例必须使用自己的 Codex 状态目录")
    codex_home.mkdir(exist_ok=True)
    template = Path(__file__).resolve().parent / "deploy" / "feishu-coding-group.toml"
    permissions = template.read_text()
    readable = {codex_bin, git_dir.resolve(), repo.parent / "AGENTS.md"}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=codex_home, delete=False) as stream:
        stream.write(permissions)
        for path in sorted(readable):
            stream.write(f'\n{json.dumps(str(path), ensure_ascii=False)} = "read"\n')
    Path(stream.name).replace(codex_home / "config.toml")
    if not binding_file.exists():
        temporary_binding = state_dir / "binding.next.json"
        temporary_binding.write_text(json.dumps(binding, ensure_ascii=False))
        temporary_binding.replace(binding_file)
    return codex_home


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Codex instance for a dedicated Feishu group")
    parser.add_argument("--config", type=Path, default=CONFIG_ROOT / "coding-group" / "group.env")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = args.config.expanduser().resolve(strict=True)
    state_dir = config_path.parent
    config = dotenv_values(config_path)
    app_config = Path(config["FEISHU_APP_CONFIG"]).expanduser().resolve(strict=True)
    repo = Path(config["FEISHU_CODING_REPO"]).expanduser().resolve(strict=True)
    if app_config.is_relative_to(repo):
        raise ValueError("飞书应用凭据必须放在工作区外")
    app = dotenv_values(app_config)
    chat_id = config["FEISHU_CODING_CHAT_ID"]
    if not chat_id.startswith("oc_") or not chat_id[3:].isalnum():
        raise ValueError("FEISHU_CODING_CHAT_ID 必须为飞书群聊 ID")
    executable = shutil.which(os.path.expanduser(app.get("FEISHU_CODEX_BIN") or "codex"))
    if not executable:
        raise ValueError("找不到 Codex CLI，请确认 PATH 或设置 FEISHU_CODEX_BIN")
    codex_bin = Path(executable).resolve(strict=True)
    client = lark.Client.builder().app_id(app["FEISHU_APP_ID"]).app_secret(app["FEISHU_APP_SECRET"]).timeout(10).build()
    group = CodingGroup(client, chat_id, config["FEISHU_CODING_OWNER_ID"], state_dir)
    locks = CONFIG_ROOT / "group-locks"
    locks.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    with (state_dir / "worker.lock").open("a") as lock, (locks / f"{chat_id}.lock").open("a") as chat_lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(chat_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        codex_home = prepare_instance(state_dir, repo, codex_bin, app["FEISHU_APP_ID"], chat_id, group.owner_id)
        runner = CodexRunner(str(codex_bin), codex_home, app.get("FEISHU_CODEX_MODEL") or "",
                             permission_profile=PERMISSION_PROFILE)
        queue = FeishuTaskQueue(state_dir / "tasks.sqlite3")
        if not group.cursor.exists():
            group.cursor.write_text(str(int(time.time())))
        logger.info("Feishu coding group worker ready")
        try:
            while True:
                try:
                    group.check_membership()
                    group.collect(queue)
                    group.process(queue, runner, repo)
                except Exception as exc:
                    logger.warning("Coding group loop failed: %s", type(exc).__name__)
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        finally:
            queue.db.close()


if __name__ == "__main__":
    main()
