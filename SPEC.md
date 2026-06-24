# 텔레그램 ↔ Claude 브릿지 — 설계 명세

> 텔레그램으로 Claude Code와 대화하며 서버(k3-instance)를 원격 조종한다.
> SSH 세션을 텔레그램 채팅으로 옮기는 것이 목표. 이 문서는 구현·운영의 단일 기준이다.

---

## 1. 한 줄 요약

호스트에서 도는 **파이썬 데몬** 하나. 텔레그램을 long-polling으로 바라보다가, **허용된 사용자(개인 DM)**
가 메시지를 보내면 **Claude Agent SDK**(인프로세스 영속 세션)에 전달하고, Claude가 호스트에서 직접
명령을 실행한 결과를 텔레그램으로 스트리밍 회신한다.

---

## 2. 확정된 설계

| 항목 | 결정 | 이유 |
|------|------|------|
| 배포 위치 | 호스트(k3-instance)에 **systemd 서비스**, `User=root` | 운영자가 평소 `sudo su`로 root에서 작업 → root가 주 사용자. root만 node v20+claude 동작(ubuntu는 node v10이라 CLI가 죽음). `~/.claude` 인증·호스트 조작 능력이 root에 있음 |
| 인증 | `/root/.claude` **구독 인증** 그대로 (API 키 불필요) | Agent SDK는 CLI 인증을 상속. `ANTHROPIC_API_KEY` 미설정이면 구독으로 동작 |
| 텔레그램 연결 | **long-polling** (`getUpdates`) | 인바운드 포트 0, 공개 URL/TLS 불필요. 단일 유저라 webhook 이점 무의미 |
| 대화 채널 | **1:1 개인 DM** (전용 봇) | root RCE 통로라 격리가 최우선. 그룹/토픽보다 노출면이 작음 |
| 구현 방식 | **Claude Agent SDK (Python)** — 인프로세스 영속 세션 | 맥락 연속 + 스트리밍 + 도구 이벤트 중계 |
| 권한 모델 | **완전 자동** (자동승인 콜백) | root에선 `--dangerously-skip-permissions`(=`bypassPermissions`)가 거부되므로, `can_use_tool` 콜백으로 모든 도구를 자동 allow. ⚠️ §3 필독 |

---

## 3. 보안 모델 (최우선)

권한이 완전 자동이고 프로세스가 **root**로 실행되므로, 텔레그램 메시지는 곧 서버에서 **root 권한으로
무확인 실행**된다. root는 k3s 전체 + 워커 SSH 키 + API 키 + 호스트 전부를 쥔다.
즉 **봇을 통과하면 = 서버 root 장악**. 방어선은 둘뿐이다.

1. **사용자 화이트리스트 (유일한 핵심 방어선)**
   - `ALLOWED_IDS`에 없는 `from.id`는 **조용히 무시**(응답조차 안 함).
   - 검사는 모든 핸들러의 **첫 줄**에서. 그룹 채팅은 차단하고 1:1 개인 DM만 허용.
   - 비어 있으면 기동을 거부한다(잘못 떠서 공개 RCE가 되는 것을 방지).

2. **봇 토큰 보호**
   - 코드 하드코딩 금지. `keys.env`(권한 600)에 두고 환경변수로 주입.
   - **운영 알림 봇과 분리된 전용 봇**을 쓴다(토큰·getUpdates·privacy 독립).

> 텔레그램 일반 채팅은 E2E 암호화가 아니다(텔레그램 서버 경유). 민감 출력 로깅에 주의.
>
> 참고: 대화형 Claude Code 세션에 있는 안전 분류기는 **독립 실행되는 Agent SDK에는 없다.**
> 즉 이 봇은 그 가드 없이 그대로 실행된다 → 화이트리스트가 사실상 유일한 안전장치다.

---

## 4. 아키텍처

```
[텔레그램 앱]
   ⇕ long-polling (outbound HTTPS only, 인바운드 포트 0)
┌────────────────────────────────────────────────────────┐
│  브릿지 데몬 (Python · k3-instance · systemd · User=root)   │
│    ① 텔레그램 어댑터 — getUpdates / 화이트리스트 / 스트리밍   │
│    ② Claude 오케스트레이터 — Agent SDK 영속 세션(bypass)     │
│    ③ Claude 엔진 → Bash/kubectl/Edit 를 호스트에서 로컬 실행  │
└────────────────────────────────────────────────────────┘
        │                              │
        ↓ outbound HTTPS               ↓ 로컬 실행 (네트워크 없음)
  api.telegram.org              호스트 OS / k3s / 파일시스템
        │
        ↓ outbound HTTPS
  Anthropic API (모델 추론)
```

네트워크 연결은 outbound 2개(텔레그램, Anthropic API)뿐. 서버 조작은 로컬 실행이라 네트워크 미사용.

---

## 5. 기술 스택

| 구성 | 선택 | 비고 |
|------|------|------|
| 언어 | Python 3.12 (`uv` venv) | 시스템 python은 3.8이라 불가 → `uv`로 standalone 3.12 설치 |
| Claude | `claude-agent-sdk==0.2.108` | 내부적으로 claude CLI(v2.1.187, node v20) 구동. root 로그인됨 |
| 텔레그램 | `python-telegram-bot==22.x` | `Application.run_polling()` long-polling, async 네이티브 |
| 프로세스 | systemd (`User=root`) | 자동 재시작, 부팅 시 기동 |
| 시크릿 | 환경변수 (`EnvironmentFile` → `keys.env`) | 토큰/허용 id 주입 |

> Agent SDK는 `claude` CLI를 PATH에서 찾는다. systemd `PATH`에 **nvm node 경로**
> (`/root/.nvm/versions/node/v20.20.1/bin`)가 반드시 포함돼야 한다. 빠지면 시스템 node v10을 잡아 CLI가 죽는다.

---

## 6. Agent SDK 사용 (claude-agent-sdk 0.2.108, 실측 검증)

```python
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, StreamEvent,
    TextBlock, ToolUseBlock,
)

async def _allow_all(tool_name, tool_input, context):
    return PermissionResultAllow()          # 모든 도구 자동 승인

options = ClaudeAgentOptions(
    can_use_tool=_allow_all,                # root에선 bypassPermissions 불가 → 콜백으로 완전 자동
    cwd="/home/ubuntu",
    system_prompt="...",
    include_partial_messages=True,          # StreamEvent로 텍스트 델타 스트리밍
    model="claude-opus-4-8",                # (선택) 미지정 시 기본 모델
)

client = ClaudeSDKClient(options=options)
await client.connect()                      # 기동 시 1회, 세션을 살려둠
await client.query(user_text)               # 메시지마다 세션에 투입
async for msg in client.receive_response():
    # StreamEvent      → 텍스트 델타 (스트리밍 갱신)
    # AssistantMessage → ToolUseBlock(.name/.input) / TextBlock(.text)
    # ResultMessage    → 턴 완료 (.is_error / .result / .total_cost_usd)
    ...
```

검증된 사실
- ⚠️ **root에서는 `bypassPermissions`가 거부된다**(`--dangerously-skip-permissions cannot be used with root/sudo`).
  → root로 돌릴 땐 `permission_mode` 대신 `can_use_tool` 콜백으로 자동 allow 해야 한다(완전 자동 동일 효과).
- `permission_mode` 허용값: `default · acceptEdits · plan · bypassPermissions · dontAsk · auto` (비-root 한정 bypass).
- `ClaudeSDKClient` 메서드: `connect · disconnect · query · receive_response · interrupt · set_model …`.
  - `interrupt()`로 진행 중 작업 중단을 구현할 수 있다.
- 멀티턴은 `ClaudeSDKClient`를 살려두면 자동 유지(수동 `--resume` 불필요).
- 위험명령 게이트가 필요해지면 `can_use_tool` 콜백 / `hooks(PreToolUse)`로 확장 가능(현재 미사용).

### 텔레그램 스트리밍 규칙
- `editMessageText`는 채팅당 ~1초 1회로 throttle(429 회피). 토큰마다 edit 금지.
- 4096자 초과 시 현재 메시지를 확정하고 새 메시지로 이어감.
- 스트리밍 중엔 plain text, 완료 시 마지막 메시지만 MarkdownV2(실패 시 plain fallback).
- `typing` 인디케이터를 주기적으로 갱신.
- 도구 호출은 `$ Bash: …` 상태 라인으로 중계("SSH 느낌"의 핵심).

### 동시성
- 영속 세션은 동시 1요청만 안전 → 메시지를 큐에 적재하고 단일 워커가 순차 처리.

---

## 7. 설정 (keys.env, 권한 600)

```env
TELEGRAM_CLAUDE_BRIDGE_BOT_TOKEN=<전용 봇 토큰>
TELEGRAM_CLAUDE_BRIDGE_ALLOWED_IDS=<내 텔레그램 user id>   # 쉼표로 다중 가능
# CLAUDE_BRIDGE_MODEL=claude-opus-4-8     (선택)
# CLAUDE_BRIDGE_CWD=/home/ubuntu          (선택)
# CLAUDE_BRIDGE_SYSTEM_PROMPT=...         (선택)
```

봇 토큰: @BotFather에서 전용 봇 신규 생성(`/setprivacy → Disable`).
내 user id: @userinfobot으로 확인(개인 DM에서는 chat_id와 동일).

---

## 8. systemd 배포

`/etc/systemd/system/telegram-claude-bridge.service`:

```ini
[Service]
Type=simple
User=root
WorkingDirectory=/home/ubuntu/services/telegram-claude-bridge
EnvironmentFile=/home/ubuntu/services/api-keys/keys.env
Environment=HOME=/root
Environment=PATH=/root/.nvm/versions/node/v20.20.1/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/ubuntu/services/telegram-claude-bridge/.venv/bin/python -m bridge
Restart=always
RestartSec=5
LimitCORE=0
```

```bash
systemctl daemon-reload
systemctl enable --now telegram-claude-bridge
journalctl -u telegram-claude-bridge -f
```

---

## 9. 호스트 환경 (준비·검증 완료)

- Claude Code CLI **v2.1.187** + root 구독 로그인(`/root/.claude/.credentials.json`).
- node **v20.20.1** (`/root/.nvm/versions/node/v20.20.1/bin`) — claude CLI 정상 동작.
- `uv` 설치, 프로젝트 venv = **Python 3.12** + `claude-agent-sdk 0.2.108` · `python-telegram-bot 22.x` import 검증.
- end-to-end 체인(node→CLI→구독인증→모델 응답) 스모크 통과.

남은 운영 작업: ① 전용 봇 생성+토큰 ② 내 user id 확인 → `keys.env` 기입 ③ systemd 등록 & 기동.

---

## 10. 향후 확장 (선택)

위험 명령 게이트(`can_use_tool`/hooks + 인라인 버튼), 음성·이미지 입력,
멀티 세션/프로젝트 전환, 응답 포맷 개선(코드블록/파일 첨부).
