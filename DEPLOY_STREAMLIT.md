# Streamlit 배포 가이드 (automail_st.py)

## 인증 구조

- **배포 환경** (secrets 설정 시): 각 사용자가 **자기 Google 계정으로 로그인**하는
  웹 OAuth 플로우. 사용자별로 자기 Gmail 발송, 자기 스프레드시트 접근.
- **로컬 개발** (secrets 없을 때): 기존 `token.json` / `credentials.json` 방식 그대로.

## 로컬 실행

```bash
pip install -r requirements.txt
streamlit run automail_st.py
```

## 1. GCP 웹 OAuth 클라이언트 만들기

기존 `credentials.json` 은 "데스크톱 앱" 타입이라 웹 배포에 못 쓴다. 새로 만들어야 한다.

1. [Google Cloud Console](https://console.cloud.google.com) → 기존 프로젝트 선택
   (Gmail/Sheets API 가 이미 활성화된 프로젝트)
2. **API 및 서비스 → 사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID**
3. 애플리케이션 유형: **웹 애플리케이션**
4. **승인된 리디렉션 URI** 에 배포될 앱 주소를 정확히 입력 (마지막 `/` 포함):
   - `https://<앱이름>.streamlit.app/`
   - 로컬 테스트도 하려면 `http://localhost:8501/` 추가
5. 생성된 클라이언트 ID / 보안 비밀번호를 아래 secrets 에 입력

## 2. GitHub 푸시

`.gitignore` 가 `*.json`, `.env`, `uploads/` 를 제외하므로 토큰·키는 올라가지 않는다.
푸시 전에 `git status` 로 확인할 것. (`.streamlit/config.toml` 은 테마 파일이라 올려도 됨 —
단, `.streamlit/secrets.toml` 은 절대 올리지 말 것.)

## 3. Streamlit Community Cloud 설정

1. [share.streamlit.io](https://share.streamlit.io) 에서 저장소 연결,
   Main file = `automail_st.py`
2. **App settings → Secrets** 에 입력:

```toml
GEMINI_API_KEY = "AIza..."           # .env 의 값
# GEMINI_MODEL = "gemini-3.1-flash-lite"   # 바꿀 때만
TAVILY_API_KEY = "tvly-..."          # 검색 에이전트용 (.env 에 있으면)

[google]
client_id = "1234567890-abc.apps.googleusercontent.com"
client_secret = "GOCSPX-..."
redirect_uri = "https://<앱이름>.streamlit.app/"
```

`redirect_uri` 는 GCP 에 등록한 리디렉션 URI 와 **한 글자도 다르지 않게** 일치해야 한다.

## 4. OAuth 동의 화면 / 테스트 사용자 (중요)

이 앱은 `gmail.send`, `gmail.modify` 같은 **제한(restricted) 스코프**를 쓴다.

- 동의 화면이 **테스트 모드**면: **테스트 사용자로 등록된 계정(최대 100명)만 로그인 가능.**
  다른 사람에게 쓰게 하려면 GCP → OAuth 동의 화면 → 테스트 사용자에 그 사람의
  Gmail 주소를 추가하면 된다. 소규모 공유는 이걸로 충분하다.
- **불특정 다수 공개**는 Google 의 앱 검증(verification) 심사가 필요하다
  (restricted 스코프는 보안 평가까지 요구될 수 있어 개인 프로젝트에는 비현실적).
  → 사실상 "테스트 사용자 등록" 방식으로 아는 사람에게만 공유하는 것을 권장.
- 테스트 모드에서는 로그인 시 "확인되지 않은 앱" 경고가 뜨는데,
  "고급 → 이동" 으로 진행하면 된다.

## 5. 알아둘 점

- 로그인 상태는 브라우저 탭 세션 동안 유지된다. **페이지를 새로고침하면 재로그인**해야
  한다 (Streamlit 세션 특성).
- 각 사용자는 자기 계정 권한으로 동작하므로, 스프레드시트도 **자기 계정이 접근 가능한
  시트 ID** 를 설정 패널에 입력해야 한다.
- `config/` 스냅샷과 `uploads/` 첨부는 Streamlit Cloud 재배포 시 초기화된다
  (컨테이너가 비영속적). 설정은 앱에서 다시 저장하면 된다.
- Gemini/Tavily API 키는 배포자 것을 공유한다 — 사용량/과금 한도에 유의.
