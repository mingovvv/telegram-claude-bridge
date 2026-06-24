"""파괴적/되돌리기 어려운 도구 호출 감지.

완전 자동(can_use_tool=allow-all) 상태에서, 여기서 위험으로 판정된 호출만
텔레그램 인라인 버튼으로 확인을 받는다. 나머지(읽기/조회)는 그대로 자동 실행.

보수적으로: 애매하면 위험으로 보지 않는다(과도한 확인 프롬프트 방지).
명확히 파괴적인 패턴만 잡는다.
"""

from __future__ import annotations

import re

# Bash 명령에서 파괴적으로 보는 패턴.
_BASH_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf]", re.I), "파일 삭제(rm)"),
    (re.compile(r"\brm\s+-", re.I), "파일 삭제(rm)"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=|\bshred\b|\bwipefs\b", re.I), "디스크 파괴"),
    (re.compile(r":\(\)\s*\{.*\}\s*;|\bfork\b.*bomb", re.I), "포크밤"),
    (re.compile(r">\s*/dev/sd|>\s*/dev/nvme", re.I), "블록장치 덮어쓰기"),
    (re.compile(r"\bkubectl\s+delete\b", re.I), "kubectl delete"),
    (re.compile(r"\bkubectl\s+(drain|cordon|taint)\b", re.I), "노드 상태 변경"),
    (re.compile(r"\bkubectl\b.*\b(scale|rollout\s+restart|rollout\s+undo)\b", re.I), "워크로드 재배포/스케일"),
    (re.compile(r"\bhelm\s+(uninstall|delete|rollback)\b", re.I), "helm 제거/롤백"),
    (re.compile(r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+rm)\b", re.I), "docker 삭제"),
    (re.compile(r"\b(systemctl|service)\s+(stop|disable|mask)\b", re.I), "서비스 중지/비활성"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "시스템 종료/재부팅"),
    (re.compile(r"\b(git\s+push\b.*(--force|-f)\b|git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f)", re.I), "git 파괴적 작업"),
    (re.compile(r"\b(truncate|drop)\s+(table|database)\b|\bdelete\s+from\b", re.I), "DB 파괴 쿼리"),
    (re.compile(r"\bchown\s+-R\b|\bchmod\s+-R\b", re.I), "재귀 권한 변경"),
    (re.compile(r"\b(iptables|ip6tables)\b.*(-F|-X|DROP|REJECT)", re.I), "방화벽 규칙 변경"),
    (re.compile(r"\bcrontab\s+-r\b", re.I), "크론탭 삭제"),
]

# 위험으로 보는 파일경로(쓰기/편집 시).
_DANGER_PATHS = re.compile(
    r"(?:^|/)(?:\.ssh|secret|secrets|api-keys|keys\.env|\.credentials|"
    r"id_rsa|id_ed25519|\.kube/config)|^/etc/|^/boot/|^/root/\.claude",
    re.I,
)


def assess(tool_name: str, tool_input: dict) -> str | None:
    """위험하면 사람이 읽을 사유 문자열, 아니면 None."""
    name = (tool_name or "").lower()
    ti = tool_input or {}

    if name in ("bash", "bashoutput", "shell") or name.endswith("bash"):
        cmd = str(ti.get("command") or ti.get("cmd") or "")
        for pat, reason in _BASH_PATTERNS:
            if pat.search(cmd):
                return reason
        return None

    # 파일 쓰기/편집 도구 — 민감 경로면 확인
    if name in ("write", "edit", "multiedit", "notebookedit", "create"):
        path = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
        if _DANGER_PATHS.search(path):
            return f"민감 파일 {('쓰기' if name=='write' else '편집')}: {path}"
        return None

    # 그 외 도구(읽기/조회/검색 등)는 위험으로 보지 않음
    return None
