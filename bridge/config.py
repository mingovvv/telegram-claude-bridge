"""환경변수 기반 설정 로더.

systemd `EnvironmentFile`(keys.env)로 주입된 환경변수를 읽어 검증한다.
필수값 누락 시 명확한 에러로 즉시 죽는다 — 조용히 잘못 뜨는 것보다 낫다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# --- 환경변수 키 이름 (SPEC §6) ---
ENV_BOT_TOKEN = "TELEGRAM_CLAUDE_BRIDGE_BOT_TOKEN"
ENV_ALLOWED_IDS = "TELEGRAM_CLAUDE_BRIDGE_ALLOWED_IDS"
ENV_MODEL = "CLAUDE_BRIDGE_MODEL"
ENV_CWD = "CLAUDE_BRIDGE_CWD"
ENV_SYSTEM_PROMPT = "CLAUDE_BRIDGE_SYSTEM_PROMPT"

DEFAULT_CWD = "/home/ubuntu"

DEFAULT_SYSTEM_PROMPT = (
    "너는 k3-instance 호스트에서 텔레그램을 통해 운영자와 대화하는 운영 비서다. "
    "Bash/kubectl/파일 편집 등 호스트의 모든 도구를 직접 실행할 수 있다 "
    "(ubuntu 사용자 권한, 모든 권한 자동 승인). "
    "운영자는 SSH 대신 텔레그램으로 너에게 작업을 지시한다. "
    "답변은 간결하게, 실행한 명령과 핵심 결과 위주로. "
    "긴 출력은 요약하되 중요한 에러/경고는 빠뜨리지 마라. "
    "파괴적이거나 되돌리기 어려운 작업은 실행 전에 한 줄로 무엇을 할지 먼저 알린다."
)


class ConfigError(RuntimeError):
    """설정이 잘못됐거나 누락됐을 때."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_ids: frozenset[int]
    cwd: str = DEFAULT_CWD
    model: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        token = (env.get(ENV_BOT_TOKEN) or "").strip()
        if not token:
            raise ConfigError(
                f"{ENV_BOT_TOKEN} 가 비어 있습니다. keys.env 에 봇 토큰을 설정하세요."
            )

        raw_ids = (env.get(ENV_ALLOWED_IDS) or "").strip()
        if not raw_ids:
            raise ConfigError(
                f"{ENV_ALLOWED_IDS} 가 비어 있습니다. 허용할 chat_id(들)를 "
                "쉼표로 구분해 설정하세요. 미설정 = 공개 RCE 위험."
            )
        try:
            allowed = frozenset(
                int(part.strip()) for part in raw_ids.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ConfigError(
                f"{ENV_ALLOWED_IDS} 파싱 실패: 정수 chat_id 를 쉼표로 구분하세요. ({exc})"
            ) from exc
        if not allowed:
            raise ConfigError(f"{ENV_ALLOWED_IDS} 에 유효한 chat_id 가 없습니다.")

        cwd = (env.get(ENV_CWD) or DEFAULT_CWD).strip()
        model = (env.get(ENV_MODEL) or "").strip() or None
        system_prompt = (env.get(ENV_SYSTEM_PROMPT) or "").strip() or DEFAULT_SYSTEM_PROMPT

        return Config(
            bot_token=token,
            allowed_ids=allowed,
            cwd=cwd,
            model=model,
            system_prompt=system_prompt,
        )

    def redacted_token(self) -> str:
        """로그용 마스킹된 토큰."""
        if len(self.bot_token) <= 8:
            return "***"
        return f"{self.bot_token[:6]}…{self.bot_token[-3:]}"
