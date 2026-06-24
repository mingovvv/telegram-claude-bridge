# telegram-claude-bridge

> **Talk to Claude on Telegram, let it run your server.**
> A self-hosted bridge that turns a Telegram chat into a full Claude Code session.

텔레그램으로 Claude Code와 대화하며 **서버를 원격 조종**하는 단일 파이썬 데몬.
SSH로 들어와서 하던 작업을 그대로 텔레그램 채팅으로 옮긴다.

호스트에서 도는 데몬이 텔레그램을 long-polling으로 바라보다가, **허용된 사용자**가 메시지를 보내면
**Claude Agent SDK**(인프로세스 영속 세션)에 전달한다. Claude는 호스트에서 직접 명령을 실행하고,
그 과정과 결과를 텔레그램으로 스트리밍 회신한다.

```
[Telegram] ⇄ long-polling ⇄ [bridge 데몬 · systemd · root]
                                   └─ Claude Agent SDK (영속 세션, bypassPermissions)
                                        └─ Bash · kubectl · 파일 편집 …  (호스트에서 로컬 실행)
```

네트워크 연결은 **outbound 2개뿐** — 텔레그램, Anthropic API. 인바운드 포트는 열지 않는다(long-polling).
서버 조작은 전부 로컬 실행이라 네트워크를 타지 않는다. 설계 상세는 [SPEC.md](SPEC.md).

---

## ⚠️ 보안 — 타협 불가

권한이 **완전 자동(`bypassPermissions`)** 이고 프로세스가 **root**로 돌기 때문에,
텔레그램 메시지는 곧 서버에서 **root 권한으로 무확인 실행**된다. **봇을 통과하면 곧 서버 장악**이다.
방어선은 둘뿐이다.

| 방어선 | 내용 |
|--------|------|
| **사용자 화이트리스트** | `ALLOWED_IDS`에 없는 사용자는 **조용히 무시**. 모든 핸들러 첫 줄에서 검사하고, 1:1 개인 DM만 허용(그룹 차단). 비우면 기동을 거부한다. |
| **봇 토큰 보호** | 코드에 하드코딩 금지. `keys.env`(권한 600)에 두고 환경변수로 주입. **운영 알림 봇과 분리된 전용 봇**을 쓴다. |

> 텔레그램 일반 채팅은 E2E 암호화가 아니다(텔레그램 서버 경유). 민감 출력 로깅에 주의.

---

## 구조

```
bridge/
  __main__.py         엔트리포인트(python -m bridge): 설정 로드 + run_polling
  config.py           환경변수 로더 + 검증
  claude_session.py   Agent SDK 영속 세션 → 정규화된 이벤트 스트림
  telegram_stream.py  스트리밍 어댑터(throttle · 4096 분할 · MarkdownV2 fallback · typing)
  app.py              배선: 화이트리스트 · 큐 워커 · 핸들러
deploy/
  telegram-claude-bridge.service   systemd 유닛
```

동작 요약
- 단일 영속 세션을 기동 시 1회 `connect`, 살려둔 채 메시지마다 `query`.
- 들어온 메시지는 `asyncio.Queue` → **단일 워커가 순차 처리**(영속 세션은 동시 1요청만 안전).
- 텍스트는 델타로 스트리밍(≈1.2초 throttle), 도구 호출은 `$ Bash: …` 상태 라인으로 중계.
- 4096자를 넘으면 메시지를 확정하고 새 메시지로 이어감. 완료 시 마지막 메시지만 MarkdownV2.

명령어
| 명령 | 동작 |
|------|------|
| `/status` | 세션 · 큐 · 모델 · 가동시간 |
| `/reset` | 대화 맥락 초기화(새 세션) |
| `/help`, `/start` | 도움말 |

---

## 사전 요건

- **Claude Code CLI 설치 + 구독 로그인** (실행 유저 기준). 데몬이 같은 유저로 돌면
  `~/.claude` 인증을 자동 상속한다 — `ANTHROPIC_API_KEY` 불필요.
- **Node 18+** (Agent SDK가 내부적으로 claude CLI를 구동). 구버전 node면 CLI가 죽는다.
- **Python 3.10+** (개발/검증 환경은 3.12).

---

## 빠른 시작 (로컬 실행)

```bash
# 의존성
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 설정 (.env.example 참고)
export TELEGRAM_CLAUDE_BRIDGE_BOT_TOKEN="<봇 토큰>"
export TELEGRAM_CLAUDE_BRIDGE_ALLOWED_IDS="<내 텔레그램 user id>"
# export CLAUDE_BRIDGE_MODEL="claude-opus-4-8"   # (선택)

.venv/bin/python -m bridge
```

> 봇 토큰은 [@BotFather](https://t.me/BotFather)에서 **전용 봇**으로 새로 발급.
> `/setprivacy → Disable` 권장. 내 user id는 [@userinfobot](https://t.me/userinfobot)으로 확인한다.

---

## systemd 배포

> 운영 환경(k3-instance)에서는 **root**로 실행한다. 유닛 파일에 `User=root`,
> `HOME=/root`, nvm node 경로가 포함된 `PATH`가 박혀 있다.

```bash
# 1) 코드 + venv 배치
git clone https://github.com/mingovvv/telegram-claude-bridge.git \
  /home/ubuntu/services/telegram-claude-bridge
cd /home/ubuntu/services/telegram-claude-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2) 시크릿 (권한 600) — keys.env 에 토큰/허용 id 기입
vi /home/ubuntu/services/api-keys/keys.env

# 3) 유닛 등록 & 기동
cp deploy/telegram-claude-bridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-claude-bridge

# 4) 로그
journalctl -u telegram-claude-bridge -f
```

---

## 라이선스

개인 인프라용. 포크/재사용 시 화이트리스트·토큰 격리를 반드시 검토할 것.
