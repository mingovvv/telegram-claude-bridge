"""CommonMark → 텔레그램 HTML 변환.

Claude 출력(표준 마크다운)을 텔레그램 `parse_mode=HTML` 부분집합으로 렌더한다.
지원 태그: <b> <i> <s> <code> <pre> <a> <blockquote>.

텔레그램이 모르는 구조는 가독성 좋은 형태로 바꾼다:
  - 헤더  → 볼드 라인
  - 리스트 → "• " / "1. " 접두 (중첩은 들여쓰기)
  - 표    → 모노스페이스 <pre> 로 컬럼 정렬 (텔레그램은 표 미지원)
  - hr    → 구분선

파싱이 실패해도 절대 예외를 던지지 않는다 — escape 한 평문으로 폴백한다.
"""

from __future__ import annotations

import html
import re
import unicodedata

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark").enable("table")
for _plugin in ("strikethrough",):
    try:
        _md.enable(_plugin)
    except Exception:  # noqa: BLE001
        pass


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _disp_w(s: str) -> int:
    """모노스페이스 표시 폭(칸 수). 한중일(W/F)은 2칸, 결합문자는 0칸."""
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(s: str, width: int) -> str:
    """표시 폭 기준 우측 공백 패딩(한글 정렬 보정)."""
    return s + " " * max(0, width - _disp_w(s))


def to_telegram_html(text: str) -> str:
    """마크다운 → 텔레그램 HTML. 실패 시 escape 한 평문."""
    try:
        body = _render_blocks(_md.parse(text or ""))
        body = "\n".join(line.rstrip() for line in body.split("\n"))  # 줄 끝 패딩 제거
        return re.sub(r"\n{3,}", "\n\n", body).strip("\n")
    except Exception:  # noqa: BLE001 — 렌더는 절대 안 깨진다
        return _esc(text or "")


# --- 인라인 ---
def _inline(children) -> str:
    if not children:
        return ""
    out: list[str] = []
    for tok in children:
        t = tok.type
        if t == "text":
            out.append(_esc(tok.content))
        elif t == "code_inline":
            out.append("<code>" + _esc(tok.content) + "</code>")
        elif t == "strong_open":
            out.append("<b>")
        elif t == "strong_close":
            out.append("</b>")
        elif t == "em_open":
            out.append("<i>")
        elif t == "em_close":
            out.append("</i>")
        elif t in ("s_open",):
            out.append("<s>")
        elif t in ("s_close",):
            out.append("</s>")
        elif t in ("softbreak", "hardbreak"):
            out.append("\n")
        elif t == "link_open":
            href = tok.attrGet("href") or ""
            out.append(f'<a href="{_esc(href)}">')
        elif t == "link_close":
            out.append("</a>")
        elif t == "image":
            out.append(_esc(tok.content))  # 이미지 → alt 텍스트만
        elif tok.content:
            out.append(_esc(tok.content))
    return "".join(out)


def _plain_inline(children) -> str:
    """표 셀용: 서식 제거하고 평문만 (어차피 모노스페이스로 들어감)."""
    out: list[str] = []
    for tok in children or []:
        if tok.type in ("text", "code_inline"):
            out.append(tok.content)
        elif tok.type in ("softbreak", "hardbreak"):
            out.append(" ")
    return "".join(out)


# --- 블록 ---
def _find_close(tokens, start: int, open_t: str, close_t: str) -> int:
    depth = 0
    for k in range(start, len(tokens)):
        if tokens[k].type == open_t:
            depth += 1
        elif tokens[k].type == close_t:
            depth -= 1
            if depth == 0:
                return k
    return len(tokens) - 1


def _render_blocks(tokens, indent: int = 0) -> str:
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        tp = tok.type
        if tp == "heading_open":
            out.append("<b>" + _inline(tokens[i + 1].children) + "</b>\n\n")
            i += 3
        elif tp == "paragraph_open":
            # 들여쓰기는 리스트 마커/중첩 리스트가 담당한다(여기서 pad 적용 X — 이중 들여쓰기 방지)
            out.append(_inline(tokens[i + 1].children) + "\n\n")
            i += 3
        elif tp in ("fence", "code_block"):
            out.append("<pre>" + _esc(tok.content.rstrip("\n")) + "</pre>\n\n")
            i += 1
        elif tp in ("bullet_list_open", "ordered_list_open"):
            j = _find_close(tokens, i, tp, tp.replace("_open", "_close"))
            out.append(_render_list(tokens[i : j + 1], ordered=tp.startswith("ordered"), indent=indent))
            i = j + 1
        elif tp == "blockquote_open":
            j = _find_close(tokens, i, "blockquote_open", "blockquote_close")
            inner = _render_blocks(tokens[i + 1 : j]).strip("\n")
            out.append("<blockquote>" + inner + "</blockquote>\n\n")
            i = j + 1
        elif tp == "table_open":
            j = _find_close(tokens, i, "table_open", "table_close")
            out.append(_render_table(tokens[i : j + 1]))
            i = j + 1
        elif tp == "hr":
            out.append("──────────\n\n")
            i += 1
        elif tp == "inline":
            out.append(_inline(tok.children))
            i += 1
        else:
            i += 1
    return "".join(out)


def _render_list(tokens, ordered: bool, indent: int) -> str:
    out: list[str] = []
    pad = "  " * indent
    idx = 1
    i, n = 1, len(tokens) - 1  # *_open / *_close 제외
    while i < n:
        if tokens[i].type == "list_item_open":
            j = _find_close(tokens, i, "list_item_open", "list_item_close")
            marker = f"{idx}. " if ordered else "• "
            inner = _render_blocks(tokens[i + 1 : j], indent=indent + 1).strip("\n")
            inner = inner.replace("\n", "\n" + pad + "  ")
            out.append(pad + marker + inner + "\n")
            idx += 1
            i = j + 1
        else:
            i += 1
    out.append("\n")
    return "".join(out)


def _render_table(tokens) -> str:
    rows: list[list[str]] = []
    cur: list[str] | None = None
    i, n = 0, len(tokens)
    while i < n:
        tp = tokens[i].type
        if tp == "tr_open":
            cur = []
        elif tp == "tr_close":
            if cur is not None:
                rows.append(cur)
                cur = None
        elif tp in ("th_open", "td_open"):
            txt = ""
            if i + 1 < n and tokens[i + 1].type == "inline":
                txt = _plain_inline(tokens[i + 1].children)
            if cur is not None:
                cur.append(txt.strip())
        i += 1

    rows = [r for r in rows if r]
    if not rows:
        return ""
    ncol = max(len(r) for r in rows)
    for r in rows:
        r += [""] * (ncol - len(r))

    # 한글(2배폭) 같은 wide 문자가 있으면 공백 정렬이 텔레그램 폰트에서 틀어진다.
    # → wide 문자 포함 시 정렬 없는 "레코드 레이아웃", 순수 narrow면 정렬 그리드.
    has_wide = any(_disp_w(c) != len(c) for r in rows for c in r)
    return _table_records(rows) if has_wide else _table_grid(rows, ncol)


def _table_grid(rows, ncol: int) -> str:
    """순수 narrow(ASCII 등) 표 → 모노스페이스 정렬 그리드."""
    widths = [max(_disp_w(r[c]) for r in rows) for c in range(ncol)]
    lines: list[str] = []
    for ri, r in enumerate(rows):
        lines.append(" | ".join(_pad(r[c], widths[c]) for c in range(ncol)))
        if ri == 0:
            lines.append("-+-".join("-" * widths[c] for c in range(ncol)))
    return "<pre>" + _esc("\n".join(lines)) + "</pre>\n\n"


def _table_records(rows) -> str:
    """wide 문자(한글 등) 포함 표 → 정렬 없는 레코드 레이아웃(폰트 무관).

    예) • <b>argocd</b> — 노드 s1 · Restart 0
    헤더행을 라벨로, 첫 칸을 제목으로 쓴다.
    """
    header = rows[0]
    out: list[str] = []
    for r in rows[1:]:
        title = r[0].strip()
        pairs = []
        for c in range(1, len(r)):
            val = r[c].strip()
            if not val:
                continue
            label = header[c].strip() if c < len(header) else ""
            pairs.append(f"{_esc(label)} {_esc(val)}".strip())
        line = f"• <b>{_esc(title)}</b>"
        if pairs:
            line += " — " + " · ".join(pairs)
        out.append(line)
    return "\n".join(out) + "\n\n"
