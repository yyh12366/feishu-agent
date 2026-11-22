import json
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from feishu_task_queue import FeishuTaskQueue, HookLimitEnum, SshCommandEnum, TaskStatusEnum


logger = logging.getLogger(__name__)
global_thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu_global_")


class FeishuDispatcher:
    def __init__(self, queue: FeishuTaskQueue, ssh_target: str, ssh_key: str, known_hosts: str):
        self.queue = queue
        self.ssh_target = ssh_target
        self.ssh_key = Path(ssh_key).resolve(strict=True)
        self.known_hosts = Path(known_hosts).resolve(strict=True)
        self.wakeup = threading.Event()
        self.stopped = threading.Event()
        self.process_lock = threading.Lock()
        self.process = None
        self.wakeup.set()

    def send_task(self, task) -> TaskStatusEnum:
        task_id, message_id, prompt = task
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "IdentitiesOnly=yes",
                   "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                   "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-i", str(self.ssh_key),
                   self.ssh_target, SshCommandEnum.RUN.value]
        with self.process_lock:
            if self.stopped.is_set():
                logger.info(f"send_task stopped task_id:{task_id}")
                return TaskStatusEnum.PENDING
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True)
            process = self.process
        logger.info(f"send_task started task_id:{task_id} message_id:{message_id} ssh_target:{self.ssh_target}")
        try:
            output, error = process.communicate(
                input=json.dumps({"message_id": message_id, "prompt": prompt}),
                timeout=HookLimitEnum.TASK_TIMEOUT_SECONDS + 60,
            )
        except subprocess.TimeoutExpired:
            logger.error(f"send_task timeout task_id:{task_id}")
            self.stop_process()
            process.communicate()
            return TaskStatusEnum.PENDING
        finally:
            with self.process_lock:
                self.process = None
        if process.returncode == 255:
            logger.warning(f"send_task connection_failed task_id:{task_id} error:{error.strip()!r}")
            return TaskStatusEnum.PENDING
        if process.returncode != 0:
            logger.error(f"send_task failed task_id:{task_id} returncode:{process.returncode} error:{error.strip()!r}")
            return TaskStatusEnum.FAILED
        result = json.loads(output)
        if result["message_id"] != message_id or result["status"] not in (
            TaskStatusEnum.SUCCEEDED.value, TaskStatusEnum.FAILED.value
        ):
            raise ValueError(f"send_task invalid_result task_id:{task_id}")
        status = TaskStatusEnum(result["status"])
        logger.info(f"send_task finished task_id:{task_id} status:{status.value}")
        return status

    def run(self) -> None:
        # 空闲时只等待新事件；只有未交付任务遇到连接失败时才延迟重试。
        while not self.stopped.is_set():
            self.wakeup.wait()
            self.wakeup.clear()
            while not self.stopped.is_set():
                task = self.queue.get_next_task()
                if task is None:
                    break
                try:
                    status = self.send_task(task)
                except Exception as exc:
                    logger.error(f"run dispatch_failed task_id:{task[0]} error:{exc!r}")
                    status = TaskStatusEnum.FAILED
                self.queue.finish_task(task[0], status)
                if status == TaskStatusEnum.PENDING:
                    self.stopped.wait(HookLimitEnum.DELIVERY_RETRY_SECONDS)
        logger.info("run stopped")

    def stop_process(self) -> None:
        with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.kill()
                except ProcessLookupError as exc:
                    logger.info(f"stop_process already_exited error:{exc!r}")
        logger.info("stop_process finished")

    def stop(self) -> None:
        self.stopped.set()
        self.wakeup.set()
        self.stop_process()
        logger.info("stop success")
