#!/usr/bin/env python3

import json
import logging
import os
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from codex_runner import CodexRunner


logger = logging.getLogger(__name__)
MCP_PROTOCOL_VERSION = "2025-06-18"
SESSION_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
global_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="codex_mcp_")
ASK_CODEX_TOOL = {
    "name": "ask_codex",
    "description": (
        "让本机的 Codex CLI 在指定代码目录里做只读分析，并返回它的最终回答；一次调用可能持续数分钟。"
        "需要另一个模型独立阅读代码、排查问题原因或评审方案时使用。"
        "Codex 运行在只读沙箱里，不能修改文件、提交或部署，不要用它来实施改动。"
        "就同一话题追问时传入上次返回的 session_id。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "交给 Codex 的问题或任务，需自带完整背景；Codex 看不到当前对话。",
            },
            "repo_dir": {
                "type": "string",
                "description": "Codex 的工作目录，绝对路径，必须位于服务配置的允许根目录内。",
            },
            "session_id": {
                "type": "string",
                "description": "续接之前的会话时，原样填上次返回的 session_id；不填则开新会话。",
            },
        },
        "required": ["prompt", "repo_dir"],
    },
}


class CodexMcpServer:
    def __init__(self, runner: CodexRunner, roots: list[Path]):
        self.runner = runner
        self.roots = roots
        self.write_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.running_sessions: set[str] = set()

    def _write(self, message: dict) -> None:
        with self.write_lock:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def _ask_codex(self, arguments: dict) -> str:
        prompt, repo_dir = arguments.get("prompt"), arguments.get("repo_dir")
        session_id = arguments.get("session_id") or ""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 不能为空")
        if not isinstance(repo_dir, str) or not Path(repo_dir).is_absolute():
            raise ValueError(f"repo_dir 必须是绝对路径，收到 {repo_dir!r}")
        path = Path(repo_dir).resolve()
        if not any(path == root or root in path.parents for root in self.roots):
            roots = "、".join(str(root) for root in self.roots)
            raise ValueError(f"repo_dir 必须位于以下目录内：{roots}，收到 {repo_dir!r}")
        if not path.is_dir():
            raise ValueError(f"repo_dir 不存在或不是目录：{repo_dir!r}")
        if not isinstance(session_id, str) or (session_id and not SESSION_ID_PATTERN.match(session_id)):
            raise ValueError("session_id 格式无效，请原样使用上次返回的 session_id，或留空开新会话")
        with self.session_lock:
            if session_id in self.running_sessions:
                raise ValueError(f"会话 {session_id} 正在运行，等它返回后再续接")
            if session_id:
                self.running_sessions.add(session_id)
        try:
            result = self.runner.run(prompt, path, session_id)
        finally:
            with self.session_lock:
                self.running_sessions.discard(session_id)
        return f"{result.answer}\n\nsession_id: {result.session_id}" if result.session_id else result.answer

    def _call_tool(self, request_id, params: dict) -> None:
        is_error = False
        try:
            if params.get("name") != ASK_CODEX_TOOL["name"]:
                raise ValueError(f"未知 tool {params.get('name')!r}，可用：{ASK_CODEX_TOOL['name']}")
            text = self._ask_codex(params.get("arguments") or {})
        except Exception as exc:
            # tool 报错以文本返回给调用方的 LLM，由它自行纠正，不中断服务。
            is_error = True
            text = str(exc)
            logger.error(f"call_tool failed request_id:{request_id} error:{exc!r}")
        self._write({
            "jsonrpc": "2.0", "id": request_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
        })

    def serve(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            method, request_id = message.get("method"), message.get("id")
            if method == "tools/call":
                # 一次调用可能持续数分钟，放进线程池，主循环继续应答其他请求。
                global_thread_pool.submit(self._call_tool, request_id, message.get("params") or {})
                continue
            if method is None or request_id is None:
                continue
            if method == "initialize":
                version = (message.get("params") or {}).get("protocolVersion") or MCP_PROTOCOL_VERSION
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codex-readonly", "version": "1.0.0"},
                }
            elif method == "tools/list":
                result = {"tools": [ASK_CODEX_TOOL]}
            elif method == "ping":
                result = {}
            else:
                self._write({
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
                continue
            self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        # 客户端断开后结束仍在运行的 Codex，避免无人接收结果的进程继续消耗额度。
        self.runner.stop()
        global_thread_pool.shutdown(wait=False)
        logger.info("serve stopped")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env.feishu-codex.local")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    required = ("FEISHU_CODEX_REPO", "FEISHU_CODEX_MCP_ROOTS")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ValueError(f"main missing_config keys:{','.join(missing)}")
    repo_dir = Path(os.getenv("FEISHU_CODEX_REPO")).expanduser().resolve(strict=True)
    roots = [
        Path(item.strip()).expanduser().resolve(strict=True)
        for item in os.getenv("FEISHU_CODEX_MCP_ROOTS").split(",") if item.strip()
    ]
    codex_bin = shutil.which(os.getenv("FEISHU_CODEX_BIN", "codex"))
    if not codex_bin:
        raise FileNotFoundError("main Codex CLI not found")
    # 与飞书 hook 的 CODEX_HOME 分开：hook 按“最新会话文件”取结果，共用会串结果。
    codex_home = repo_dir / "temp" / "feishu-codex" / "codex-mcp-home"
    runner = CodexRunner(codex_bin, codex_home, os.getenv("FEISHU_CODEX_MODEL", "").strip())
    logger.info(f"main ready roots:{[str(root) for root in roots]} codex_home:{codex_home}")
    CodexMcpServer(runner, roots).serve()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("main interrupted")
    except Exception as exc:
        logger.error(f"main failed error:{exc!r}")
        sys.exit(1)
