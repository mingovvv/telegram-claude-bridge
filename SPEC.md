\# 텔레그램 ↔ Claude 브릿지 (개발 명세서)



> \*\*목적\*\*: 텔레그램으로 Claude Code와 대화하며 이 서버(k3-instance)를 원격 조종한다.

> 지금 SSH로 들어와서 하는 작업을, 텔레그램 채팅으로 그대로 옮기는 것이 목표.

>

> \*\*이 문서의 용도\*\*: 로컬 PC의 코딩 AI가 이 명세 하나만 보고 구현할 수 있도록 정리한 개발 스펙.

> 아래 "확정된 설계"는 변경 금지. "구현 가이드"는 참고용이며 최신 SDK 문서로 검증할 것.



\---



\## 0. 한 줄 요약



호스트(k3-instance)에서 도는 \*\*파이썬 데몬\*\* 한 개. 텔레그램을 long-polling으로 바라보다가,

\*\*허용된 사용자(chat\_id)\*\* 가 메시지를 보내면 \*\*Claude Agent SDK\*\*(인프로세스 영속 세션)에 전달하고,

Claude가 호스트에서 직접 명령을 실행한 결과를 텔레그램으로 스트리밍 회신한다.



\---



\## 1. 확정된 설계 (변경 금지)



| 항목 | 결정 | 이유 |

|------|------|------|

| \*\*배포 위치\*\* | 호스트(k3-instance)에 \*\*systemd 서비스\*\*, `User=ubuntu` | k3s 파드 불가 — `\~/.claude` 인증과 호스트 조작 능력(kubectl/ssh키/파일)이 호스

트에 있음. 파드는 격리라 정반대 |

| \*\*인증\*\* | 기존 `\~/.claude` \*\*구독 인증\*\* 그대로 사용 (API 키 불필요) | Agent SDK는 CLI 인증을 상속. `ANTHROPIC\_API\_KEY` 미설정이면 구독으로 동작 |

| \*\*텔레그램 연결\*\* | \*\*long-polling\*\* (`getUpdates`) | 인바운드 포트 0, 공개 URL/TLS 불필요. 단일 유저라 webhook 성능 이점 무의미. 전송 지연(수십 ms)은 LLM 추론 시간(수 초)에 비해 노이즈 |

| \*\*구현 방식\*\* | \*\*Claude Agent SDK (Python)\*\* — 인프로세스 영속 세션 | 매번 `claude -p` 재기동 대신 세션 유지 → 맥락 연속 + 스트리밍 + tool 이벤트 중계 가능 |

| \*\*권한 모델\*\* | \*\*완전 자동 (bypassPermissions)\*\* — 모든 tool 확인 없이 실행 | 사용자 명시적 결정. SSH 세션과 동일한 자유도. ⚠️ 아래 보안 섹션 필독 |

| \*\*세션 모델\*\* | 단일 영속 세션 유지 (cwd=`/home/ubuntu`) | SSH 세션의 연속성 재현 |



\---



\## 2. 보안 모델 (⚠️ 최우선 — 타협 불가)



권한이 \*\*완전 자동(bypassPermissions)\*\* 이므로, 텔레그램으로 들어온 메시지가 곧 서버에서 무확인 실행된다.

이 서버 `ubuntu` 유저는 \*\*k3s 클러스터 전체 제어 + 워커 SSH 키(`/home/ubuntu/secret/`) + API 키(`keys.env`)\*\* 를 가진다.

즉 \*\*봇을 통과하면 인프라 전체 장악\*\*이다. 방어선은 아래 둘뿐이다.



1\. \*\*chat\_id 화이트리스트 (유일한 핵심 방어선)\*\*

&#x20;  - 환경변수/설정에 \*\*허용된 chat\_id 목록\*\*을 두고, 메시지의 `from.id` 가 목록에 없으면 \*\*조용히 무시\*\*(응답조차 하지 않음).

&#x20;  - 화이트리스트 검사는 \*\*모든 핸들러의 맨 첫 줄\*\*에서. 누락 시 = 공개 RCE.

&#x20;  - 그룹 채팅 금지, 1:1 개인 chat만 허용 권장.



2\. \*\*봇 토큰 보호\*\*

&#x20;  - 토큰을 \*\*코드에 하드코딩 금지\*\*. `/home/ubuntu/services/api-keys/keys.env`(권한 600)에 보관하고 환경변수로 주입.

&#x20;  - 토큰 유출돼도 chat\_id 화이트리스트가 막지만, 토큰은 별개로 보호.



3\. \*\*인지 사항\*\*

&#x20;  - 텔레그램 일반 채팅은 E2E 암호화가 아님 → 명령/출력이 텔레그램 서버를 경유한다.

&#x20;  - 로그에 민감 정보(키 파일 내용 등)가 남지 않도록 출력 로깅 주의.



\---



\## 3. 아키텍처



```

\[텔레그램 앱]

&#x20;  ↕ long-polling (outbound HTTPS only, 인바운드 포트 0)

┌──────────────────────────────────────────────────────────┐

│  브릿지 데몬 (Python, k3-instance 호스트, systemd, User=ubuntu) │

│                                                            │

│   ① 텔레그램 어댑터                                          │

│      - getUpdates 폴링 / 메시지 수신                         │

│      - chat\_id 화이트리스트 필터 (★)                         │

│      - 응답 스트리밍(editMessageText) / typing 인디케이터     │

│                                                            │

│   ② Claude 오케스트레이터                                   │

│      - Claude Agent SDK 영속 세션 (bypassPermissions)        │

│      - 사용자 메시지 → 세션에 투입                            │

│      - text delta / tool-use 이벤트를 ①로 중계               │

│                                                            │

│   ③ Claude 엔진 → Bash/kubectl/Edit tool 을                  │

│      이 호스트에서 로컬 서브프로세스로 직접 실행 (ubuntu 권한)  │

└──────────────────────────────────────────────────────────┘

&#x20;       │                              │

&#x20;       ↓ outbound HTTPS               ↓ 로컬 실행 (네트워크 없음)

&#x20; api.telegram.org              호스트 OS / k3s / 파일시스템

&#x20;       │

&#x20;       ↓ outbound HTTPS

&#x20; Anthropic API (모델 추론)

```



\*\*네트워크 연결은 outbound 2개뿐\*\*: 텔레그램, Anthropic API. 서버 조작은 로컬 실행이라 네트워크 미사용.



\---



\## 4. 기술 스택



| 구성 | 선택 | 비고 |

|------|------|------|

| 언어 | Python 3.10+ | Agent SDK 파이썬 바인딩 |

| Claude | `claude-agent-sdk` (pip) | 내부적으로 Claude Code CLI 엔진 구동. \*\*CLI가 호스트에 설치+로그인되어 있어야 함\*\* |

| 텔레그램 | `python-telegram-bot` (v20+, async) | `Application.run\_polling()` 으로 long-polling. async 네이티브 |

| 프로세스 관리 | systemd (`User=ubuntu`) | 자동 재시작, 부팅 시 기동 |

| 설정/시크릿 | 환경변수 (systemd `EnvironmentFile` → `keys.env`) | 토큰/chat\_id 주입 |



> ⚠️ 전제: 호스트에 \*\*Claude Code CLI가 설치되어 있고 `ubuntu` 유저로 로그인(구독)\*\* 되어 있어야 한다.

> 데몬이 같은 `ubuntu` 유저로 돌면 `\~/.claude` 인증을 자동 상속한다. (`claude` 명령이 `ubuntu`로 동작하는지 먼저 확인)



\---



\## 5. 구현 가이드 (참고용 — 최신 SDK 문서로 검증할 것)



> Agent SDK / python-telegram-bot 의 정확한 API 시그니처는 버전마다 바뀐다.

> 아래는 \*\*구조 참고용 의사코드\*\*다. 구현 전 공식/Context7 문서로 메서드명·옵션을 반드시 확인하라.



\### 5.1 Claude Agent SDK — 영속 세션



```python

\# 참고용. ClaudeSDKClient = 멀티턴/스트리밍용 영속 세션 클라이언트.

from claude\_agent\_sdk import ClaudeSDKClient, ClaudeAgentOptions



options = ClaudeAgentOptions(

&#x20;   permission\_mode="bypassPermissions",   # ★ 완전 자동 (모든 tool 무확인 실행)

&#x20;   cwd="/home/ubuntu",                     # SSH 세션과 동일한 작업 디렉토리

&#x20;   system\_prompt="...",                    # 봇 페르소나/운영 컨텍스트 (선택)

&#x20;   include\_partial\_messages=True,          # text delta 스트리밍 수신 (옵션명 확인 필요)

&#x20;   # model="claude-opus-4-8" 등 모델 지정 가능 (미지정 시 기본값)

)



\# 데몬 기동 시 1회 connect, 세션을 살려둠

client = ClaudeSDKClient(options=options)

await client.connect()



\# 사용자 메시지가 올 때마다:

await client.query(user\_text)

async for event in client.receive\_response():

&#x20;   # event 타입 분기:

&#x20;   #  - 텍스트 delta  → 텔레그램 메시지 누적/갱신

&#x20;   #  - tool-use 블록 → "🔧 Running: ..." 상태 중계

&#x20;   #  - 결과/완료      → 최종 메시지 확정

&#x20;   ...

```



핵심:

\- `query()` 가 아니라 `ClaudeSDKClient`(영속 세션) 사용 — 매 메시지마다 새로 만들지 말 것.

\- `permission\_mode="bypassPermissions"` 가 "완전 자동"의 핵심. (값 표기는 SDK 버전 확인)

\- 모델은 강력한 것 권장(서버 조작 추론). 미지정 시 기본 모델. 설정으로 바꿀 수 있게 할 것.



\### 5.2 텔레그램 — long-polling + 스트리밍



```python

\# 참고용.

from telegram.ext import Application, MessageHandler, filters



ALLOWED = {123456789}  # ★ chat\_id 화이트리스트 (env에서 로드)



async def on\_message(update, context):

&#x20;   if update.effective\_user.id not in ALLOWED:   # ★ 맨 첫 줄 필터

&#x20;       return                                     # 조용히 무시



&#x20;   chat\_id = update.effective\_chat.id

&#x20;   placeholder = await context.bot.send\_message(chat\_id, "🤔 ...")



&#x20;   buffer = ""

&#x20;   last\_edit = 0

&#x20;   await client.query(update.message.text)

&#x20;   async for event in client.receive\_response():

&#x20;       # 텍스트 delta 누적

&#x20;       buffer += delta\_text

&#x20;       # throttle: 1초(또는 N글자)마다만 editMessageText (429 회피)

&#x20;       if time.time() - last\_edit > 1.0:

&#x20;           await safe\_edit(placeholder, buffer)   # 스트리밍 중엔 plain text

&#x20;           last\_edit = time.time()

&#x20;       # tool-use 이벤트는 별도 상태 라인으로 중계 가능

&#x20;   await safe\_edit(placeholder, buffer, markdown=True)  # 최종본만 markdown



app = Application.builder().token(BOT\_TOKEN).build()

app.add\_handler(MessageHandler(filters.TEXT \& \~filters.COMMAND, on\_message))

app.run\_polling()   # ← long-polling 루프. endpoint/포트 불필요

```



스트리밍 구현 규칙(텔레그램 제약 대응):

\- \*\*editMessageText는 한 채팅당 \~1초에 1회로 throttle\*\* — 더 빠르면 `429 Too Many Requests(retry\_after)`. 토큰마다 edit 금지.

\- \*\*4096자 제한\*\* — 응답이 넘으면 현재 메시지를 확정하고 새 메시지로 이어서 스트리밍.

\- \*\*Markdown 파싱 깨짐\*\* — 스트리밍 도중엔 plain text로 edit, \*\*완료 시 최종본만\*\* markdown(가능하면 MarkdownV2 escape 처리). 파싱 실패 시 plain text로 fallback.

\- \*\*typing 인디케이터\*\* — `send\_chat\_action(action="typing")` 을 주기적으로 갱신해 "입력 중" 표시 유지(약 5초 지속).

\- \*\*tool-use 중계\*\*(선택, "SSH 느낌"의 핵심) — Claude가 tool 호출 시 `🔧 Running: kubectl get pods ...` 같은 상태 라인을 흘려줌.



\### 5.3 동시성 / 큐

\- Agent SDK 영속 세션은 \*\*동시에 한 요청만\*\* 처리하는 게 안전. 사용자가 답변 중에 또 보내면 \*\*큐잉하거나 "처리 중" 안내\*\* 후 순차 처리.

\- claude 작업이 수십 초 걸릴 수 있으니 핸들러는 전부 async, 블로킹 금지.



\---



\## 6. 설정 / 시크릿



`keys.env`(권한 600)에 추가 후 systemd `EnvironmentFile`로 주입:



```env

TELEGRAM\_CLAUDE\_BRIDGE\_BOT\_TOKEN=<봇 토큰>

TELEGRAM\_CLAUDE\_BRIDGE\_ALLOWED\_IDS=<내 chat\_id>   # 쉼표구분 다중 허용 가능

\# (선택) CLAUDE\_BRIDGE\_MODEL=claude-opus-4-8

```



> \*\*봇은 새 전용 봇 생성 권장\*\* (기존 알림용 @mingov\_bot과 분리 — 역할/토큰 격리).

> 내 chat\_id 확인 방법: @userinfobot 등에 말 걸어 id 확인, 또는 첫 메시지 로그로 확인.



\---



\## 7. systemd 배포 (호스트 1회 셋업)



`/etc/systemd/system/telegram-claude-bridge.service` (예시):



```ini

\[Unit]

Description=Telegram <-> Claude Bridge

After=network-online.target

Wants=network-online.target



\[Service]

Type=simple

User=ubuntu                       # ★ \~/.claude 인증 상속 위해 필수

WorkingDirectory=/home/ubuntu/services/telegram-claude-bridge

EnvironmentFile=/home/ubuntu/services/api-keys/keys.env

ExecStart=/home/ubuntu/services/telegram-claude-bridge/.venv/bin/python -m bridge

Restart=always

RestartSec=5



\[Install]

WantedBy=multi-user.target

```



```bash

sudo systemctl daemon-reload

sudo systemctl enable --now telegram-claude-bridge

journalctl -u telegram-claude-bridge -f   # 로그 확인

```



\---



\## 8. 구현 체크리스트 (로컬 AI용)



\- \[ ] 프로젝트 구조 (`bridge/` 패키지, `pyproject.toml`/`requirements.txt`, `.venv`)

\- \[ ] 설정 로더 (env에서 토큰·chat\_id·모델 로드, 누락 시 명확한 에러)

\- \[ ] \*\*chat\_id 화이트리스트 필터\*\* (모든 핸들러 첫 줄, 미허용 시 무시) ★

\- \[ ] Agent SDK 영속 세션 초기화 (`bypassPermissions`, `cwd=/home/ubuntu`)

\- \[ ] 메시지 핸들러: 사용자 입력 → 세션 투입 → 응답 스트림 수신

\- \[ ] 스트리밍 어댑터: throttled editMessageText(1초) + 4096자 분할 + markdown fallback

\- \[ ] tool-use 이벤트 중계(상태 라인) — 선택이지만 권장

\- \[ ] typing 인디케이터

\- \[ ] 동시 요청 큐잉/순차 처리

\- \[ ] 에러 핸들링(세션 끊김 시 재연결, 텔레그램 429 retry\_after 존중)

\- \[ ] 로깅(journalctl, 민감정보 마스킹)

\- \[ ] systemd 유닛 파일 + 배포 README

\- \[ ] (선택) `/reset` 같은 명령으로 세션 새로 시작, `/status` 헬스체크



\---



\## 9. 구현 전 반드시 검증할 것 (SDK는 버전마다 바뀜)



\- `claude-agent-sdk` 의 정확한 클래스/메서드명(`ClaudeSDKClient`, `ClaudeAgentOptions`, `receive\_response`)과 `permission\_mode` 허용값, 스트리밍 옵션(부분

&#x20;메시지/delta) → \*\*공식 문서 또는 Context7로 확인\*\*.

\- 호스트에서 `ubuntu` 유저로 `claude` CLI가 로그인되어 동작하는지 먼저 확인 (안 되면 SDK도 인증 실패).

\- `python-telegram-bot` v20+ async API 형태.



\---



\## 10. 향후 확장 아이디어 (지금은 범위 밖)



\- 위험 명령 인라인 버튼 확인(permission callback) — 지금은 완전 자동이라 미적용

\- 음성 메시지 → STT, 이미지 첨부 처리

\- 멀티 세션/프로젝트별 컨텍스트 전환

\- 응답을 텔레그램 코드블록/파일첨부로 보기 좋게 포맷

```









