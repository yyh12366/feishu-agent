#!/usr/bin/env python3

import fcntl
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from codex_runner import CodexRunner, READ_ONLY_NETWORK_ARGS, prepare_codex_home
from feishu_task_queue import FeishuTaskQueue, HookLimitEnum, SshCommandEnum, TaskStatusEnum


logger = logging.getLogger(__name__)
SCREEN_STUFF_CHUNK_SIZE = 256


class FeishuNotifier:
    def __init__(self, app_id: str, app_secret: str, chat_id: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id

    def _tenant_token(self) -> str:
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"FeishuNotifier tenant_token failed code:{data.get('code')} msg:{data.get('msg')}")
        return data["tenant_access_token"]

    def message_chat_id(self, message_id: str) -> str:
        token = self._tenant_token()
        request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages/" + urllib.parse.quote(message_id, safe=""),
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
        if data.get("code") != 0:
            raise RuntimeError(f"FeishuNotifier get message failed code:{data.get('code')} msg:{data.get('msg')}")
        items = (data.get("data") or {}).get("items") or []
        chat_id = items[0].get("chat_id") if items else ""
        if not chat_id:
            raise RuntimeError("FeishuNotifier get message missing chat_id")
        return chat_id

    def with_chat_id(self, chat_id: str) -> "FeishuNotifier":
        return FeishuNotifier(self.app_id, self.app_secret, chat_id)

    def _send(self, msg_type: str, content: str) -> None:
        token = self._tenant_token()
        body = json.dumps({
            "receive_id": self.chat_id,
            "msg_type": msg_type,
            "content": content,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
        if data.get("code") != 0:
            raise RuntimeError(f"FeishuNotifier send failed code:{data.get('code')} msg:{data.get('msg')}")
        logger.info(f"send_message success chat_id:{self.chat_id} msg_type:{msg_type} content_length:{len(content)}")

    def send_text(self, text: str) -> None:
        self._send("text", json.dumps({"text": text}, ensure_ascii=False))

    def send_card(self, title: str, text: str, template: str = "blue") -> None:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [{"tag": "markdown", "content": text}],
        }
        self._send("interactive", json.dumps(card, ensure_ascii=False))


class CodexWorker:
    def __init__(self, queue: FeishuTaskQueue, repo_dir: Path, codex_bin: str, terminal_repo_dir: Path,
                 model: str = "", codex_home: Path | None = None, notifier: FeishuNotifier | None = None,
                 use_screen: bool = True, screen_name: str = "feishu-codex", runner: CodexRunner | None = None):
        self.queue = queue
        self.repo_dir = repo_dir
        self.codex_bin = codex_bin
        self.terminal_repo_dir = terminal_repo_dir
        self.model = model
        self.state_dir = repo_dir / "temp" / "feishu-codex"
        self.codex_home = codex_home or (self.state_dir / "codex-home")
        self.notifier = notifier
        self.use_screen = use_screen
        self.screen_name = screen_name
        self.runner = runner

    def _prepare_codex_home(self) -> None:
        prepare_codex_home(self.codex_home)

    def _build_instructions(self, prompt: str) -> str:
        return (
            "本轮只做只读分析，不要修改任何文件，也不要执行 git add、commit 或部署。"
            "先阅读相关代码，用中文解释问题出现的原因，并给出具体修改方案：涉及文件、改动点和验证方式。"
            "不要自行实施修改。遵循仓库 AGENTS.md 和现有代码风格。\n\n飞书需求：\n" + prompt
        )

    def _run_exec(self, prompt: str) -> str:
        # 沿用“所有飞书需求续接同一条对话”的行为，但按显式会话 id 续接，不依赖 --last。
        session_path = self.state_dir / "exec-session-id"
        session_id = session_path.read_text(encoding="utf-8").strip() if session_path.exists() else ""
        result = self.runner.run(self._build_instructions(prompt), self.terminal_repo_dir, session_id)
        session_path.write_text(result.session_id, encoding="utf-8")
        return result.answer

    def _has_codex_session(self) -> bool:
        sessions = self.codex_home / "sessions"
        return sessions.exists() and any(sessions.rglob("rollout-*.jsonl"))

    def _session_sizes(self) -> dict[str, int]:
        sessions = self.codex_home / "sessions"
        if not sessions.exists():
            return {}
        return {str(path): path.stat().st_size for path in sessions.rglob("rollout-*.jsonl")}

    def _wait_for_result(self, before_sizes: dict[str, int]) -> str:
        deadline = time.monotonic() + HookLimitEnum.RESULT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            sessions = self.codex_home / "sessions"
            files = sorted(
                sessions.rglob("rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True
            ) if sessions.exists() else []
            if files:
                path = files[0]
                start = before_sizes.get(str(path), 0)
                if path.stat().st_size > start:
                    with path.open("r", encoding="utf-8") as output:
                        output.seek(start)
                        for line in output:
                            try:
                                row = json.loads(line)
                            except (TypeError, ValueError):
                                continue
                            payload = row.get("payload") or {}
                            if payload.get("type") == "task_complete":
                                return str(payload.get("last_agent_message") or "").strip()
            time.sleep(1)
        raise TimeoutError("wait_for_result timeout")

    def _screen_lines(self) -> list[str]:
        result = subprocess.run(["screen", "-ls"], capture_output=True, text=True)
        return (result.stdout + result.stderr).splitlines()

    def _screen_exists(self) -> bool:
        return any(f".{self.screen_name}" in line for line in self._screen_lines())

    def _screen_attached(self) -> bool:
        return any(f".{self.screen_name}" in line and "(Attached)" in line for line in self._screen_lines())

    def _stuff_screen(self, text: str) -> None:
        for start in range(0, len(text), SCREEN_STUFF_CHUNK_SIZE):
            subprocess.run(
                ["screen", "-S", self.screen_name, "-p", "0", "-X", "stuff",
                 text[start:start + SCREEN_STUFF_CHUNK_SIZE]],
                check=True,
            )
            time.sleep(0.05)

    def _open_screen_terminal(self, prompt_path: Path, started_path: Path, model_arg: str, resume_arg: str) -> None:
        if not self._screen_exists():
            shell_command = (
                f"cd {shlex.quote(str(self.terminal_repo_dir))} && "
                f"export CODEX_HOME={shlex.quote(str(self.codex_home))} && "
                f"exec {shlex.quote(self.codex_bin)} {resume_arg}{shlex.join(READ_ONLY_NETWORK_ARGS)} "
                f"{model_arg}".rstrip()
            )
            subprocess.run(
                ["screen", "-dmS", self.screen_name, "/bin/zsh", "-lc", shell_command], check=True
            )
            time.sleep(3)
        if not self._screen_attached():
            apple_script = (
                'tell application "Terminal"\n'
                f"do script {json.dumps(f'screen -r {shlex.quote(self.screen_name)}', ensure_ascii=False)}\n"
                "activate\n"
                "end tell"
            )
            subprocess.run(["osascript", "-e", apple_script], check=True)
        prompt_text = prompt_path.read_text(encoding="utf-8").replace("\n", " ")
        self._stuff_screen(prompt_text)
        time.sleep(0.2)
        self._stuff_screen("\r")
        started_path.write_text("", encoding="utf-8")
        logger.info(f"open_screen_terminal success screen:{self.screen_name} prompt:{prompt_path}")

    def open_terminal(self, task_id: int, prompt: str) -> None:
        instructions = self._build_instructions(prompt)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_codex_home()
        prompt_path = self.state_dir / f"task-{task_id}.prompt"
        prompt_path.write_text(instructions, encoding="utf-8")
        script_path = self.state_dir / f"task-{task_id}.command"
        started_path = self.state_dir / f"task-{task_id}.started"
        started_path.unlink(missing_ok=True)
        model_arg = f"-m {shlex.quote(self.model)} " if self.model else ""
        resume_arg = "resume --last " if self._has_codex_session() else ""
        script_path.write_text(
            "#!/bin/zsh\n"
            f"export CODEX_HOME={shlex.quote(str(self.codex_home))}\n"
            f"cd {shlex.quote(str(self.terminal_repo_dir))} || exit 1\n"
            f"prompt=\"$(cat {shlex.quote(str(prompt_path))})\" || exit 1\n"
            f"touch {shlex.quote(str(started_path))} || exit 1\n"
            f"exec {shlex.quote(self.codex_bin)} {resume_arg}{shlex.join(READ_ONLY_NETWORK_ARGS)} "
            f"{model_arg}\"$prompt\"\n",
            encoding="utf-8",
        )
        script_path.chmod(0o700)
        if self.use_screen and shutil.which("screen"):
            self._open_screen_terminal(prompt_path, started_path, model_arg, resume_arg)
            logger.info(
                f"open_terminal success task_id:{task_id} repo_dir:{self.terminal_repo_dir} model:{self.model} "
                f"screen:{self.screen_name}"
            )
            return
        apple_script = (
            'tell application "Terminal"\n'
            f"do script {json.dumps(f'exec {shlex.quote(str(script_path))}', ensure_ascii=False)}\n"
            "activate\n"
            "end tell"
        )
        result = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"open_terminal failed task_id:{task_id} returncode:{result.returncode} error:{result.stderr.strip()!r}"
            )
        deadline = time.monotonic() + HookLimitEnum.TERMINAL_START_TIMEOUT_SECONDS
        while not started_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not started_path.exists():
            raise RuntimeError(f"open_terminal not started task_id:{task_id} repo_dir:{self.terminal_repo_dir}")
        logger.info(
            f"open_terminal success task_id:{task_id} repo_dir:{self.terminal_repo_dir} model:{self.model} "
            f"script:{script_path}"
        )

    def process_task(self, task) -> TaskStatusEnum:
        task_id, message_id, prompt = task
        status = TaskStatusEnum.SUCCEEDED
        try:
            if self.runner:
                answer = self._run_exec(prompt)
            else:
                before_sizes = self._session_sizes()
                self.open_terminal(task_id, prompt)
                answer = self._wait_for_result(before_sizes)
            if not answer:
                raise RuntimeError("Codex returned empty answer")
            if self.notifier:
                self.notifier.send_card("Codex 回复", answer)
        except Exception as exc:
            status = TaskStatusEnum.FAILED
            logger.error(f"process_task failed task_id:{task_id} message_id:{message_id} error:{exc!r}")
            if self.notifier:
                try:
                    self.notifier.send_text(f"Codex 本轮执行失败：{type(exc).__name__}: {exc}")
                except Exception as notify_exc:
                    logger.error(f"process_task notify_failed task_id:{task_id} error:{notify_exc!r}")
        self.queue.finish_task(task_id, status)
        logger.info(f"process_task finished task_id:{task_id} status:{status.value}")
        return status


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.feishu-codex.local")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    required = ("FEISHU_CODEX_REPO",)
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ValueError(f"main missing_config keys:{','.join(missing)}")
    repo_dir = Path(os.getenv("FEISHU_CODEX_REPO")).expanduser().resolve(strict=True)
    terminal_repo_dir = Path(os.getenv("FEISHU_CODEX_TERMINAL_REPO", str(repo_dir))).expanduser().resolve(strict=True)
    model = os.getenv("FEISHU_CODEX_MODEL", "").strip()
    use_screen = os.getenv("FEISHU_CODEX_USE_SCREEN", "1").lower() not in ("0", "false", "no")
    screen_name = os.getenv("FEISHU_CODEX_SCREEN_NAME", "feishu-codex").strip() or "feishu-codex"
    use_exec = os.getenv("FEISHU_CODEX_USE_EXEC", "0").lower() in ("1", "true", "yes")
    codex_bin = shutil.which(os.getenv("FEISHU_CODEX_BIN", "codex"))
    if not codex_bin:
        raise FileNotFoundError("main Codex CLI not found")
    subprocess.run([codex_bin, "login", "status"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    action = os.getenv("SSH_ORIGINAL_COMMAND", SshCommandEnum.RUN.value)
    if action == SshCommandEnum.CHECK.value:
        logger.info(f"main ready repo_dir:{repo_dir} terminal_repo_dir:{terminal_repo_dir} model:{model}")
        print(json.dumps({"status": "ready"}), flush=True)
        return
    if action != SshCommandEnum.RUN.value:
        raise ValueError("main unsupported SSH command")
    body = sys.stdin.buffer.read(HookLimitEnum.MAX_REQUEST_BYTES + 1)
    if len(body) > HookLimitEnum.MAX_REQUEST_BYTES:
        raise ValueError("main request too large")
    payload = json.loads(body)
    message_id, prompt = payload["message_id"], payload["prompt"]
    if not isinstance(message_id, str) or not message_id or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("main message_id and prompt must be nonempty strings")
    state_dir = repo_dir / "temp" / "feishu-codex"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "hook.lock").open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        queue = FeishuTaskQueue(state_dir / "tasks.sqlite3")
        notify_chat_id = os.getenv("FEISHU_CODEX_NOTIFY_CHAT_ID", "").strip()
        notifier = None
        if notify_chat_id:
            notifier = FeishuNotifier(os.getenv("FEISHU_APP_ID", ""), os.getenv("FEISHU_APP_SECRET", ""), notify_chat_id)
            try:
                notifier = notifier.with_chat_id(notifier.message_chat_id(message_id))
            except Exception as exc:
                logger.warning(f"reply chat lookup failed, fallback to configured chat error:{exc!r}")
        runner = CodexRunner(codex_bin, state_dir / "codex-home", model, allow_network=True) if use_exec else None
        worker = CodexWorker(
            queue, repo_dir, codex_bin, terminal_repo_dir, model, notifier=notifier,
            use_screen=use_screen, screen_name=screen_name, runner=runner,
        )
        try:
            queue.enqueue(message_id, prompt)
            task = queue.get_message_task(message_id)
            status = TaskStatusEnum(task[3])
            if status == TaskStatusEnum.PENDING:
                queue.start_task(task[0])
                status = worker.process_task(task[:3])
            logger.info(f"main finished message_id:{message_id} status:{status.value}")
            print(json.dumps({"message_id": message_id, "status": status.value}), flush=True)
        finally:
            queue.db.close()
    logger.info("main stopped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("main interrupted")
    except Exception as exc:
        logger.error(f"main failed error:{exc!r}")
        sys.exit(1)
