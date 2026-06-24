"""인라인 키보드 인터랙션.

메시지에 버튼을 띄우고 사용자가 탭할 때까지 `await` 로 기다렸다 선택을 돌려준다.
콜백은 `CallbackQueryHandler` 가 받아 token으로 대기 중인 Future를 깨운다.

callback_data 형식 (텔레그램 64바이트 제한 안):
  - 버튼 프롬프트: ``bp:<token>:<idx>``
  - 정적 메뉴:     ``menu:<key>``
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

log = logging.getLogger(__name__)

CB_PROMPT = "bp"
CB_MENU = "menu"


class ButtonPrompt:
    """버튼을 띄우고 탭을 기다리는 프롬프트 매니저."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._pending: dict[str, asyncio.Future[int]] = {}
        self._seq = 0

    def _token(self) -> str:
        self._seq += 1
        return str(self._seq)

    async def ask(
        self,
        chat_id: int,
        text: str,
        options: list[str],
        *,
        timeout: float = 120.0,
        timeout_index: int = -1,
    ) -> int:
        """버튼을 띄우고 선택 인덱스를 반환. 시간초과 시 ``timeout_index``."""
        token = self._token()
        fut: asyncio.Future[int] = asyncio.get_event_loop().create_future()
        self._pending[token] = fut
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(opt, callback_data=f"{CB_PROMPT}:{token}:{i}")]
                for i, opt in enumerate(options)
            ]
        )
        msg = await self.bot.send_message(
            chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
        try:
            idx = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            idx = timeout_index
        finally:
            self._pending.pop(token, None)

        chosen = options[idx] if 0 <= idx < len(options) else "⏱ 시간 초과"
        try:  # 버튼 제거 + 선택 결과 표시
            await self.bot.edit_message_text(
                f"{text}\n\n➡️ <b>{chosen}</b>",
                chat_id=chat_id,
                message_id=msg.message_id,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return idx

    def resolve(self, token: str, idx: int) -> None:
        """콜백 핸들러가 호출 — 대기 중인 Future를 깨운다."""
        fut = self._pending.get(token)
        if fut and not fut.done():
            fut.set_result(idx)
