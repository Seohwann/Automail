# Automail — 멀티 에이전트 협찬/제안 메일 자동화 시스템

구글 시트에 업체 목록(업체명·힌트)만 넣으면, **이메일 자동 탐색 → 업체별 맞춤 제안 메일 작성 → (사람 승인) → 일괄 발송 → 답장 분류·후속 대응**까지 이어지는 협찬/제안 메일 자동화 도구입니다. 성균관대학교 총학생회 S'PEAK 대외협력국의 협찬·부스 입점 제안 업무를 염두에 두고 만들었습니다.

- **LLM**: Google Gemini (`langchain-google-genai`)
- **웹 검색**: Tavily (`langchain-tavily`)
- **오케스트레이션**: LangGraph — supervisor 멀티 에이전트 그래프 + 답장 그래프
- **연동**: Google Sheets(업체/초안 저장), Gmail(발송·답장 조회·라벨)
- **UI**: Flask 웹 대시보드 (`automail.py`) — `자동 실행` / `후속 대응` 2개 탭

---

## 1. 아키텍처 — Agent 형식 vs Workflow 형식

구분 기준: **START 이후의 경로(분기)를 누가 정하는가** — LLM 이 정하면 Agent, 코드/사람이 정하면 Workflow.
불확실성이 큰 부분만 Agent 에 맡기고, 예측 가능성·안전이 중요한 부분은 Workflow 로 고정했습니다.

| 구성 요소 | 형식 | 키워드 |
|---|---|---|
| supervisor (`supervisor.py`) | **Agent** | 동적 라우팅 (search/write/approve_send/finish), 재시도 지시, **발송까지만** 관리 |
| 검색 에이전트 (`search_agent.py`) | **Agent** (ReAct) | 도구 자율 선택·반복 (`web_search`/`open_website`), 업체별 탐색 전략 |
| 답장 에이전트 (`reply_agent.py`) | **Agent** (ReAct) | 분류 판단, 조사 필요 여부 자율 결정, 필요 시에만 `web_search` |
| 작성 모듈 (`writer_agent.py`) | **Workflow** (LLM 워커) | 도구·루프 없음 — LLM 은 도입부만, 템플릿은 코드가 보장 |
| 발송 (`send_email`) | **Workflow** (도구) | 판단 없는 결정적 실행, 사람 승인 필수 |
| grounding (`tools.CandidateStore`) | **Workflow** | 후보 정확 일치 + 공식 도메인 목격 검증 — 환각 차단 |
| 발송 승인 (`interrupt()`) | **Workflow** (사람) | 발송 최종 결정권은 항상 사람 |
| 후속 대응 탭 흐름 (`graph.py`) | **Workflow** | 고정 그래프 START → reply → END, 사람이 버튼으로 실행 |

```
[자동 실행 탭]   START → supervisor → ? → supervisor → ? …   ← 매 턴 LLM 이 다음 노드 결정 (Agent 지휘)
                   ├→ search (Agent/ReAct)
                   ├→ write  (Workflow 워커)
                   └→ approve_send (사람 승인 → 발송 도구)
[후속 대응 탭]   START → reply → END                          ← 경로 고정 (Workflow 지휘, 사람 트리거)
                   └→ reply_agent (Agent/ReAct)
```

핵심: **지휘 구조와 일꾼의 형식은 별개**입니다.
- supervisor 는 검색 에이전트(Agent)와 작성 워커(Workflow)를 함께 오케스트레이션합니다.
- reply_agent 는 그 자체로는 Agent 지만, 사람이 지휘하는 Workflow 흐름 안에서 실행됩니다.
- supervisor 는 답장을 확인하지 않습니다 — 답장은 실행 종료 후 며칠 뒤에 오므로,
  발송 이후의 시간 축은 사람이 후속 대응 탭에서 지휘합니다.

---

## 2. 구성 요소 상세 (키워드 중심)

### ① 검색 에이전트 — `agents/search_agent.py` (Agent/ReAct)
- 목표: 협찬/제휴 문의 이메일 발견 + 신뢰도 판정 (한국 업체/한국 공식몰만)
- Agent: 도구 조합·순서·반복을 LLM 이 결정 (도메인 확인 → 한정 검색 → 직접 조회 → 통합 검색은 '권장'일 뿐), `max_steps=6`
- Workflow: grounding — 도구가 수집한 후보와 **정확 일치**해야 채택, **공식 도메인에서 실제 목격**된 경우에만 `HIGH`(검증 O). 그 외 `REVIEW`/`NONE`
- 관측: 도구 호출 체인(`→`)이 카드에 표시

### ② 작성 모듈 — `agents/writer_agent.py` (Workflow, LLM 워커)
- 제목: 고정 템플릿 / 본문: 담당자 소개 + (LLM) 도입부 + 제안 블록(굵게) + 맺음말·서명
- 엄밀히는 에이전트가 아닌 **LLM 워커**: 도구·루프 없이 LLM 1회 호출을 템플릿 코드가 감쌈 — 형식 보장이 목적

### ③ 답장 에이전트 — `agents/reply_agent.py` (Agent/ReAct)
- Agent: 수락/거절/대기 분류, **조사 필요 여부 자율 판단** — 사실 질문(재학생 수, 행사 규모 등)이면 `web_search` 호출·재검색(최대 3턴), 단순 답장이면 도구 0회
- Workflow: 답장 조회는 결정적 Gmail 쿼리(발신자+제목 격리), 답장 없으면 LLM 없이 `no_reply`, 미확인 사실은 "담당자 확인 후 회신", **발송은 사람**

### ④ supervisor — `agents/supervisor.py` (Agent/관리자)
> **한 줄 요약**: supervisor 에이전트가 검색 에이전트와 작성 에이전트를 **멀티 에이전트 오케스트레이션**한다.

- 매 턴 상태표 → `search / write / approve_send / finish` 결정, 재시도 시 지시(instruction) 변경
- 범위: **발송까지** (답장 확인은 후속 대응 탭 담당)
- 안전장치(Workflow): `interrupt()` 사람 승인 없인 발송 불가, 검색 재시도 상한(2회)·턴 상한(12회), 무효 결정 시 결정적 폴백

### 발송 도구 — `google_clients.send_email` (Workflow)
- Gmail 발송 + PDF 첨부 + `{행사명}/{이름}` 라벨, `**굵게**` HTML 변환, 후속 메일은 원본 스레드에 회신(`threadId`/`In-Reply-To`)

---

## 3. 폴더 구조

```
automail/
├── automail.py           # 웹 대시보드 (Flask) — 자동 실행/후속 대응 2탭 + 기본 설정(DEFAULTS)
├── agents/
│   ├── supervisor.py     # supervisor 멀티 에이전트 그래프 (Agent — 동적 라우팅 + 발송 승인 interrupt)
│   ├── search_agent.py   # ① 검색 에이전트 (Agent/ReAct)
│   ├── writer_agent.py   # ② 작성 에이전트 (Workflow + LLM 도입부)
│   ├── reply_agent.py    # ③ 답장 에이전트 (Agent/ReAct)
│   ├── tools.py          # 에이전트 도구 (web_search/open_website) + grounding 후보 저장소 (Workflow)
│   ├── react.py          # 경량 ReAct 루프 실행기 (도구 선택 루프 + 반복 상한 + trace)
│   ├── graph.py          # 후속 대응 탭용 답장 그래프 (Workflow: START → reply → END)
│   ├── google_clients.py # OAuth + Sheets/Gmail + send_email(발송 도구)
│   ├── config.py         # .env 로드 + Gemini LLM 팩토리
│   └── state.py          # 상태 정의(WorkflowState/Company)
├── requirements.txt
├── .env                  # GEMINI_API_KEY, TAVILY_API_KEY  (직접 작성)
├── credentials.json      # Google OAuth 인증서  (직접 준비)
└── token.json            # 첫 인증 시 자동 생성
```

---

## 4. 실행 흐름 (데이터 흐름)

구글 시트가 중심 저장소입니다. 에이전트가 시트를 읽고 쓰며, Gmail 로 실제 메일을 주고받습니다.

```
[구글 시트]  업체명 · 힌트 · 이메일 · 제목 · 본문
     │  (제목·본문 열은 이메일 열의 오른쪽 칸으로 자동 지정: 이메일 F → 제목 G · 본문 H)
     ▼
[자동 실행 탭 — supervisor 가 지휘, 발송까지]
  검색(이메일 탐색·등급) → 작성(초안) → 발송 승인 대기(사람) → 발송(승인분만) → 종료
  · 각 단계 결과는 시트에 자동 저장 (승인 대기 시점 + 종료 시점)
[후속 대응 탭 — 사람이 지휘]  (답장이 실제로 도착한 뒤)
  전체 분류 → 업체 선택 → 답장 원문·후속 초안 확인 → 수정 → 발송(원본 스레드에 회신)
```

---

## 5. 설치 & 실행 (uv)

### 5-1. uv 설치
[uv](https://github.com/astral-sh/uv)는 빠른 파이썬 패키지·가상환경 관리 도구입니다.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
#  또는  brew install uv

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 5-2. 가상환경 생성 · 활성화 · 라이브러리 설치

```bash
cd automail

uv venv                        # .venv 가상환경 생성 (필요 시: uv venv --python 3.12)
source .venv/bin/activate      # 활성화 (Windows: .venv\Scripts\activate)

uv pip install -r requirements.txt   # 라이브러리 설치
```

### 5-3. 인증 준비

**(1) `.env` 작성** — 프로젝트 루트에 아래 두 키를 넣습니다.

```dotenv
GEMINI_API_KEY=여기에_Gemini_API_키      # https://aistudio.google.com/app/apikey
TAVILY_API_KEY=여기에_Tavily_API_키      # https://app.tavily.com
# GEMINI_MODEL=gemini-3.1-flash-lite     # (선택) 기본값 사용 시 생략
```

**(2) `credentials.json` 배치** — Google Cloud OAuth 클라이언트 인증서를 프로젝트 루트에 둡니다. Sheets·Gmail 권한을 사용하며, 첫 실행 시 브라우저 로그인 후 `token.json`이 자동 생성됩니다.

> `.env`, `credentials.json`, `token.json`은 개인 인증 정보이므로 **git 등 공개된 곳에 올리지 마세요.**

### 5-4. 실행

```bash
python automail.py           # → http://localhost:5002
```

> **배포 버전으로 바로 사용하기** — 로컬 설치 없이 배포된 Streamlit 앱에 접속해 바로 실행해 볼 수도 있습니다: **https://automail1398.streamlit.app/**

---

## 6. 사용법

상단 **설정** 패널에서 스프레드시트 ID·범위(업체명/힌트/이메일)·제안 내용·행사명·행사 일자·담당자·캠퍼스를 입력하고 PDF 를 첨부합니다. 상단 헤더의 **테스트 모드** 토글을 켜면 실제 업체 대신 지정한 테스트 주소로 발송해 미리 확인할 수 있습니다.

### 자동 실행 탭 (supervisor)
- **자동 실행 시작** — 시트의 기존 이메일·초안을 무시하고 **검색부터 전체 파이프라인을 새로** 실행합니다.
- **검색 건너뛰기** — 시트에 기재된 업체명·이메일을 그대로 사용해 **초안 작성부터** 진행합니다 (이메일이 빈 업체는 제외).
- 진행 로그에 supervisor 의 결정과 각 에이전트의 작업이 실시간 표시됩니다.
- 초안이 준비되면 **발송 승인 대기** 카드가 뜨며 전체가 멈춥니다. 초안을 수정하고 보낼 업체만 체크해 **"선택한 업체 발송 승인"** — 이 승인 없이는 어떤 메일도 나가지 않습니다. **"모두 건너뛰기"** 는 이번 실행에서 발송을 전부 보류합니다(초안은 시트에 저장됨).

### 후속 대응 탭 (답장 도착 후)
1. 테스트 모드 설정을 발송 때와 동일하게 두고 **"전체 분류"** — 답장 에이전트가 업체별 답장을 조회·분류합니다.
2. 왼쪽에서 업체를 선택하면 답장 원문과 후속 초안이 표시됩니다 (문의성 답장이면 에이전트가 필요 시 웹 검색으로 사실 확인 후 작성).
3. 초안을 검토·수정하고 **발송** — 원본 스레드에 답장으로 나갑니다.

---

## 7. 주의사항

- **테스트 모드로 본인 메일에 먼저 보내본 뒤** 실제 발송하세요.
- Gmail 일일 발송 한도: 일반 계정 약 **500통/일**.
- OAuth 앱이 "테스트" 상태면 `token.json`은 약 7일 후 만료됩니다. 만료 시 `token.json`을 삭제하고 다시 실행하면 재로그인됩니다.
- Gemini/Tavily 무료 티어는 분당 요청 한도가 있습니다. 자동 실행은 supervisor 판단 + 에이전트 도구 반복으로 **업체당 LLM 호출이 기존 워크플로보다 많으므로**, 한도에 걸리면 처리 개수를 줄여서 실행하세요.
