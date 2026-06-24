"""브릿지 애플리케이션: 텔레그램 핸들러 ↔ Claude 세션 배선.

- chat_id 화이트리스트가 모든 핸들러의 첫 줄 (SPEC §2 — 유일 핵심 방어선).
- 들어온 메시지는 asyncio.Queue 에 적재, 단일 워커가 순차 처리(단일 영속 세션 보호).
- 워커가 바쁘면 "큐 추가" 안내, 처리 시 스트리밍 중계.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import danger
from .claude_session import ClaudeSession, Done, Notice, TextDelta, ToolStart
from .config import Config
from .interactive import CB_MENU, CB_PROMPT, ButtonPrompt
from .telegram_stream import TelegramStreamer, typing_loop, _summarize_tool_input

log = logging.getLogger(__name__)

# 큐 작업 종류
JOB_MSG = "msg"
JOB_RESET = "reset"

HELP_TEXT = (
    "<b>Telegram ↔ Claude Bridge</b>\n\n"
    "메시지를 보내면 호스트에서 Claude 가 직접 작업을 수행하고 결과를 스트리밍합니다.\n\n"
    "명령어:\n"
    "/menu — 빠른 작업 메뉴\n"
    "/status — 세션/큐 상태\n"
    "/reset — 대화 맥락 초기화 (새 세션)\n"
    "/help — 도움말"
)

# /menu 버튼 → 실제로 Claude 에 보낼 프롬프트
MENU_ITEMS: list[tuple[str, str, str]] = [
    # (key, 버튼라벨, Claude에 보낼 프롬프트)
    ("nodes", "🖥 노드 상태", "kubectl get nodes 로 모든 노드 상태를 확인하고 표로 요약해줘."),
    ("pods", "📦 파드 상태", "전체 네임스페이스 파드를 확인하고 Running 아닌 것 위주로 요약해줘."),
    ("disk", "💾 디스크", "각 노드의 디스크 사용량(df -h /)을 확인해서 요약해줘."),
    ("argocd", "🔄 ArgoCD", "ArgoCD 애플리케이션들의 sync/health 상태를 확인해줘."),
    ("logs", "📜 최근 에러", "최근 문제가 있어 보이는 파드의 로그를 살펴보고 이상 징후를 알려줘."),
    ("reset", "🧹 대화 초기화", ""),  # prompt 빈값 = 특수 동작(아래에서 별도 처리)
]


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = ClaudeSession(cfg)
        self.queue: asyncio.Queue[tuple[str, int, str | None]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._busy = False
        self._started_at = time.time()
        self._buttons: ButtonPrompt | None = None
        self._current_chat_id: int | None = None  # 게이트가 어느 채팅에 물어볼지

    # --- 위험 명령 게이트 (can_use_tool 콜백이 호출) ---
    async def _permission_gate(self, tool_name: str, tool_input: dict) -> bool:
        reason = danger.assess(tool_name, tool_input)
        if reason is None:
            return True  # 안전 → 즉시 허용
        chat_id = self._current_chat_id
        if chat_id is None or self._buttons is None:
            return True  # 물어볼 채널이 없으면 기존처럼 자동 허용
        preview = _summarize_tool_input(tool_input, limit=300) or tool_name
        text = (
            f"⚠️ <b>위험 작업 확인</b>\n"
            f"유형: {html.escape(reason)}\n"
            f"<pre>{html.escape(preview)}</pre>\n실행할까요?"
        )
        idx = await self._buttons.ask(
            chat_id, text, ["✅ 실행", "❌ 취소"], timeout=180.0, timeout_index=1
        )
        return idx == 0

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
        # 초기 연결이 실패해도(예: claude 로그인 만료) 데몬을 죽이지 않는다.
        # 죽으면 systemd 크래시 루프가 되어 텔레그램으로 아무 신호도 못 준다.
        # 대신 가동을 유지하고, 첫 메시지 때 재연결을 시도하며 원인을 안내한다.
        self._buttons = ButtonPrompt(application.bot)
        self.session.set_permission_gate(self._permission_gate)
        try:
            await self.session.connect()
        except Exception as exc:  # noqa: BLE001
            log.error("초기 Claude 세션 연결 실패 — 데몬은 계속 가동: %s", exc)
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
        await self._reply(context, update.effective_chat.id, HELP_TEXT, html=True)

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
        session_state = "🟢 연결됨" if self.session.connected else "🔴 끊김"
        busy_state = "🔄 처리 중" if self._busy else "💤 대기"
        status = (
            "📊 <b>브릿지 상태</b>\n\n"
            f"• 세션: {session_state}\n"
            f"• 작업: {busy_state}\n"
            f"• 대기 큐: <b>{self.queue.qsize()}</b>건\n"
            f"• 모델: <code>{html.escape(self.cfg.model or '기본값')}</code>\n"
            f"• 작업 경로: <code>{html.escape(self.cfg.cwd)}</code>\n"
            f"• 가동 시간: {h}시간 {m}분 {s}초"
        )
        await self._reply(context, update.effective_chat.id, status, html=True)

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
                err = f"❗ <b>오류가 발생했어요</b>\n<pre>{html.escape(str(exc))}</pre>"
                await self._safe_send(bot, chat_id, err, html=True)
            finally:
                self._busy = False
                self.queue.task_done()

    async def _process_message(self, bot, chat_id: int, text: str) -> None:
        streamer = TelegramStreamer(bot, chat_id)
        typing = asyncio.create_task(typing_loop(bot, chat_id))
        self._current_chat_id = chat_id  # 위험명령 게이트가 이 채팅에 확인 버튼을 띄운다
        try:
            async for ev in self.session.ask(text):
                if isinstance(ev, TextDelta):
                    streamer.add_text(ev.text)
                    await streamer.flush()
                elif isinstance(ev, ToolStart):
                    streamer.add_tool(ev.name, ev.tool_input)
                    await streamer.flush(force=True)
                elif isinstance(ev, Notice):
                    streamer.add_notice(ev.text)
                    await streamer.flush(force=True)
                elif isinstance(ev, Done):
                    if ev.is_error:
                        streamer.add_notice("⚠️ 오류로 종료됨")
        finally:
            self._current_chat_id = None
            typing.cancel()
            try:
                await typing
            except asyncio.CancelledError:
                pass
            await streamer.finalize()

    # --- /menu + 콜백 ---
    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=f"{CB_MENU}:{key}")]
             for key, label, _ in MENU_ITEMS]
        )
        await context.bot.send_message(
            update.effective_chat.id, "⚡ <b>빠른 작업</b> — 항목을 선택하세요.",
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if update.effective_user is None or update.effective_user.id not in self.cfg.allowed_ids:
            if query:
                await query.answer()  # 미허용자: 조용히 무시
            return
        await query.answer()
        data = query.data or ""
        if data.startswith(f"{CB_PROMPT}:"):
            # 위험명령 확인 버튼 → 대기 중인 게이트를 깨운다
            _, token, idx = data.split(":", 2)
            if self._buttons:
                self._buttons.resolve(token, int(idx))
        elif data.startswith(f"{CB_MENU}:"):
            key = data.split(":", 1)[1]
            chat_id = query.message.chat.id
            if key == "reset":
                # 되돌릴 수 없는 동작 → 확인 버튼 한 번 더
                if self._buttons is None:
                    return
                idx = await self._buttons.ask(
                    chat_id,
                    "🧹 <b>대화 맥락을 초기화</b>할까요?\n지금까지의 대화 내용이 사라지고 새 세션으로 시작합니다.",
                    ["✅ 초기화", "❌ 취소"], timeout=60.0, timeout_index=1,
                )
                if idx == 0:
                    await self._enqueue(context, JOB_RESET, chat_id, None)
                return
            prompt = next((p for k, _, p in MENU_ITEMS if k == key), None)
            if prompt:
                await self._enqueue(context, JOB_MSG, chat_id, prompt)

    # --- 헬퍼 ---
    async def _reply(self, context, chat_id: int, text: str, html: bool = False) -> None:
        await self._safe_send(context.bot, chat_id, text, html=html)

    async def _safe_send(self, bot, chat_id: int, text: str, html: bool = False) -> None:
        try:
            await bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML if html else None
            )
        except TelegramError:
            # HTML 파싱 실패 등 → plain 으로 한 번 더 시도
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
    app.add_handler(CommandHandler("menu", bridge.cmd_menu))
    app.add_handler(CommandHandler("reset", bridge.cmd_reset))
    app.add_handler(CommandHandler("status", bridge.cmd_status))
    app.add_handler(CallbackQueryHandler(bridge.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.on_message))
    return app
