"""텔레그램 스트리밍 어댑터.

하나의 응답을 placeholder 메시지에 점진적으로 editMessageText 로 갱신한다.
텔레그램 제약(SPEC §5.2)을 모두 처리:

  - editMessageText 는 채팅당 ~1초 1회로 throttle (429 회피)
  - 4096자 초과 시 현재 메시지를 확정하고 새 메시지로 이어감
  - 스트리밍 중엔 plain text, 완료 시 최종본만 MarkdownV2 (실패 시 plain fallback)
  - RetryAfter / "not modified" BadRequest 정상 처리
"""

from __future__ import annotations

import asyncio
import json
import logging

from telegram import Bot
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError

from .markdown import to_markdown_v2

log = logging.getLogger(__name__)

# 텔레그램 메시지 길이 한도는 4096. 안전 여유를 둔다.
MAX_LEN = 3900
# editMessageText throttle 간격(초).
EDIT_INTERVAL = 1.2
PLACEHOLDER = "..."
EMPTY_RESULT = "완료 (텍스트 출력 없음)"


class TelegramStreamer:
    """한 응답 턴 동안의 텔레그램 메시지 렌더링 상태."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self._full = ""          # 지금까지의 전체 transcript (텍스트 + 도구 마커)
        self._committed = 0      # 앞선 메시지로 이미 확정(seal)된 길이
        self._msg_id: int | None = None
        self._last_edit = 0.0
        self._last_rendered = ""

    # --- 입력 ---
    def add_text(self, text: str) -> None:
        self._full += text

    def add_tool(self, name: str, tool_input: dict) -> None:
        summary = _summarize_tool_input(tool_input)
        line = f"\n\n$ {name}"
        if summary:
            line += f": {summary}"
        self._full += line + "\n"

    # --- 출력 ---
    async def flush(self, force: bool = False) -> None:
        """현재 페이지를 throttle 규칙에 맞춰 텔레그램에 반영."""
        now = asyncio.get_event_loop().time()
        if not force and (now - self._last_edit) < EDIT_INTERVAL:
            return

        await self._ensure_message()
        await self._render_pages(final=False)
        self._last_edit = now

    async def finalize(self) -> None:
        """턴 종료: 남은 내용을 모두 반영하고 마지막 메시지를 MarkdownV2 로 마무리."""
        if not self._full.strip():
            self._full = EMPTY_RESULT
        await self._ensure_message()
        # 1) 남은 plain 내용 확정 (오버플로 분할 포함)
        await self._render_pages(final=True)
        # 2) 마지막 메시지만 MarkdownV2 시도, 실패 시 plain 유지
        page = self._full[self._committed :] or EMPTY_RESULT
        md = to_markdown_v2(page)
        if md == page:
            return  # escape 할 게 없으면 그대로 둔다
        try:
            await self.bot.edit_message_text(
                md,
                chat_id=self.chat_id,
                message_id=self._msg_id,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.debug("MarkdownV2 최종 렌더 실패, plain 유지: %s", exc)

    # --- 내부 ---
    async def _ensure_message(self) -> None:
        if self._msg_id is None:
            msg = await self._send(PLACEHOLDER)
            self._msg_id = msg.message_id

    async def _render_pages(self, final: bool) -> None:
        """committed 이후 내용을 페이지 단위(<=MAX_LEN)로 렌더링.

        한 페이지가 넘치면 현재 메시지를 확정하고 새 메시지를 만들어 이어간다.
        """
        page = self._full[self._committed :]
        while len(page) > MAX_LEN:
            split = page.rfind("\n", 0, MAX_LEN)
            if split < MAX_LEN // 2:  # 마땅한 줄바꿈이 없으면 강제 분할
                split = MAX_LEN
            head = page[:split]
            await self._edit(self._msg_id, head)
            self._committed += len(head)
            self._last_rendered = ""
            # 새 메시지로 이어감
            new_msg = await self._send("...")
            self._msg_id = new_msg.message_id
            page = self._full[self._committed :]

        text = page or PLACEHOLDER
        if text != self._last_rendered:
            await self._edit(self._msg_id, text)
            self._last_rendered = text

    async def _send(self, text: str):
        while True:
            try:
                return await self.bot.send_message(self.chat_id, text)
            except RetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
            except TelegramError:
                log.warning("send_message 실패", exc_info=True)
                raise

    async def _edit(self, msg_id: int | None, text: str, parse_mode=None) -> None:
        if msg_id is None:
            return
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=msg_id, parse_mode=parse_mode
            )
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.5)
            try:
                await self.bot.edit_message_text(
                    text, chat_id=self.chat_id, message_id=msg_id, parse_mode=parse_mode
                )
            except TelegramError:
                log.debug("edit 재시도 실패", exc_info=True)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.debug("edit BadRequest: %s", exc)


async def typing_loop(bot: Bot, chat_id: int, interval: float = 4.0) -> None:
    """처리 중 'typing...' 인디케이터를 주기적으로 갱신 (취소될 때까지)."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except TelegramError:
                pass
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass


def _summarize_tool_input(tool_input: dict, limit: int = 140) -> str:
    """도구 입력에서 사람이 읽을 한 줄 요약을 뽑는다."""
    if not tool_input:
        return ""
    for key in ("command", "file_path", "path", "pattern", "url", "query"):
        if key in tool_input and tool_input[key]:
            value = str(tool_input[key])
            break
    else:
        value = json.dumps(tool_input, ensure_ascii=False)
    value = " ".join(value.split())  # 개행/연속공백 정리
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value
