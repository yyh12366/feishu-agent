import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import lark_oapi as lark
from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from feishu_dispatcher import FeishuDispatcher, global_thread_pool
from feishu_task_queue import FeishuTaskQueue, HookLimitEnum, TaskStatusEnum


logger = logging.getLogger(__name__)
QUOTED_TEXT_MAX_CHARS = 4000
MESSAGE_TYPES = {"text", "interactive", "post"}


def _parse_id_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


class FeishuMessageReceiver:
    def __init__(self, queue: FeishuTaskQueue, app_id: str, app_secret: str, chat_ids: set[str], bot_open_id: str,
                 wakeup: threading.Event, app_sender_chat_ids: set[str] | None = None):
        self.queue = queue
        self.app_id = app_id
        self.chat_ids = set(chat_ids)
        self.bot_open_id = bot_open_id
        self.wakeup = wakeup
        self.app_sender_chat_ids = set(app_sender_chat_ids or ())
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(
            lark.LogLevel.ERROR
        ).timeout(10).build()

    def receive(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        message = data.event.message
        sender = data.event.sender
        if data.header.app_id != self.app_id or (self.chat_ids and message.chat_id not in self.chat_ids):
            logger.info(f"receive ignored app_id:{data.header.app_id} chat_id:{message.chat_id}")
            return
        if message.message_type not in MESSAGE_TYPES:
            logger.info(
                f"receive ignored message_id:{message.message_id} message_type:{message.message_type} "
                f"sender_type:{sender.sender_type}"
            )
            return
        is_app_sender = sender.sender_type == "app" and message.chat_id in self.app_sender_chat_ids
        if sender.sender_type != "user" and not is_app_sender:
            logger.info(
                f"receive ignored message_id:{message.message_id} sender_type:{sender.sender_type} "
                f"chat_id:{message.chat_id}"
            )
            return
        mentions = [mention for mention in message.mentions or [] if mention.id.open_id == self.bot_open_id]
        if not mentions:
            logger.info(f"receive ignored missing_bot_mention message_id:{message.message_id}")
            return
        if message.message_type == "text":
            prompt = json.loads(message.content)["text"]
        else:
            prompt = self._message_text(message.message_type, message.content)
        for mention in mentions:
            prompt = prompt.replace(mention.key, "")
        prompt = prompt.strip()
        if not prompt:
            logger.info(f"receive ignored empty_prompt message_id:{message.message_id}")
            return
        quoted_text = self._get_quoted_text(getattr(message, "parent_id", None))
        if quoted_text:
            prompt = f"引用消息：\n{quoted_text}\n\n当前消息：\n{prompt}"
        self.queue.enqueue(message.message_id, prompt)
        self.wakeup.set()
        logger.info(f"receive queued message_id:{message.message_id} quoted:{bool(quoted_text)}")

    def _get_quoted_text(self, parent_id: str) -> str:
        if not parent_id:
            return ""
        try:
            request = lark.api.im.v1.GetMessageRequest.builder().message_id(
                parent_id
            ).card_msg_content_type("user_card_content").build()
            response = self.client.im.v1.message.get(request)
            if not response.success():
                logger.warning(
                    f"quoted_message get failed parent_id:{parent_id} code:{response.code} msg:{response.msg}"
                )
                return ""
            data = json.loads(response.raw.content).get("data", {})
            items = data.get("items", [])
            if not items:
                logger.info(f"quoted_message empty parent_id:{parent_id}")
                return ""
            item = items[0]
            text = self._message_text(item.get("msg_type", ""), item.get("body", {}).get("content", ""))
            if len(text) > QUOTED_TEXT_MAX_CHARS:
                text = text[:QUOTED_TEXT_MAX_CHARS] + "\n...（引用内容已截断）"
            logger.info(
                f"quoted_message loaded parent_id:{parent_id} msg_type:{item.get('msg_type')} text_length:{len(text)}"
            )
            return text
        except Exception as exc:
            logger.warning(f"quoted_message get error parent_id:{parent_id} error:{exc!r}")
            return ""

    @staticmethod
    def _message_text(msg_type: str, content: str) -> str:
        if not content:
            return ""
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return str(content).strip()
        if msg_type == "text":
            return str(data.get("text", "")).strip()
        texts = []
        if msg_type == "interactive" and isinstance(data, dict):
            header = data.get("header", {})
            title = header.get("title", {}) if isinstance(header, dict) else {}
            if isinstance(title, dict) and isinstance(title.get("content"), str):
                title_text = title["content"].strip()
                if title_text:
                    texts.append(title_text)

        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("text", "content", "title") and isinstance(item, str):
                        item = item.strip()
                        if item:
                            texts.append(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(data)
        return "\n".join(dict.fromkeys(texts)).strip()


class FeishuLiaodiaoPoller:
    def __init__(self, queue: FeishuTaskQueue, client: lark.Client, chat_id: str, app_id: str,
                 wakeup: threading.Event, poll_interval: int = HookLimitEnum.LIAODIAO_POLL_INTERVAL_SECONDS):
        self.queue = queue
        self.client = client
        self.chat_id = chat_id
        self.app_id = app_id
        self.wakeup = wakeup
        self.poll_interval = poll_interval
        self.stop_event = threading.Event()
        self.seen_message_ids: set[str] = set()
        self.last_create_time = 0
        self.initialized = False

    def run(self) -> None:
        logger.info(f"liaodiao_poller started chat_id:{self.chat_id} app_id:{self.app_id} interval:{self.poll_interval}")
        try:
            while not self.stop_event.is_set():
                try:
                    if self.initialized:
                        self._poll()
                    else:
                        self._initialize()
                except Exception as exc:
                    logger.error(f"liaodiao_poller loop_failed error:{exc!r}")
                self.stop_event.wait(self.poll_interval)
        finally:
            logger.info("liaodiao_poller stopped")

    def stop(self) -> None:
        self.stop_event.set()
        logger.info("liaodiao_poller stop success")

    def _list_messages(self):
        # shortcut: 5 秒轮询最新 20 条；当前联调群消息量低，超过 20 条的突发会漏，后续再改分页
        request = lark.api.im.v1.ListMessageRequest.builder().container_id_type("chat").container_id(
            self.chat_id
        ).sort_type("ByCreateTimeDesc").page_size(20).build()
        response = self.client.im.v1.message.list(request)
        if not response.success():
            logger.warning(f"liaodiao_poller list_failed code:{response.code} msg:{response.msg}")
            return []
        return list((response.data.items if response.data else None) or [])

    def _is_target(self, item) -> bool:
        sender = getattr(item, "sender", None)
        return bool(
            sender
            and getattr(sender, "sender_type", "") == "app"
            and getattr(sender, "id", "") == self.app_id
        )

    @staticmethod
    def _create_time(item) -> int:
        try:
            return int(getattr(item, "create_time", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _initialize(self) -> None:
        items = self._list_messages()
        self.initialized = True
        if not items:
            logger.info("liaodiao_poller initialize empty")
            return
        self.last_create_time = max(self._create_time(item) for item in items)
        target_items = [item for item in items if self._is_target(item)]
        for item in target_items:
            self.seen_message_ids.add(getattr(item, "message_id", ""))
        if target_items:
            latest = target_items[0]
            self.seen_message_ids.discard(getattr(latest, "message_id", ""))
            self._enqueue(latest)
        logger.info(
            f"liaodiao_poller initialized last_create_time:{self.last_create_time} target_count:{len(target_items)}"
        )

    def _poll(self) -> None:
        items = self._list_messages()
        if not items:
            return
        candidates = [
            item for item in items
            if self._is_target(item)
            and self._create_time(item) >= self.last_create_time
            and getattr(item, "message_id", "") not in self.seen_message_ids
        ]
        for item in sorted(candidates, key=self._create_time):
            self._enqueue(item)
        self.last_create_time = max(
            [self.last_create_time] + [self._create_time(item) for item in items]
        )
        logger.debug(f"liaodiao_poller polled count:{len(items)} candidates:{len(candidates)}")

    def _enqueue(self, item) -> None:
        message_id = getattr(item, "message_id", "")
        if not message_id:
            return
        self.seen_message_ids.add(message_id)
        prompt = self._fetch_prompt(message_id)
        if not prompt:
            logger.warning(f"liaodiao_poller empty_prompt message_id:{message_id}")
            return
        inserted = self.queue.enqueue(message_id, prompt)
        if inserted:
            self.wakeup.set()
            logger.info(f"liaodiao_poller enqueued message_id:{message_id} prompt_length:{len(prompt)}")
        else:
            logger.info(f"liaodiao_poller skipped_existing message_id:{message_id}")

    def _fetch_prompt(self, message_id: str) -> str:
        request = lark.api.im.v1.GetMessageRequest.builder().message_id(
            message_id
        ).card_msg_content_type("user_card_content").build()
        response = self.client.im.v1.message.get(request)
        if not response.success():
            logger.warning(f"liaodiao_poller get_failed message_id:{message_id} code:{response.code} msg:{response.msg}")
            return ""
        items = (response.data.items if response.data else None) or []
        if not items:
            return ""
        item = items[0]
        msg_type = getattr(item, "msg_type", "")
        body = getattr(item, "body", None)
        content = getattr(body, "content", "") if body else ""
        if msg_type == "text":
            try:
                return str(json.loads(content).get("text", "")).strip()
            except (TypeError, ValueError):
                return str(content).strip()
        return FeishuMessageReceiver._message_text(msg_type, content)


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    chat_ids = _parse_id_set(os.getenv("FEISHU_CHAT_IDS", os.getenv("FEISHU_CHAT_ID", "")))
    app_sender_chat_ids = _parse_id_set(os.getenv("FEISHU_APP_SENDER_CHAT_IDS", ""))
    required = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BOT_OPEN_ID", "FEISHU_VERIFICATION_TOKEN",
                "FEISHU_CODEX_STATE_FILE", "FEISHU_CODEX_SSH_TARGET", "FEISHU_CODEX_SSH_KEY",
                "FEISHU_CODEX_SSH_KNOWN_HOSTS")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ValueError(f"create_app missing_config keys:{','.join(missing)}")
    state_file = Path(os.getenv("FEISHU_CODEX_STATE_FILE"))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    queue = FeishuTaskQueue(state_file, interrupted_status=TaskStatusEnum.PENDING)
    delivery = FeishuDispatcher(queue, os.getenv("FEISHU_CODEX_SSH_TARGET"), os.getenv("FEISHU_CODEX_SSH_KEY"),
                                os.getenv("FEISHU_CODEX_SSH_KNOWN_HOSTS"))
    receiver = FeishuMessageReceiver(queue, os.getenv("FEISHU_APP_ID"), os.getenv("FEISHU_APP_SECRET"),
                                     chat_ids, os.getenv("FEISHU_BOT_OPEN_ID"), delivery.wakeup, app_sender_chat_ids)
    liaodiao_app_id = os.getenv("FEISHU_LIAODIAO_APP_ID", "").strip()
    liaodiao_chat_id = os.getenv("FEISHU_LIAODIAO_CHAT_ID", os.getenv("FEISHU_CHAT_ID", "")).strip()
    liaodiao_poller = None
    if liaodiao_app_id and liaodiao_chat_id:
        liaodiao_poller = FeishuLiaodiaoPoller(
            queue, receiver.client, liaodiao_chat_id, liaodiao_app_id, delivery.wakeup
        )
        logger.info(f"create_app liaodiao_poller enabled chat_id:{liaodiao_chat_id} app_id:{liaodiao_app_id}")
    else:
        logger.info("create_app liaodiao_poller disabled")
    dispatcher = lark.EventDispatcherHandler.builder(os.getenv("FEISHU_ENCRYPT_KEY", ""),
                                                     os.getenv("FEISHU_VERIFICATION_TOKEN"))
    handler = dispatcher.register_p2_im_message_receive_v1(receiver.receive).build()

    @asynccontextmanager
    async def lifespan(app):
        logger.info(f"lifespan started state_file:{state_file}")
        worker = global_thread_pool.submit(delivery.run)
        poller_worker = global_thread_pool.submit(liaodiao_poller.run) if liaodiao_poller else None
        try:
            yield
        finally:
            delivery.stop()
            if liaodiao_poller:
                liaodiao_poller.stop()
            try:
                await run_in_threadpool(worker.result)
            finally:
                if poller_worker:
                    await run_in_threadpool(poller_worker.result)
                global_thread_pool.shutdown(wait=True)
                queue.db.close()
            logger.info("lifespan stopped")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/feishu/codex/events")
    async def receive_event(request: Request):
        raw = lark.RawRequest()
        raw.uri = request.url.path
        raw.headers = request.headers
        raw.body = await request.body()
        response = await run_in_threadpool(handler.do, raw)
        logger.info(f"receive_event finished status_code:{response.status_code}")
        return Response(content=response.content, status_code=response.status_code, headers=response.headers)

    logger.info(
        f"create_app success chat_ids:{sorted(receiver.chat_ids)} "
        f"app_sender_chat_ids:{sorted(receiver.app_sender_chat_ids)} "
        f"liaodiao_poller:{bool(liaodiao_poller)}"
    )
    return app
