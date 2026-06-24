"""브릿지 애플리케이션: 텔레그램 핸들러 ↔ Claude 세션 배선.

- chat_id 화이트리스트가 모든 핸들러의 첫 줄 (SPEC §2 — 유일 핵심 방어선).
- 들어온 메시지는 asyncio.Queue 에 적재, 단일 워커가 순차 처리(단일 영속 세션 보호).
- 워커가 바쁘면 "큐 추가" 안내, 처리 시 스트리밍 중계.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .claude_session import ClaudeSession, Done, Notice, TextDelta, ToolStart
from .config import Config
from .telegram_stream import TelegramStreamer, typing_loop

log = logging.getLogger(__name__)

# 큐 작업 종류
JOB_MSG = "msg"
JOB_RESET = "reset"

HELP_TEXT = (
    "*Telegram <-> Claude Bridge*\n\n"
    "메시지를 보내면 호스트에서 Claude 가 직접 작업을 수행하고 결과를 스트리밍합니다.\n\n"
    "명령어:\n"
    "/status — 세션/큐 상태\n"
    "/reset — 대화 맥락 초기화 (새 세션)\n"
    "/help — 도움말"
)


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = ClaudeSession(cfg)
        self.queue: asyncio.Queue[tuple[str, int, str | None]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._busy = False
        self._started_at = time.time()

    # --- 보안: 화이트리스트 (모든 핸들러 첫 줄) ---
    def _allowed(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or user.id not in self.cfg.allowed_ids:
            return False
        if chat is None or chat.type != "private":  # 그룹 채팅 금지
            return False
        return True

    # --- 라이프사이클 (PTB post_init/post_shutdown 훅) ---
    async def post_init(self, application: Application) -> None:
        await self.session.connect()
        self._worker = asyncio.create_task(self._run_worker(application))
        log.info("브릿지 시작됨. 허용 chat_id: %s", sorted(self.cfg.allowed_ids))

    async def post_shutdown(self, application: Application) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        await self.session.disconnect()
        log.info("브릿지 종료됨.")

    # --- 텔레그램 핸들러 ---
    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return  # 조용히 무시
        text = (update.message.text or "").strip()
        if not text:
            return
        chat_id = update.effective_chat.id
        await self._enqueue(context, JOB_MSG, chat_id, text)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        await self._reply(context, update.effective_chat.id, HELP_TEXT, markdown=True)

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        await self._enqueue(context, JOB_RESET, update.effective_chat.id, None)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        uptime = int(time.time() - self._started_at)
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        status = (
            "상태\n"
            f"• 세션: {'연결됨' if self.session.connected else '끊김'}\n"
            f"• 처리 중: {'예' if self._busy else '아니오'}\n"
            f"• 대기 큐: {self.queue.qsize()}건\n"
            f"• 모델: {self.cfg.model or '기본값'}\n"
            f"• 작업 디렉토리: {self.cfg.cwd}\n"
            f"• 가동 시간: {h}시간 {m}분 {s}초"
        )
        await self._reply(context, update.effective_chat.id, status)

    # --- 큐 / 워커 ---
    async def _enqueue(self, context, kind: str, chat_id: int, payload: str | None) -> None:
        backlog = self._busy or not self.queue.empty()
        await self.queue.put((kind, chat_id, payload))
        if backlog and kind == JOB_MSG:
            position = self.queue.qsize()
            await self._reply(
                context, chat_id, f"처리 중인 작업이 있어 큐에 넣었어요 (대기 {position}번째)."
            )

    async def _run_worker(self, application: Application) -> None:
        bot = application.bot
        while True:
            kind, chat_id, payload = await self.queue.get()
            self._busy = True
            try:
                if kind == JOB_RESET:
                    await self.session.reset()
                    await self._safe_send(bot, chat_id, "세션을 초기화했어요. 새 대화로 시작합니다.")
                else:
                    await self._process_message(bot, chat_id, payload or "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 워커는 절대 죽지 않는다
                log.exception("작업 처리 실패")
                await self._safe_send(bot, chat_id, f"오류가 발생했어요: {exc}")
            finally:
                self._busy = False
                self.queue.task_done()

    async def _process_message(self, bot, chat_id: int, text: str) -> None:
        streamer = TelegramStreamer(bot, chat_id)
        typing = asyncio.create_task(typing_loop(bot, chat_id))
        try:
            async for ev in self.session.ask(text):
                if isinstance(ev, TextDelta):
                    streamer.add_text(ev.text)
                    await streamer.flush()
                elif isinstance(ev, ToolStart):
                    streamer.add_tool(ev.name, ev.tool_input)
                    await streamer.flush(force=True)
                elif isinstance(ev, Notice):
                    streamer.add_text("\n" + ev.text + "\n")
                    await streamer.flush(force=True)
                elif isinstance(ev, Done):
                    if ev.is_error:
                        streamer.add_text("\n\n(오류로 종료됨)")
        finally:
            typing.cancel()
            try:
                await typing
            except asyncio.CancelledError:
                pass
            await streamer.finalize()

    # --- 헬퍼 ---
    async def _reply(self, context, chat_id: int, text: str, markdown: bool = False) -> None:
        await self._safe_send(context.bot, chat_id, text, markdown=markdown)

    async def _safe_send(self, bot, chat_id: int, text: str, markdown: bool = False) -> None:
        try:
            await bot.send_message(
                chat_id, text, parse_mode=ParseMode.MARKDOWN if markdown else None
            )
        except TelegramError:
            # markdown 파싱 실패 등 → plain 으로 한 번 더 시도
            try:
                await bot.send_message(chat_id, text)
            except TelegramError:
                log.warning("메시지 전송 실패 (chat_id=%s)", chat_id, exc_info=True)


def build_application(cfg: Config) -> Application:
    bridge = Bridge(cfg)
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .post_init(bridge.post_init)
        .post_shutdown(bridge.post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", bridge.cmd_help))
    app.add_handler(CommandHandler("help", bridge.cmd_help))
    app.add_handler(CommandHandler("reset", bridge.cmd_reset))
    app.add_handler(CommandHandler("status", bridge.cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.on_message))
    return app
