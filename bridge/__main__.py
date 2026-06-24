"""엔트리포인트: `python -m bridge`.

설정을 로드하고 long-polling 애플리케이션을 기동한다.
"""

from __future__ import annotations

import logging
import sys

from .app import build_application
from .config import Config, ConfigError


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    # python-telegram-bot 의 HTTP 디버그 로그는 시끄러우니 한 단계 낮춘다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


def main() -> int:
    _setup_logging()
    log = logging.getLogger("bridge")

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2

    log.info(
        "토큰=%s, 허용 ID 수=%d, cwd=%s, model=%s",
        cfg.redacted_token(),
        len(cfg.allowed_ids),
        cfg.cwd,
        cfg.model or "기본값",
    )

    app = build_application(cfg)
    # run_polling 이 이벤트 루프/시그널 처리/그레이스풀 종료를 모두 담당한다.
    # callback_query 를 반드시 포함해야 인라인 버튼 탭이 봇에 도달한다.
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
