# Automail — 멀티 에이전트 협찬/제안 메일 자동화 시스템

구글 시트에 업체 목록(업체명·힌트)만 넣으면, **이메일 자동 탐색 → 업체별 맞춤 제안 메일 작성 → 일괄 발송 → 답장 분류·후속 대응**까지 이어지는 협찬/제안 메일 자동화 도구입니다. 성균관대학교 총학생회 S'PEAK 대외협력국의 협찬·부스 입점 제안 업무를 염두에 두고 만들었습니다.

- **LLM**: Google Gemini (`langchain-google-genai`)
- **웹 검색**: Tavily (`langchain-tavily`)
- **오케스트레이션**: LangGraph (검색·작성·답장 3개 에이전트를 그래프로 실행)
- **연동**: Google Sheets(업체/초안 저장), Gmail(발송·답장 조회·라벨)
- **UI**: Flask 웹 대시보드 (`automail.py`)

---

## 1. 핵심 — 3개의 에이전트

검색 → 작성 → 답장, 세 에이전트가 하나의 파이프라인으로 이어집니다. (실제 **발송**은 판단이 없는 결정적 작업이라 에이전트가 아니라 **도구**로 분리되어 있습니다.)

### ① 검색 에이전트 — `agents/search_agent.py`
- **역할**: 업체명(+ 시트의 업종/키워드 힌트)으로 웹을 검색해 협찬/제휴 문의 **이메일을 찾아내고 신뢰도를 판정**합니다. 한국 업체 또는 해외 브랜드의 **한국 지사/한국 공식몰**만 대상으로 합니다.
- **동작** (4단계 폴백 구조 — 앞 단계가 실패하면 다음 단계로):
  1. **공식 도메인 식별**: `"{업체명} {힌트} 공식 홈페이지"` 검색 → LLM이 업체 자체 도메인 확정 (포털/SNS/해외 법인 사이트 제외). 힌트에 URL을 적어두면 이 단계를 건너뛰고 그 도메인을 사용
  2. **도메인 한정 검색**: 확정된 도메인 안에서만(`include_domains`) 이메일 검색
  3. **공식 사이트 직접 조회**: 검색 인덱스에 없는 페이지 대비 — 홈페이지(+문의성 하위 페이지)를 직접 열어(fetch) 푸터·사업자 정보의 이메일 추출. 카페24류 소규모 쇼핑몰이 검색 인덱스에 수록되지 않는 사각지대를 해소
  4. **통합 웹 검색 폴백**: `"{업체명} {힌트} 협찬 제휴 문의 이메일"`로 전체 웹 검색
- **Grounding(환각 방지)**: 검색/조회 원문에서 정규식으로 이메일 **후보를 먼저 추출**하고, LLM은 그 후보 중에서만 선택 → 원문에 없는 주소는 생성 자체가 불가. 선택 결과도 후보와 **정확 일치**로 재검증 (부분 문자열 오인 방지)
- **등급 부여**: `HIGH`(검증 O)는 **공식 홈페이지에서 확인된 이메일에만** 부여 / 폴백에서 찾은 주소는 최대 `REVIEW`(검토 필요) / `NONE`(미발견)
- **관측 가능성**: 어떤 단계를 거쳤는지 검색어 체인(`→`)과 실패 사유가 결과 카드에 그대로 표시되어 판정 근거를 추적 가능
- **출력**: 이메일, 검증 등급, 판단 근거, 업체 요약 → 구글 시트 이메일 열에 저장

### ② 작성 에이전트 — `agents/writer_agent.py`
- **역할**: 업체 정보와 제안 내용을 바탕으로 **업체별 맞춤 제안 메일(제목·본문)**을 생성합니다.
- **구성**:
  - **제목**: `[업체명] 성균관대학교 {캠퍼스} {행사명} 프로모션 제안서 송부의 건` (고정 템플릿)
  - **본문**: 담당자 소개 → (LLM) 도입부 → **제안 사항 블록**(행사 일자·제안 내용·홍보 효과, 굵게 처리) → 맺음말 → 서명
  - 도입부는 LLM이 담백·정중하게 작성하고, 제안 항목·날짜·맺음말·서명은 코드가 결정적으로 붙여 형식을 보장
- **출력**: 제목·본문 → 구글 시트에 저장

### ③ 답장 에이전트 — `agents/reply_agent.py`
- **역할**: 발송 후 업체에서 온 **답장을 분류하고 후속 대응 초안을 생성**합니다.
- **동작**:
  1. Gmail에서 해당 업체가 보낸 최신 답장 조회 (제목으로 업체를 격리해 답장 섞임 방지)
  2. Gemini가 `accepted`(수락) / `rejected`(거절) / `question`(문의) / `no_reply`(무응답)로 분류
  3. 분류에 맞는 후속 이메일 초안 생성(사람이 검토 후 발송)

### 발송 — 도구(`agents/google_clients.py`의 `send_email`)
LLM 판단이 없는 결정적 실행이라 에이전트가 아닌 도구입니다. Gmail API로 메일을 보내고, **PDF 제안서 첨부**와 **`{행사명}/{이름}` 라벨**을 자동 적용합니다. 본문에 `**...**` 마커가 있으면 HTML 메일로 변환해 굵게 렌더링하고, 후속 대응 메일은 원본 답장 스레드에 **`threadId`·`In-Reply-To`로 '답장'으로 묶어** 새 메일이 아닌 회신으로 보냅니다.

---

## 2. 오케스트레이션

세 에이전트는 `agents/graph.py`의 **LangGraph** 그래프로 실행됩니다. `automail.py`(Flask 대시보드)의 각 탭이 해당 단계 그래프를 호출하고, 모두 같은 `agents/` 코어와 구글 시트를 공유합니다. 초안을 시트에 저장해두고 사람이 검토·수정한 뒤 발송하는 구조입니다.

```
검색 탭        →  build_search_graph  (START → search → END)  →  이메일·검증 등급을 시트에 저장
초안 작성 탭    →  build_write_graph   (START → write  → END)  →  제목·본문 초안을 시트에 저장
   (사람이 검토·수정)
후속 대응 탭    →  build_reply_graph   (START → reply  → END)  →  답장 분류·후속 초안
   + 발송(도구, google_clients.send_email)
```

- 각 그래프의 노드는 `state["companies"]`를 순회하며 자기 단계 필드를 채우고, **한 업체에서 오류가 나도 배치 전체가 멈추지 않도록** 예외를 격리합니다.
- `검색`/`작성`/`답장`은 LLM으로 판단하는 **에이전트 노드**, `발송`은 판단 없이 Gmail API를 호출하는 **도구**입니다.
- 상태 정의는 `agents/state.py`의 `WorkflowState`이며, 검색+작성 / 발송+답장을 묶은 배치 파이프라인(`build_prepare_graph` / `build_send_graph`)도 함께 제공됩니다.

---

## 3. 폴더 구조

```
automail/
├── automail.py           # 웹 대시보드 (Flask) — 3탭 UI + 기본 설정(DEFAULTS)
├── agents/
│   ├── search_agent.py   # ① 검색 에이전트
│   ├── writer_agent.py   # ② 작성 에이전트
│   ├── reply_agent.py    # ③ 답장 에이전트
│   ├── graph.py          # LangGraph 오케스트레이션 (검색/작성/답장 그래프)
│   ├── google_clients.py # OAuth + Sheets/Gmail + send_email(발송 도구)
│   ├── config.py         # .env 로드 + Gemini LLM 팩토리
│   └── state.py          # LangGraph 상태 정의(WorkflowState)
├── requirements.txt
├── .env                  # GEMINI_API_KEY, TAVILY_API_KEY  (직접 작성)
├── credentials.json      # Google OAuth 인증서  (직접 준비)
└── token.json            # 첫 인증 시 자동 생성
```

---

## 4. 실행 흐름 (데이터 흐름)

구글 시트가 중심 저장소입니다. 에이전트가 시트를 읽고 쓰며, Gmail로 실제 메일을 주고받습니다.

```
[구글 시트]  업체명 · 힌트 · 이메일 · 제목 · 본문
     │  (제목·본문 열은 이메일 열의 오른쪽 칸으로 자동 지정: 이메일 F → 제목 G · 본문 H)
     ▼
① 검색   업체명+힌트로 이메일 탐색·검증  →  이메일 열에 저장
② 작성   업체 정보+제안 내용으로 초안 생성 →  제목·본문 열에 저장
── (사람이 검토) ──
③ 발송   시트의 초안을 읽어 Gmail 발송(+PDF 첨부, 라벨)
④ 답장   받은 답장 조회 → 수락/거절/문의/무응답 분류 → 후속 초안
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

---

## 6. 사용법

### 웹 대시보드 (`automail.py`)
상단 **설정** 패널에서 스프레드시트 ID·범위(업체명/힌트/이메일)·제안 내용·행사명·행사 일자·담당자·캠퍼스를 입력하고 PDF를 첨부합니다. 이후 3개의 탭:

1. **검색** — 업체를 검색해 이메일·검증 등급을 확인하고 시트에 저장
2. **초안 작성** — 제안 메일 초안을 생성·편집하고, 개별/전체 발송
3. **후속 대응** — 받은 답장을 전체 분류(수락/거절/문의/무응답)하고 후속 초안을 편집·발송

상단 헤더의 **테스트 모드** 토글을 켜면 실제 업체 대신 지정한 테스트 주소로 발송해 미리 확인할 수 있습니다.

---

## 7. 주의사항

- **테스트 모드로 본인 메일에 먼저 보내본 뒤** 실제 발송하세요.
- Gmail 일일 발송 한도: 일반 계정 약 **500통/일**.
- OAuth 앱이 "테스트" 상태면 `token.json`은 약 7일 후 만료됩니다. 만료 시 `token.json`을 삭제하고 다시 실행하면 재로그인됩니다.
- Gemini/Tavily 무료 티어는 분당 요청 한도가 있어, 업체가 많으면 검색·분류가 다소 걸릴 수 있습니다.

> 참고: 저장소의 `app.py`는 초기 버전의 단순 대량 발송 Flask UI(레거시)로, 위 멀티 에이전트 시스템과는 별개입니다.
