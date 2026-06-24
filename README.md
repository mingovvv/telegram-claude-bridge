# telegram-claude-bridge

텔레그램으로 Claude Code와 대화하며 **k3-instance 호스트를 원격 조종**하는 파이썬 데몬.
SSH 세션에서 하던 작업을 텔레그램 채팅으로 그대로 옮긴다.

호스트에서 도는 데몬 하나가 텔레그램을 long-polling으로 바라보다가, **허용된 chat_id**가
메시지를 보내면 **Claude Agent SDK**(인프로세스 영속 세션)에 전달하고, Claude가 호스트에서
직접 명령을 실행한 결과를 텔레그램으로 스트리밍 회신한다. 자세한 설계는 [SPEC.md](SPEC.md) 참조.

## 보안 (최우선 — 타협 불가)

권한이 **완전 자동(`bypassPermissions`)** 이라, 텔레그램 메시지가 곧 서버에서 무확인 실행된다.
이 호스트 `ubuntu` 유저는 k3s 전체 + SSH키 + API키를 쥐고 있다 → **봇 통과 = 인프라 장악**.

방어선은 둘뿐:

1. **chat_id 화이트리스트** — `TELEGRAM_CLAUDE_BRIDGE_ALLOWED_IDS` 에 없는 사용자는 조용히 무시.
   모든 핸들러 첫 줄에서 검사한다. 비우면 기동 거부. 그룹 채팅도 차단(1:1 개인 chat만).
2. **봇 토큰 보호** — 코드에 하드코딩 금지. `keys.env`(권한 600)에 두고 환경변수 주입.

텔레그램 일반 채팅은 E2E 암호화가 아니다(텔레그램 서버 경유). 민감 출력 로깅에 주의.

## 구조

```
bridge/
  __main__.py        엔트리포인트 (python -m bridge): 설정 로드 + run_polling
  config.py          환경변수 로더 + 검증
  claude_session.py  Agent SDK 영속 세션 래퍼 → 정규화 이벤트 스트림
  telegram_stream.py 스트리밍 어댑터 (throttle / 4096 분할 / MarkdownV2 fallback / typing)
  app.py             배선: 화이트리스트 · 큐 워커 · 핸들러 · 명령어
deploy/
  telegram-claude-bridge.service   systemd 유닛
```

### 동작 요약
- 단일 영속 세션(`cwd=/home/ubuntu`, `bypassPermissions`)을 기동 시 1회 connect.
- 들어온 메시지는 `asyncio.Queue`에 적재 → **단일 워커가 순차 처리**(영속 세션은 동시 1요청만 안전).
- 바쁘면 "큐 추가" 안내. 처리 중엔 `typing…` 인디케이터.
- 텍스트는 델타로 스트리밍(≈1.2초 throttle), 도구 호출은 `$ Bash: …` 상태 라인으로 중계.
- 응답이 4096자를 넘으면 메시지를 확정하고 새 메시지로 이어감. 완료 시 마지막 메시지만 MarkdownV2.

### 명령어
- `/status` — 세션/큐/모델/가동시간
- `/reset` — 대화 맥락 초기화(새 세션). 큐를 거쳐 안전하게 처리됨
- `/help`, `/start` — 도움말

## 사전 요건

- 호스트에 **Claude Code CLI 설치 + `ubuntu` 유저로 로그인(구독 인증)**.
  데몬이 같은 유저로 돌면 `~/.claude` 인증을 자동 상속한다(`ANTHROPIC_API_KEY` 불필요).
  먼저 `sudo -u ubuntu claude --version` / 간단 쿼리로 동작 확인.
- Python 3.10+

## 설치 & 로컬 실행

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip

# 환경변수 설정 (.env.example 참고)
export TELEGRAM_CLAUDE_BRIDGE_BOT_TOKEN="<봇 토큰>"
export TELEGRAM_CLAUDE_BRIDGE_ALLOWED_IDS="<내 chat_id>"
# export CLAUDE_BRIDGE_MODEL="claude-opus-4-8"   # (선택)
# export CLAUDE_BRIDGE_CWD="/home/ubuntu"        # (선택)

.venv/bin/python -m bridge
```

> 봇 토큰은 [@BotFather](https://t.me/BotFather)에서 **전용 봇 신규 생성** 권장.
> 내 chat_id는 [@userinfobot](https://t.me/userinfobot)으로 확인하거나, 봇에 첫 메시지를 보낸 뒤
> 데몬 로그에서 차단된 id를 본다.

## systemd 배포 (호스트 1회 셋업)

```bash
# 1) 코드 배치 + venv
sudo -u ubuntu git clone <repo> /home/ubuntu/services/telegram-claude-bridge
cd /home/ubuntu/services/telegram-claude-bridge
sudo -u ubuntu python3 -m venv .venv
sudo -u ubuntu .venv/bin/pip install -r requirements.txt

# 2) 시크릿 (권한 600)
sudo -u ubuntu vi /home/ubuntu/services/api-keys/keys.env   # .env.example 참고
sudo chmod 600 /home/ubuntu/services/api-keys/keys.env

# 3) 유닛 설치
sudo cp deploy/telegram-claude-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-claude-bridge

# 4) 로그
journalctl -u telegram-claude-bridge -f
```

## 테스트 (네트워크 없이)

핵심 로직(설정 검증, MarkdownV2 변환, 스트리머 분할/throttle, 세션 이벤트 정규화,
화이트리스트)은 가짜 객체로 검증 가능 — 텔레그램/Anthropic 연결 불필요.

## 향후 확장 (범위 밖)

위험 명령 인라인 버튼 확인, 음성/이미지 입력, 멀티 세션, 코드블록/파일첨부 포맷팅.
