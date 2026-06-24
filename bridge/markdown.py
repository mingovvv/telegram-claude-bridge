"""텔레그램 MarkdownV2 best-effort 변환.

Claude 출력은 일반 마크다운이고, 텔레그램 MarkdownV2 는 escape 규칙이 까다롭다.
임의 텍스트를 완벽 변환하는 건 깨지기 쉬우므로, 여기선 **안전 우선** 전략을 쓴다:

  - ```fenced code block``` 은 코드블록으로 보존 (명령/출력 가독성 = "SSH 느낌"의 핵심)
  - 그 외 모든 텍스트는 MarkdownV2 특수문자를 전부 escape → 서식 없이 '있는 그대로' 표시
  - 변환 결과가 파싱 실패하면 호출측에서 plain text 로 fallback

즉 굵게/기울임 같은 인라인 서식은 포기하는 대신 절대 깨지지 않는 출력을 보장한다.
"""

from __future__ import annotations

# MarkdownV2 에서 escape 가 필요한 특수문자 (텔레그램 공식 목록)
_SPECIAL = r"_*[]()~`>#+-=|{}.!"
_SPECIAL_SET = set(_SPECIAL)


def escape_v2(text: str) -> str:
    """코드블록 바깥 일반 텍스트용 MarkdownV2 escape."""
    return "".join("\\" + ch if ch in _SPECIAL_SET else ch for ch in text)


def _escape_code(text: str) -> str:
    """코드블록 내부 escape — 백슬래시와 백틱만 처리하면 된다."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def to_markdown_v2(text: str) -> str:
    """일반 마크다운 텍스트를 텔레그램 MarkdownV2 로 best-effort 변환.

    ```code fence``` 단위로 쪼개, 홀수 인덱스(=펜스 내부)는 코드블록으로,
    짝수 인덱스(=펜스 외부)는 전부 escape 한다.
    """
    parts = text.split("```")
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # 코드블록 내부. 첫 줄이 언어 지정(```python)일 수 있으니 떼어낸다.
            body = part
            if "\n" in body:
                first, rest = body.split("\n", 1)
                # 첫 줄이 짧은 언어 토큰이면 제거
                if first.strip() and " " not in first.strip() and len(first.strip()) <= 20:
                    body = rest
            body = _escape_code(body).strip("\n")
            out.append("```\n" + body + "\n```")
        else:
            out.append(escape_v2(part))
    return "".join(out)
