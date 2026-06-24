"""Claude Agent SDK 영속 세션 래퍼.

데몬 기동 시 한 번 connect 해서 세션을 살려두고, 메시지마다 query → 스트림 수신.
SDK 의 다양한 메시지/이벤트를 텔레그램 어댑터가 다루기 쉬운 단순 이벤트로 정규화한다.

동시성: 단일 워커가 ask() 를 순차 소비하므로 in-flight 쿼리는 항상 1개다 (SPEC §5.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    PermissionResultAllow,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from .config import Config

log = logging.getLogger(__name__)


# --- 정규화된 브릿지 이벤트 ---
@dataclass
class TextDelta:
    """스트리밍 텍스트 조각."""
    text: str


@dataclass
class ToolStart:
    """Claude 가 도구를 호출하기 시작함 (상태 라인 중계용)."""
    name: str
    tool_input: dict = field(default_factory=dict)


@dataclass
class Notice:
    """운영 알림(재연결 등) — 본문과 구분되는 시스템 메시지."""
    text: str


@dataclass
class Done:
    """턴 완료."""
    is_error: bool = False
    result: str | None = None
    cost_usd: float | None = None


BridgeEvent = TextDelta | ToolStart | Notice | Done


class ClaudeSession:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client: ClaudeSDKClient | None = None

    # --- 라이프사이클 ---
    @staticmethod
    async def _allow_all_tools(tool_name, tool_input, context):
        """모든 도구 호출을 무조건 승인 = 완전 자동.

        root 에서는 --dangerously-skip-permissions(=bypassPermissions)가 거부되므로,
        기본 권한 모드 + 이 콜백으로 'ask' 를 전부 allow 처리해 동일한 완전 자동 효과를 낸다.
        """
        return PermissionResultAllow()

    def _build_options(self) -> ClaudeAgentOptions:
        kwargs: dict = dict(
            # root 에서 bypassPermissions 사용 불가 → 자동승인 콜백으로 완전 자동 구현
            can_use_tool=self._allow_all_tools,
            cwd=self.cfg.cwd,
            system_prompt=self.cfg.system_prompt,
            include_partial_messages=True,  # 텍스트 델타 스트리밍 수신
        )
        if self.cfg.model:
            kwargs["model"] = self.cfg.model
        return ClaudeAgentOptions(**kwargs)

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        if self._client is not None:
            return
        log.info("Claude 세션 연결 중 (cwd=%s, model=%s)", self.cfg.cwd, self.cfg.model or "default")
        client = ClaudeSDKClient(options=self._build_options())
        await client.connect()
        self._client = client
        log.info("Claude 세션 연결 완료")

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001 — 종료 경로, 어떤 예외든 무시
            log.debug("disconnect 중 예외(무시)", exc_info=True)
        finally:
            self._client = None

    async def reset(self) -> None:
        """현재 세션을 끊고 새 세션으로 재연결 (맥락 초기화)."""
        await self.disconnect()
        await self.connect()

    # --- 메시지 처리 ---
    async def ask(self, prompt: str) -> AsyncIterator[BridgeEvent]:
        """사용자 메시지를 세션에 투입하고 정규화된 이벤트를 스트리밍한다.

        세션이 끊겨 있으면 한 번 재연결을 시도하고, 그래도 실패하면 Notice 를 내보낸다.
        """
        if self._client is None:
            await self.connect()

        try:
            async for ev in self._run(prompt):
                yield ev
        except (ClaudeSDKError, ConnectionError, BrokenPipeError) as exc:
            log.warning("세션 오류, 재연결 시도: %s", exc)
            try:
                await self.reset()
                yield Notice("세션이 끊겨 재연결했어요. 메시지를 다시 보내 주세요.")
            except Exception as exc2:  # noqa: BLE001
                log.error("재연결 실패: %s", exc2)
                yield Notice(f"세션 재연결 실패: {exc2}")

    async def _run(self, prompt: str) -> AsyncIterator[BridgeEvent]:
        assert self._client is not None
        await self._client.query(prompt)

        streamed_chars = 0          # StreamEvent 로 흘려보낸 텍스트 길이
        assistant_text_buf: list[str] = []  # 델타가 안 올 때를 대비한 fallback 버퍼

        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                delta = self._text_delta(msg)
                if delta:
                    streamed_chars += len(delta)
                    yield TextDelta(delta)

            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        yield ToolStart(block.name, block.input or {})
                    elif isinstance(block, TextBlock) and block.text:
                        assistant_text_buf.append(block.text)
                    # TextBlock 텍스트는 보통 StreamEvent 로 이미 흘렀으므로 여기선 버퍼만.

            elif isinstance(msg, ResultMessage):
                # 델타가 한 번도 안 왔다면(스트리밍 비활성 등) 누적 텍스트로 대체 송출.
                if streamed_chars == 0 and assistant_text_buf:
                    yield TextDelta("".join(assistant_text_buf))
                yield Done(
                    is_error=bool(msg.is_error),
                    result=msg.result,
                    cost_usd=msg.total_cost_usd,
                )

    @staticmethod
    def _text_delta(msg: StreamEvent) -> str | None:
        """StreamEvent 에서 텍스트 델타만 추출 (thinking/tool-json 델타는 제외)."""
        ev = msg.event or {}
        if ev.get("type") != "content_block_delta":
            return None
        delta = ev.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text") or None
        return None
