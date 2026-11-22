import logging
import sqlite3
import threading
import time
from enum import Enum, IntEnum
from pathlib import Path


logger = logging.getLogger(__name__)


class TaskStatusEnum(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DELIVERED = "delivered"


class HookLimitEnum(IntEnum):
    TASK_TIMEOUT_SECONDS = 1800
    RETENTION_SECONDS = 30 * 24 * 3600
    CLEANUP_BATCH_SIZE = 100
    DELIVERY_RETRY_SECONDS = 30
    MAX_REQUEST_BYTES = 1024 * 1024
    TERMINAL_START_TIMEOUT_SECONDS = 5
    LIAODIAO_POLL_INTERVAL_SECONDS = 5
    RESULT_TIMEOUT_SECONDS = 900


class SshCommandEnum(str, Enum):
    RUN = "run"
    CHECK = "check"


class FeishuTaskQueue:
    def __init__(self, state_file: Path, interrupted_status: TaskStatusEnum = TaskStatusEnum.FAILED):
        self.db = sqlite3.connect(state_file, check_same_thread=False, timeout=1)
        self.db_lock = threading.Lock()
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS feishu_codex_task (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    prompt TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {column[1] for column in self.db.execute("PRAGMA table_info(feishu_codex_task)")}
            if "result_text" not in columns:
                self.db.execute("ALTER TABLE feishu_codex_task ADD COLUMN result_text TEXT")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_task_status_id ON feishu_codex_task(status, id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_task_status_time ON feishu_codex_task(status, updated_at)")
            # 本地中断不重跑；服务器可重新交付，由本地消息去重避免重复修改。
            interrupted = self.db.execute(
                "UPDATE feishu_codex_task SET status = ?, updated_at = ? WHERE status = ?",
                (interrupted_status.value, int(time.time()), TaskStatusEnum.RUNNING.value),
            ).rowcount
        logger.info(f"FeishuTaskQueue initialized state_file:{state_file} interrupted_count:{interrupted}")

    def enqueue(self, message_id: str, prompt: str) -> None:
        with self.db_lock, self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO feishu_codex_task (message_id, prompt, updated_at) VALUES (?, ?, ?)",
                (message_id, prompt, int(time.time())),
            ).rowcount
        logger.info(f"enqueue success message_id:{message_id} inserted:{inserted} prompt_length:{len(prompt)}")
        return inserted

    def get_message_task(self, message_id: str):
        with self.db_lock:
            task = self.db.execute(
                "SELECT id, message_id, prompt, status FROM feishu_codex_task WHERE message_id = ?", (message_id,)
            ).fetchone()
        logger.info(f"get_message_task success message_id:{message_id} found:{task is not None}")
        return task

    def start_task(self, task_id: int) -> None:
        with self.db_lock, self.db:
            self.db.execute(
                "UPDATE feishu_codex_task SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatusEnum.RUNNING.value, int(time.time()), task_id),
            )
        logger.info(f"start_task success task_id:{task_id}")

    def get_next_task(self, mark_running: bool = True):
        with self.db_lock, self.db:
            task = self.db.execute(
                "SELECT id, message_id, prompt FROM feishu_codex_task WHERE status = ? ORDER BY id LIMIT 1",
                (TaskStatusEnum.PENDING.value,),
            ).fetchone()
            if task and mark_running:
                self.db.execute(
                    "UPDATE feishu_codex_task SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatusEnum.RUNNING.value, int(time.time()), task[0]),
                )
        logger.debug(f"get_next_task success task_id:{task[0] if task else None} mark_running:{mark_running}")
        return task

    def finish_task(self, task_id: int, status: TaskStatusEnum, result_text: str | None = None) -> None:
        with self.db_lock, self.db:
            self.db.execute(
                "UPDATE feishu_codex_task SET status = ?, updated_at = ?, result_text = ? WHERE id = ?",
                (status.value, int(time.time()), result_text, task_id),
            )
            self.db.execute("""
                DELETE FROM feishu_codex_task WHERE id IN (
                    SELECT id FROM feishu_codex_task WHERE status IN (?, ?, ?) AND updated_at < ? LIMIT ?
                )
            """, (TaskStatusEnum.SUCCEEDED.value, TaskStatusEnum.FAILED.value, TaskStatusEnum.DELIVERED.value,
                  int(time.time()) - HookLimitEnum.RETENTION_SECONDS, HookLimitEnum.CLEANUP_BATCH_SIZE))
        logger.info(f"finish_task success task_id:{task_id} status:{status.value}")
