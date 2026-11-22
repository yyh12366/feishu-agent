import json
import logging
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import NamedTuple

from feishu_task_queue import HookLimitEnum


logger = logging.getLogger(__name__)
STDERR_TAIL_CHARS = 500
READ_ONLY_NETWORK_ARGS = (
    "-c", 'default_permissions="feishu-read-only-network"',
    # sshpass 需要终端写入权限，项目文件仍保持只读。
    "-c", 'permissions.feishu-read-only-network={extends=":read-only",filesystem={"/dev/tty"="write"},network={enabled=true}}',
    "-c", 'approval_policy="never"',
)


class CodexResult(NamedTuple):
    answer: str
    session_id: str


def prepare_codex_home(codex_home: Path, *, link_config: bool = True) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    for name in (("auth.json", "config.toml") if link_config else ("auth.json",)):
        source = Path.home() / ".codex" / name
        target = codex_home / name
        if target.is_symlink() and not target.exists():
            target.unlink()
        if not target.exists() and source.exists():
            target.symlink_to(source)
    logger.info(f"prepare_codex_home success home:{codex_home}")


class CodexRunner:
    def __init__(self, codex_bin: str, codex_home: Path, model: str = "", *, allow_network: bool = False,
                 permission_profile: str = "", isolated: bool = False):
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.model = model
        self.allow_network = allow_network
        self.permission_profile = permission_profile
        self.isolated = isolated
        self.process_lock = threading.Lock()
        self.processes: set[subprocess.Popen] = set()

    def run(self, prompt: str, repo_dir: Path, session_id: str = "") -> CodexResult:
        prepare_codex_home(self.codex_home, link_config=not self.isolated)
        command = [self.codex_bin, "exec"]
        if session_id:
            command += ["resume", session_id]
        # 飞书日志查询允许联网，其他调用方保留原只读沙箱。
        if self.permission_profile:
            permission_args = ("-c", f"default_permissions={json.dumps(self.permission_profile)}",
                               "-c", 'approval_policy="never"')
        else:
            permission_args = READ_ONLY_NETWORK_ARGS if self.allow_network else (
                "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"',
            )
        command += ["--json", "--skip-git-repo-check", *permission_args]
        process_env = os.environ.copy()
        if self.isolated:
            # PR content is input data. The reviewer needs neither local tools nor GitHub credentials.
            command += ["--ignore-user-config", "--ignore-rules", "--ephemeral", "-c", 'web_search="disabled"']
            for feature in ("shell_tool", "browser_use", "browser_use_external", "in_app_browser", "hooks",
                            "plugins", "skill_search", "skill_mcp_dependency_install"):
                command += ["--disable", feature]
            allowed_env = {
                "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "OPENAI_API_KEY",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                "https_proxy", "http_proxy", "all_proxy", "no_proxy",
            }
            process_env = {name: value for name, value in process_env.items() if name in allowed_env}
        process_env["CODEX_HOME"] = str(self.codex_home)
        if self.model:
            command += ["-m", self.model]
        command.append("-")
        process = subprocess.Popen(
            command, cwd=repo_dir, env=process_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", start_new_session=True,
        )
        with self.process_lock:
            self.processes.add(process)
        logger.info(
            f"run started pid:{process.pid} repo_dir:{repo_dir} session_id:{session_id} prompt_length:{len(prompt)}"
        )
        try:
            output, error = process.communicate(input=prompt, timeout=HookLimitEnum.RESULT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._kill(process)
            process.communicate()
            raise TimeoutError(
                f"Codex 在 {HookLimitEnum.RESULT_TIMEOUT_SECONDS} 秒内没有完成，已终止本次运行；请缩小问题范围后重试"
            )
        except KeyboardInterrupt:
            self._kill(process)
            process.communicate()
            raise
        finally:
            with self.process_lock:
                self.processes.discard(process)
        answer, failure = "", ""
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            item = event.get("item") or {}
            if event_type == "thread.started":
                session_id = event.get("thread_id") or session_id
            elif event_type == "item.completed" and item.get("type") == "agent_message":
                answer = str(item.get("text") or "").strip()
            elif event_type in ("error", "turn.failed"):
                failure = json.dumps(event, ensure_ascii=False)
        if process.returncode != 0 or not answer:
            detail = failure or error.strip()[-STDERR_TAIL_CHARS:] or "没有返回最终回答"
            raise RuntimeError(f"Codex 运行失败 returncode:{process.returncode} detail:{detail}")
        logger.info(f"run finished pid:{process.pid} session_id:{session_id} answer_length:{len(answer)}")
        return CodexResult(answer, session_id)

    def _kill(self, process: subprocess.Popen) -> None:
        # Codex 会再起 shell 子进程，按进程组结束才不会留下孤儿进程。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError as exc:
            logger.info(f"kill already_exited pid:{process.pid} error:{exc!r}")

    def stop(self) -> None:
        with self.process_lock:
            processes = list(self.processes)
        for process in processes:
            self._kill(process)
        logger.info(f"stop finished killed_count:{len(processes)}")
