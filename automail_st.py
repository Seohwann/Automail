"""협찬 메일 자동화 대시보드 (Streamlit 버전).

Flask 버전(automail.py)과 동일한 기능/UI 를 Streamlit 으로 옮긴 앱.
GitHub + Streamlit Community Cloud 배포를 지원한다.

  1. 자동 실행 : supervisor 멀티 에이전트가 검색 → 초안 작성 → (사람 승인) → 발송 진행
  2. 후속 대응 : 받은 답장을 수락/거절/대기로 분류 → 후속 초안 생성 → 편집 후 발송

인증 (웹 OAuth 플로우):
  - st.secrets [google] 에 client_id/client_secret/redirect_uri 가 있으면
    각 사용자가 "자기 Google 계정"으로 로그인한다 (사용자별 Gmail/시트 사용).
  - secrets 가 없으면(로컬 개발) 기존 token.json / credentials.json 방식으로 폴백.
  - Gemini/Tavily 키는 st.secrets → 환경변수로 주입 (없으면 .env 사용).

설정 기본값은 이 파일 상단의 DEFAULTS 에서 가져오며, 설정·자동 실행 상태는
로그인 세션별로 분리되어 다른 사용자와 섞이지 않는다.

  실행:  streamlit run automail_st.py
"""
import html as _html
import json
import os
import re
import threading
import time

import streamlit as st

# ---------- st.secrets → 환경변수 주입 (agents.config 임포트 전에 수행) ----------


def _secret(*keys):
    """st.secrets 에서 중첩 키를 안전하게 조회. 없으면 None."""
    try:
        v = st.secrets
        for k in keys:
            v = v[k]
        return v
    except Exception:  # noqa: BLE001 - secrets 미설정(로컬)이면 그냥 없음
        return None


for _k in ("GEMINI_API_KEY", "GEMINI_MODEL", "TAVILY_API_KEY"):
    _v = _secret(_k)
    if _v and not os.getenv(_k):
        os.environ[_k] = str(_v)

from agents.config import get_llm  # noqa: E402
from agents.google_clients import (SCOPES, authenticate,  # noqa: E402
                                   fetch_latest_reply, fetch_latest_reply_meta,
                                   get_sender_info, read_column, send_email,
                                   write_column)
from agents.graph import build_reply_graph  # noqa: E402
from agents.reply_agent import classify_reply  # noqa: E402
from agents.supervisor import build_supervisor_graph  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from google_auth_oauthlib.flow import Flow  # noqa: E402
from langgraph.types import Command  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CFG_KEYS = ["spreadsheet_id", "name_range", "hint_range", "email_range",
            "sponsor_items", "event_name", "event_date", "writer_name",
            "writer_phone", "campus", "attachment_path"]
# 기본 설정값 (UI에서 비워두면 이 값이 사용됨). 본인 스프레드시트에 맞게 수정하세요.
DEFAULTS = {
    "spreadsheet_id": "16BkIPzlETSdzbMtENtqgr0eBi0BO14UkJthKZ9uU7A8",
    "name_range": "실험용!B5:B13",
    "hint_range": "실험용!C5:C13",
    "email_range": "실험용!F5:F13",
    "sponsor_items": "문행대동제 부스 협찬: 제품 샘플 500개, 부스 배너 노출, 공식 SNS 홍보 1회",
    "event_name": "문행대동제",
}
CONFIG_DIR = os.path.join(BASE_DIR, "config")   # 설정 스냅샷 저장 폴더
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")


def _load_latest_config():
    """config/ 의 가장 최근 JSON 설정을 읽는다. 없거나 못 읽으면 빈 dict."""
    try:
        files = sorted(f for f in os.listdir(CONFIG_DIR) if f.endswith(".json"))
        if not files:
            return {}
        with open(os.path.join(CONFIG_DIR, files[-1]), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - 설정 파일 문제로 앱이 죽지 않도록
        return {}


def get_cfg():
    """로그인 세션별 설정 dict (사용자 간에 설정이 섞이지 않도록 세션에 보관)."""
    if "cfg" not in st.session_state:
        c = {k: DEFAULTS.get(k, "") for k in CFG_KEYS}
        c.update({k: v for k, v in _load_latest_config().items()
                  if k in CFG_KEYS and v})
        if not c.get("campus"):
            c["campus"] = "자연과학캠퍼스"
        if not (c.get("attachment_path") and os.path.exists(c["attachment_path"])):
            c["attachment_path"] = ""
        st.session_state["cfg"] = c
    return st.session_state["cfg"]


# ---------- 페이지 기본 설정 (로그인 화면에서도 필요하므로 먼저 수행) ----------
st.set_page_config(
    page_title="협찬 메일 자동화 대시보드",
    layout="wide",
)

st.markdown("""
<style>
  /* ---------- 공통 ---------- */

  html,
  body,
  [class*="css"] {
      font-family:
          -apple-system,
          BlinkMacSystemFont,
          "Apple SD Gothic Neo",
          "Malgun Gothic",
          sans-serif;
  }

  .block-container {
      padding-top: 2.8rem !important;
  }

  .app-header {
      margin: 8px 0 12px;
      padding: 14px 24px;
      overflow: hidden;

      background: #2b2d42;
      color: #ffffff;

      border-radius: 9px !important;
      font-size: 18px;
      font-weight: 700;
  }

  [class*="st-key-test_toggle"] {
      width: 100%;
  }

  [class*="st-key-test_toggle"] div[data-testid="stCheckbox"] {
      width: 100%;
      display: flex;
      justify-content: flex-end;
  }

  .reply-box {
      padding: 12px 14px;

      background: #f7f8fa;
      border: 1px solid #e7e9ed;
      border-radius: 8px;

      font-size: 14px;
      line-height: 1.55;
      white-space: pre-wrap;
  }

  .meta {
      color: #6b727d;
      font-size: 12.5px;
      line-height: 1.5;
  }

  .meta b {
      color: #3a3f49;
  }


  /* ---------- 진행 로그 ---------- */

  div[data-testid="stCode"] pre,
  .stCode pre {
      max-height: 320px;
      padding: 14px 16px !important;
      overflow-y: auto;

      background: #1f2133 !important;
      color: #d7dbe8 !important;

      border-radius: 9px;
      font-size: 12.5px;
      line-height: 1.55;
  }

  div[data-testid="stCode"] code,
  .stCode code {
      background: transparent !important;
      color: #d7dbe8 !important;
  }


  /* ---------- 후속 대응: 공통 Streamlit 여백 제거 ---------- */

  [class*="st-key-company_list"]
      div[data-testid="stVerticalBlock"],
  [class*="st-key-flag_col"]
      div[data-testid="stVerticalBlock"],
  [class*="st-key-flag_row_"]
      div[data-testid="stVerticalBlock"] {
      gap: 0 !important;
  }

  [class*="st-key-company_list"]
      div[data-testid="stElementContainer"],
  [class*="st-key-company_list"]
      div[data-testid="stVerticalBlockBorderWrapper"],
  [class*="st-key-flag_col"]
      div[data-testid="stElementContainer"],
  [class*="st-key-flag_row_"]
      div[data-testid="stElementContainer"] {
      margin: 0 !important;
  }


  /* ---------- 업체명 목록 ---------- */

  [class*="st-key-company_list"] {
      padding: 0 !important;
      overflow: hidden;
      gap: 0 !important;
  }

  [class*="st-key-co_row"] {
      display: flex;
      align-items: center;

      height: 45px;
      padding: 0 10px 0 15px;

      border-bottom: 1px solid #f0f1f3;
  }

  [class*="st-key-co_row"]:last-child {
      border-bottom: none;
  }

  [class*="st-key-co_row"]
      div[data-testid="stButton"] > button {
      width: 100%;
      height: 44px !important;
      min-height: 44px;
      margin: 0 !important;
      padding: 0 4px;

      justify-content: flex-start !important;

      background: transparent !important;
      color: #1a1a1a !important;

      border: none !important;
      box-shadow: none !important;

      font-weight: 400;
      text-align: left;
  }

  [class*="st-key-co_row"]
      div[data-testid="stButton"] > button:hover {
      background: #f7f8fa !important;
      color: #1a1a1a !important;
  }

  [class*="st-key-co_row"]
      div[data-testid="stButton"] > button p {
      width: 100%;
      overflow: hidden;

      font-size: 13.5px;
      line-height: 1.35;
      text-align: left;
      text-overflow: ellipsis;
      white-space: nowrap;
  }

  [class*="st-key-co_rowsel"] {
      padding-left: 12px;

      background: #eef1ff;
      border-left: 3px solid #4361ee;
  }

  [class*="st-key-co_rowsel"]
      div[data-testid="stButton"] > button:hover {
      background: transparent !important;
  }


  /* ---------- 플래그 목록 ---------- */

  [class*="st-key-flag_col"] {
      padding-top: 1px;
      gap: 0 !important;
  }

  [class*="st-key-flag_row_"],
  .flag-cell {
      display: flex;
      align-items: center;
  }

  [class*="st-key-flag_row_"] {
      min-height: 44px !important;
  }

  [class*="st-key-flag_row_"]
      div[data-testid="stVerticalBlock"] {
      min-height: 44px !important;
  }

  [class*="st-key-flag_row_"]
      div[data-testid="stElementContainer"] {
      padding: 0 !important;
  }

  .flag-cell {
      height: 45px;
  }
            
  div[data-testid="stTabs"] [role="tab"] {
      font-size: 18px !important;
      font-weight: 600 !important;
  }

  div[data-testid="stTabs"] [role="tab"] * {
      font-size: inherit !important;
      font-weight: inherit !important;
  }
</style>
""", unsafe_allow_html=True)


# ---------- 인증: 웹 OAuth 플로우 (secrets 있으면) / 로컬 폴백 ----------

def _web_client():
    """st.secrets 의 웹 OAuth 클라이언트 설정. 없으면 (None, None)."""
    cid = _secret("google", "client_id")
    csec = _secret("google", "client_secret")
    ruri = _secret("google", "redirect_uri")
    if cid and csec and ruri:
        return {
            "web": {
                "client_id": str(cid),
                "client_secret": str(csec),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }, str(ruri)
    return None, None


def _login_page(auth_url=None, error=None):
    """로그인 전용 화면을 그리고 실행을 멈춘다."""
    st.markdown('<div class="app-header">협찬 메일 자동화 대시보드</div>',
                unsafe_allow_html=True)
    st.markdown("본인 Google 계정으로 로그인하면 **본인의 Gmail 로 발송**하고 "
                "**본인의 스프레드시트**를 읽고 쓰게 됩니다.")
    if error:
        st.error(error)
    if auth_url:
        st.link_button("Google 계정으로 로그인", auth_url, type="primary")
    st.stop()


def ensure_login():
    """세션에 사용자 자격증명을 보장한다. 미로그인 시 로그인 화면에서 멈춘다."""
    c = st.session_state.get("creds")
    if c is not None:
        if c.expired and c.refresh_token:
            try:
                c.refresh(Request())
            except Exception:  # noqa: BLE001 - 갱신 실패 → 재로그인
                for k in ("creds", "sender"):
                    st.session_state.pop(k, None)
                st.rerun()
        return c

    client_config, redirect_uri = _web_client()
    if not client_config:
        # 로컬 개발 폴백: 기존 token.json / credentials.json (브라우저 로그인)
        c = authenticate()
        st.session_state["creds"] = c
        return c

    # Google 이 redirect_uri 로 되돌려준 경우 (?code=...&state=...)
    qp = st.query_params
    if "code" in qp:
        try:
            flow = Flow.from_client_config(client_config, scopes=SCOPES,
                                           redirect_uri=redirect_uri, autogenerate_code_verifier=False,
                                           state=qp.get("state"))
            flow.fetch_token(code=qp["code"])
            st.session_state["creds"] = flow.credentials
            st.query_params.clear()
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.query_params.clear()
            _login_page(error=f"로그인 실패: {e}")

    # 로그인 링크 생성
    flow = Flow.from_client_config(client_config, scopes=SCOPES, autogenerate_code_verifier=False,
                                   redirect_uri=redirect_uri)
    auth_url, _state = flow.authorization_url(
        access_type="offline",           # refresh_token 발급
        prompt="consent",
        include_granted_scopes="true")
    _login_page(auth_url=auth_url)


user_creds = ensure_login()
cfg = get_cfg()


def creds():
    return st.session_state["creds"]


@st.cache_resource
def llm():
    """LLM 은 API 키 기반이라 모든 세션이 공유해도 안전하다."""
    return get_llm()


def sender_info():
    """로그인한 사용자의 발신자 이름/이메일 (세션별)."""
    if "sender" not in st.session_state:
        st.session_state["sender"] = get_sender_info(creds())
    return st.session_state["sender"]


# ---------- 시트 범위/행 유틸 (Flask 버전과 동일 — cfg 를 명시적으로 받음) ----------

def _shift_column(rng, n):
    """A1 범위의 열 문자를 n칸 이동. '시트!F5:F13' →(n=1) '시트!G5:G13'."""
    sheet = ""
    if "!" in rng:
        sheet, rng = rng.split("!", 1)
        sheet += "!"
    m = re.match(r"([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$", rng.strip())
    if not m:
        return ""
    c1, r1, c2, r2 = m.groups()

    def shift(col):
        num = 0
        for ch in col.upper():
            num = num * 26 + (ord(ch) - 64)
        num += n
        out = ""
        while num > 0:
            num, rem = divmod(num - 1, 26)
            out = chr(65 + rem) + out
        return out

    return f"{sheet}{shift(c1)}{r1}:{shift(c2)}{r2}"


def subject_range(wcfg):
    """제목 열 = 이메일 열의 오른쪽 한 칸."""
    return _shift_column(wcfg["email_range"], 1)


def body_range(wcfg):
    """본문 열 = 이메일 열의 오른쪽 두 칸."""
    return _shift_column(wcfg["email_range"], 2)


def read_aligned(c, wcfg):
    """업체명/힌트/이메일/제목/본문을 행 정렬해 dict 리스트로 반환."""
    ranges = [wcfg["name_range"], wcfg["hint_range"], wcfg["email_range"],
              subject_range(wcfg), body_range(wcfg)]
    cols = [read_column(c, wcfg["spreadsheet_id"], rng) for rng in ranges]
    n = max((len(x) for x in cols), default=0)
    cols = [(x + [""] * n)[:n] for x in cols]
    rows = []
    for name, hint, email, subject, body in zip(*cols):
        rows.append({"name": name, "hint": hint, "email": email,
                     "subject": subject, "body": body})
    return rows


def _reply_subject(subject):
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _label(wcfg):
    """설정의 행사명/이름으로 Gmail 라벨 문자열('{행사명}/{이름}')을 만든다."""
    ev = (wcfg.get("event_name") or "").strip()
    nm = (wcfg.get("writer_name") or "").strip()[1:]
    if ev and nm:
        return f"{ev}/{nm}"
    return ev or nm or None


def load_companies(c, wcfg):
    names = read_column(c, wcfg["spreadsheet_id"], wcfg["name_range"])
    emails = read_column(c, wcfg["spreadsheet_id"], wcfg["email_range"])
    subjects = read_column(c, wcfg["spreadsheet_id"], subject_range(wcfg))
    bodies = read_column(c, wcfg["spreadsheet_id"], body_range(wcfg))
    companies = []
    for name, email, subject, body in zip(names, emails, subjects, bodies):
        if email and subject and body:
            companies.append({"name": name, "email": email,
                              "subject": subject, "body": body})
    return companies


# ---------- 자동 실행 (supervisor 멀티 에이전트) — 백그라운드 워커 ----------

def get_auto():
    """세션별 자동 실행 상태 (사용자마다 독립적으로 실행)."""
    if "AUTO" not in st.session_state:
        st.session_state["AUTO"] = {
            "running": False, "done": False, "error": "",
            "log": [], "pending": None, "pending_seq": 0, "results": [],
            "indices": [], "rows": [],
            "event": threading.Event(), "resume": None,
        }
    return st.session_state["AUTO"]


AUTO = get_auto()


def _auto_log(auto, msg):
    auto["log"].append(str(msg))


def _persist_auto_rows(auto, c, wcfg, companies):
    """supervisor 결과를 전체 행에 반영하고 시트(이메일/제목/본문 열)에 저장."""
    rows = auto["rows"]
    for local, comp in zip(auto["indices"], companies):
        rows[local].update(comp)
    write_column(c, wcfg["spreadsheet_id"], wcfg["email_range"],
                 [r.get("email", "") for r in rows])
    write_column(c, wcfg["spreadsheet_id"], subject_range(wcfg),
                 [r.get("subject", "") for r in rows])
    write_column(c, wcfg["spreadsheet_id"], body_range(wcfg),
                 [r.get("body", "") for r in rows])


def _auto_worker(auto, c, model, sender, wcfg, rows, limit, test_email_addr,
                 mode="fresh"):
    """백그라운드에서 supervisor 그래프를 실행. interrupt(발송 승인) 시 사람을 기다린다.

    세션 상태(auto)/자격증명(c)/설정(wcfg)은 메인 스레드에서 준비해 넘긴다
    (백그라운드 스레드에서 st.session_state 를 건드리지 않기 위함).
    """
    try:
        targets, considered = [], 0
        for i, r in enumerate(rows):
            if not r["name"]:
                continue
            if limit and considered >= limit:
                break
            considered += 1
            targets.append(i)
        if not targets:
            auto["error"] = "처리할 업체가 없습니다."
            return
        auto["rows"], auto["indices"] = rows, targets
        if mode == "skip":
            # 검색 건너뛰기: 시트의 업체명+이메일을 그대로 쓰고 '작성'부터 진행.
            # 검색 시도 횟수를 상한으로 채워 supervisor 가 검색을 고를 수 없게 한다.
            _auto_log(auto, "[검색 건너뛰기] 시트의 이메일을 그대로 사용해 초안 작성부터 진행합니다.")
            no_email = []
            for i in targets:
                for k in ("tier", "verified", "verify_reason", "query",
                          "sent", "message_id", "reply_status", "follow_up"):
                    rows[i].pop(k, None)
                rows[i]["subject"] = rows[i]["body"] = ""
                rows[i]["search_attempts"] = 99   # 검색 차단 (재시도 상한 초과)
                if not rows[i].get("email"):
                    no_email.append(rows[i]["name"])
            if no_email:
                _auto_log(auto, "[검색 건너뛰기] 이메일이 없어 제외되는 업체: "
                          + ", ".join(no_email))
        else:
            # 전체 새로 실행: 시트에 있던 이메일/등급/초안을 비우고 검색부터 진행
            _auto_log(auto, "[재검색] 시트의 기존 이메일·초안을 무시하고 처음부터 실행합니다.")
            for i in targets:
                for k in ("email", "tier", "verified", "verify_reason", "query",
                          "info", "subject", "body", "sent", "message_id",
                          "reply_status", "follow_up", "search_attempts"):
                    rows[i].pop(k, None)
                rows[i]["email"] = rows[i]["subject"] = rows[i]["body"] = ""
        sender_name, sender_email = sender
        graph = build_supervisor_graph(c, model,
                                       on_event=lambda m: _auto_log(auto, m))
        config = {"configurable": {"thread_id": f"auto-{time.time()}"},
                  "recursion_limit": 100}
        state = {
            "companies": [dict(rows[i]) for i in targets],
            "sponsor_items": wcfg["sponsor_items"],
            "sender_name": sender_name, "sender_email": sender_email,
            "campus": wcfg.get("campus", ""),
            "writer_name": wcfg.get("writer_name", ""),
            "event_name": wcfg.get("event_name", ""),
            "writer_phone": wcfg.get("writer_phone", ""),
            "event_date": wcfg.get("event_date", ""),
            "test_email": test_email_addr,
            "attachment_path": wcfg.get("attachment_path", ""),
            "label": _label(wcfg) or "",
        }
        result = graph.invoke(state, config)
        while "__interrupt__" in result:
            # 발송 승인 대기: 현재까지의 초안을 시트에 저장해 두고 사람을 기다린다
            try:
                _persist_auto_rows(auto, c, wcfg, result.get("companies") or [])
            except Exception as e:  # noqa: BLE001 - 시트 저장 실패해도 계속
                _auto_log(auto, f"[경고] 시트 저장 실패: {e}")
            auto["pending_seq"] += 1
            auto["pending"] = result["__interrupt__"][0].value
            _auto_log(auto, "[관리자] 발송 승인 대기 — 대시보드에서 승인해 주세요.")
            auto["event"].clear()
            auto["event"].wait()
            auto["pending"] = None
            result = graph.invoke(Command(resume=auto["resume"]), config)
        comps = result.get("companies", [])
        try:
            _persist_auto_rows(auto, c, wcfg, comps)
        except Exception as e:  # noqa: BLE001
            _auto_log(auto, f"[경고] 시트 저장 실패: {e}")
        auto["results"] = comps
        auto["done"] = True
        _auto_log(auto, "[완료] 자동 실행 종료")
    except Exception as e:  # noqa: BLE001
        auto["error"] = str(e)
        _auto_log(auto, f"[오류] {e}")
    finally:
        auto["running"] = False


def start_auto(limit, test_email_addr, mode):
    """워커 스레드 기동. 인증/LLM/시트 읽기는 메인 스레드에서 준비."""
    if AUTO["running"]:
        st.error("이미 실행 중입니다.")
        return
    AUTO.update({"running": True, "done": False, "error": "", "log": [],
                 "pending": None, "results": [], "resume": None})
    try:
        with st.spinner("인증·시트 읽기 준비 중…"):
            c, model, sender = creds(), llm(), sender_info()
            rows = read_aligned(c, cfg)
    except Exception as e:  # noqa: BLE001
        AUTO["running"] = False
        AUTO["error"] = str(e)
        st.error(f"실패: {e}")
        return
    threading.Thread(target=_auto_worker,
                     args=(AUTO, c, model, sender, dict(cfg), rows,
                           limit, test_email_addr, mode),
                     daemon=True).start()
    st.session_state["auto_polling"] = True


# ---------- UI 헬퍼 ----------

STATUS_LABEL = {"accepted": "수락", "rejected": "거절", "question": "대기",
                "no_reply": "무응답", "loading": "분류중…"}
BADGE_CSS = {
    "ok": ("#e3f6e8", "#1c7c3a"), "no": ("#fde4e4", "#c0322b"),
    "accepted": ("#e3f6e8", "#1c7c3a"), "rejected": ("#fde4e4", "#c0322b"),
    "question": ("#fff2d8", "#9a6b00"), "no_reply": ("#eceef1", "#7a808a"),
    "loading": ("#eef1ff", "#4a5bd0"),
}


def esc(s):
    return _html.escape(s or "")


def badge(kind, text):
    bg, fg = BADGE_CSS.get(kind, ("#eceef1", "#7a808a"))
    return (f'<span style="font-size:11px;padding:2px 9px;border-radius:10px;'
            f'font-weight:600;white-space:nowrap;background:{bg};color:{fg}">'
            f'{esc(text)}</span>')


def status_badge(s):
    return badge(s, STATUS_LABEL.get(s, "")) if s else ""


def tier_badge(t):
    if t == "HIGH":
        return badge("ok", "검증 O")
    if t == "REVIEW":
        return badge("question", "검토 필요")
    return badge("no", "미발견")


def email_badge(tier, email):
    if tier:
        return tier_badge(tier)
    if email:
        return badge("no_reply", "시트 입력·미검증")
    return tier_badge("")


# ---------- 헤더: 제목 + 테스트 모드 + 계정 ----------

h1, h2, h3 = st.columns(
    [5, 1.5, 2.5],
    vertical_alignment="center",
)

with h1:
    st.markdown(
        '<div class="app-header">협찬 메일 자동화 대시보드</div>',
        unsafe_allow_html=True,
    )

with h2:
    st.checkbox(
        "테스트 모드",
        value=True,
        key="test_toggle",
    )

with h3:
    st.text_input(
        "테스트 수신 주소",
        value="seohwan3549@gmail.com",
        key="test_email_input",
        label_visibility="collapsed",
        placeholder="테스트 수신 주소",
    )

try:
    _sname, _semail = sender_info()
    a1, a2 = st.columns([6, 1.4], vertical_alignment="center")
    a1.caption(f"로그인 계정: {_semail}" + (f" ({_sname})" if _sname else ""))
    if a2.button("로그아웃", use_container_width=True):
        for k in ("creds", "sender", "AUTO", "companies", "current",
                  "detail", "cfg"):
            st.session_state.pop(k, None)
        st.rerun()
except Exception as e:  # noqa: BLE001
    st.warning(f"계정 정보를 불러오지 못했습니다: {e}")


def test_email():
    return (st.session_state.get("test_email_input", "").strip()
            if st.session_state.get("test_toggle") else "")


# ---- 설정 / 첨부 ----
with st.expander("설정 / 첨부", expanded=False):
    g1, g2, g3 = st.columns(3)
    with g1:
        st.text_input("Spreadsheet ID", value=cfg["spreadsheet_id"],
                      key="c_spreadsheet_id",
                      placeholder="예: 1AbCdEf... (URL의 /d/ 뒤 문자열)")
        st.text_input("이메일 범위", value=cfg["email_range"],
                      key="c_email_range", placeholder="예: 실험용!F5:F13")
    with g2:
        st.text_input("업체명 범위", value=cfg["name_range"],
                      key="c_name_range", placeholder="예: 실험용!B5:B13")
    with g3:
        st.text_input("힌트 범위", value=cfg["hint_range"],
                      key="c_hint_range", placeholder="예: 실험용!C5:C13")
    st.text_area("제안 내용", value=cfg["sponsor_items"], key="c_sponsor_items",
                 height=68,
                 placeholder="예: 제안 내용: 협찬 가능한 제품 500개, 홍보 효과: 부스 배너 노출·공식 SNS 홍보")
    e1, e2, e3, e4, e5 = st.columns([1.4, 1.1, 1.1, 1.2, 2.2])
    with e1:
        st.text_input("행사명", value=cfg["event_name"], key="c_event_name",
                      placeholder="예: 문행대동제")
    with e2:
        st.text_input("행사 일자", value=cfg["event_date"], key="c_event_date",
                      placeholder="예: 2026-05-12")
    with e3:
        st.text_input("담당자 이름", value=cfg["writer_name"], key="c_writer_name",
                      placeholder="예: 김서환")
    with e4:
        st.text_input("연락처(Mobile)", value=cfg["writer_phone"],
                      key="c_writer_phone", placeholder="예: 010-1234-5678")
    with e5:
        campus_opts = ["자연과학캠퍼스", "인문사회과학캠퍼스"]
        st.radio("캠퍼스", campus_opts,
                 index=campus_opts.index(cfg.get("campus", "자연과학캠퍼스"))
                 if cfg.get("campus") in campus_opts else 0,
                 key="c_campus", horizontal=True)
    up = st.file_uploader("제안서 PDF 첨부", type=["pdf"], key="c_pdf")
    if up is not None:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, os.path.basename(up.name))
        with open(path, "wb") as f:
            f.write(up.getbuffer())
        cfg["attachment_path"] = os.path.abspath(path)
        st.caption(f"첨부: {up.name}")
    elif cfg["attachment_path"]:
        st.caption(f"첨부: {os.path.basename(cfg['attachment_path'])}")
    else:
        st.caption("첨부 없음")
    if st.button("설정 저장", type="primary"):
        for k in CFG_KEYS:
            if k in ("attachment_path", "campus"):
                continue
            v = (st.session_state.get("c_" + k) or "").strip()
            if v:
                cfg[k] = v
        cfg["campus"] = st.session_state.get("c_campus", "자연과학캠퍼스")
        os.makedirs(CONFIG_DIR, exist_ok=True)
        snap = os.path.join(CONFIG_DIR, time.strftime("%Y%m%d_%H%M%S") + ".json")
        with open(snap, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        st.success("저장됨")


tab_auto, tab_reply = st.tabs(["자동 실행 (에이전트)", "후속 대응"])


# ---------- 탭 1: 자동 실행 ----------

with tab_auto:
    b1, b2, b3 = st.columns([1.6, 1.4, 1.6])
    with b1:
        st.text_input("처리 개수 (빈칸=전체)", value="3", key="auto_limit")
    with b2:
        st.write("")
        st.write("")
        fresh_clicked = st.button("자동 실행 시작", type="primary",
                                  disabled=AUTO["running"], use_container_width=True)
    with b3:
        st.write("")
        st.write("")
        skip_clicked = st.button("재검색 건너뛰기", disabled=AUTO["running"],
                                 use_container_width=True)
    st.markdown(
        '<div class="meta"><b>자동 실행 시작</b>: 시트의 기존 이메일·초안을 무시하고 '
        '검색 → 초안 작성 → (사람 승인) → 발송 → 답장 확인을 처음부터 진행합니다. '
        '<b>재검색 건너뛰기</b>: 시트에 기재된 업체명·이메일을 그대로 사용해 초안 작성부터 '
        '진행합니다. 두 경우 모두 발송 전에는 반드시 아래에서 승인해야 합니다.</div>',
        unsafe_allow_html=True)

    # 시작 전 확인 (Flask 의 confirm() 대체)
    if fresh_clicked:
        st.session_state["auto_confirm"] = "fresh"
    if skip_clicked:
        st.session_state["auto_confirm"] = "skip"
    if st.session_state.get("auto_confirm") and not AUTO["running"]:
        mode = st.session_state["auto_confirm"]
        t = test_email()
        note = (f"테스트 주소({t})로 발송됩니다." if t
                else "테스트 모드 꺼짐 — 실제 업체에 발송될 수 있습니다!")
        head = ("시트에 기재된 이메일을 그대로 사용해 초안 작성부터 진행합니다. "
                if mode == "skip"
                else "시트의 기존 이메일·초안을 무시하고 처음부터 재검색·재작성합니다. ")
        st.warning(head + note + " 계속할까요?")
        c_ok, c_no, _sp = st.columns([1, 1, 4])
        if c_ok.button("계속", type="primary", key="confirm_go"):
            st.session_state["auto_confirm"] = None
            limit_raw = (st.session_state.get("auto_limit") or "").strip()
            limit = int(limit_raw) if limit_raw.isdigit() else 0
            start_auto(limit, t, mode)
            st.rerun()
        if c_no.button("취소", key="confirm_cancel"):
            st.session_state["auto_confirm"] = None
            st.rerun()

    def render_approval(p):
        seq = AUTO["pending_seq"]
        t = p.get("test_email") or ""
        note = (f"테스트 모드: {t} 로 발송됩니다." if t else "실제 업체 주소로 발송됩니다!")
        st.markdown("#### 발송 승인 대기 " + badge("question", "사람 확인 필요"),
                    unsafe_allow_html=True)
        st.markdown(f'<div class="meta">{esc(note)} 발송할 업체를 선택하고 필요하면 '
                    '수정한 뒤 승인하세요.</div>', unsafe_allow_html=True)
        drafts = p.get("drafts") or []
        for d in drafts:
            i = d["i"]
            with st.container(border=True):
                cchk, cbdg = st.columns([4, 2])
                cchk.checkbox(d.get("name", ""), value=True, key=f"ap_{seq}_{i}")
                cbdg.markdown(email_badge(d.get("tier"), d.get("email")),
                              unsafe_allow_html=True)
                st.markdown(f'<div class="meta"><b>수신</b> {esc(d.get("email",""))}</div>',
                            unsafe_allow_html=True)
                st.text_input("제목", value=d.get("subject", ""),
                              key=f"as_{seq}_{i}", label_visibility="collapsed")
                st.text_area("본문", value=d.get("body", ""), height=220,
                             key=f"ab_{seq}_{i}", label_visibility="collapsed")
        a1, a2, _sp = st.columns([1.6, 1.4, 3])
        if a1.button("선택한 업체 발송 승인", type="primary", key=f"ap_go_{seq}"):
            approved = []
            for d in drafts:
                i = d["i"]
                if st.session_state.get(f"ap_{seq}_{i}"):
                    approved.append({
                        "i": i,
                        "subject": (st.session_state.get(f"as_{seq}_{i}") or "").strip(),
                        "body": (st.session_state.get(f"ab_{seq}_{i}") or "").strip(),
                    })
            AUTO["resume"] = {"approved": approved}
            AUTO["event"].set()
            st.toast("승인 전송됨")
        if a2.button("발송 건너뛰기", key=f"ap_skip_{seq}"):
            AUTO["resume"] = {"approved": []}
            AUTO["event"].set()
            st.toast("발송을 건너뜁니다")

    def render_auto_results(results):
        if not results:
            return
        st.markdown("#### 결과 요약")
        for c in results:
            with st.container(border=True):
                if c.get("sent"):
                    sent = badge("ok", "발송됨")
                elif c.get("skipped"):
                    sent = badge("no_reply", "건너뜀")
                else:
                    sent = badge("no", "미발송")
                rep = (" " + status_badge(c.get("reply_status"))
                       if c.get("reply_status") else "")
                st.markdown(f'**{esc(c.get("name",""))}** '
                            + email_badge(c.get("tier"), c.get("email"))
                            + " " + sent + rep, unsafe_allow_html=True)
                st.markdown(f'<div class="meta"><b>이메일</b> '
                            f'{esc(c.get("email","")) or "(미발견)"}</div>',
                            unsafe_allow_html=True)
                if c.get("follow_up"):
                    st.markdown("**후속 초안**")
                    st.markdown(f'<div class="reply-box">{esc(c["follow_up"])}</div>',
                                unsafe_allow_html=True)

    @st.fragment(run_every="1.5s" if AUTO["running"] else None)
    def auto_status_area():
        if AUTO["pending"]:
            render_approval(AUTO["pending"])
        st.markdown("**진행 로그**")
        st.code("\n".join(AUTO["log"]) if AUTO["log"] else " ", language=None)
        if not AUTO["running"]:
            if AUTO["error"]:
                st.error("오류: " + AUTO["error"])
            elif AUTO["done"]:
                st.success("완료")
            render_auto_results(AUTO["results"])
            if st.session_state.get("auto_polling"):
                # 실행 종료 → 전체 rerun 으로 버튼 활성화 및 폴링 중단
                st.session_state["auto_polling"] = False
                st.rerun()
        else:
            st.caption("실행 중…")

    auto_status_area()


# ---------- 탭 2: 후속 대응 ----------

with tab_reply:
    r1, r2, _sp = st.columns([1.2, 1, 4])
    refresh_clicked = r1.button("목록 새로고침", use_container_width=True)
    classify_clicked = r2.button("전체 분류", type="primary", use_container_width=True)

    if refresh_clicked or ("companies" not in st.session_state):
        try:
            with st.spinner("불러오는 중…"):
                st.session_state["companies"] = [
                    {"name": c["name"], "status": None}
                    for c in load_companies(creds(), cfg)]
            st.session_state["current"] = None
            st.session_state["detail"] = None
        except Exception as e:  # noqa: BLE001
            st.session_state["companies"] = []
            st.error(f"오류: {e}")

    companies = st.session_state.get("companies") or []

    if classify_clicked and companies:
        try:
            with st.spinner("전체 답장을 조회·분류하는 중…"):
                comps = load_companies(creds(), cfg)
                sender_name, _se = sender_info()
                t = test_email()
                if t:
                    for c in comps:
                        c["email"] = t
                result = build_reply_graph(creds(), llm()).invoke(
                    {"companies": comps, "sender_name": sender_name})
                for i, c in enumerate(result["companies"]):
                    if i < len(companies):
                        companies[i]["status"] = c.get("reply_status", "no_reply")
        except Exception as e:  # noqa: BLE001
            st.error(f"오류: {e}")

    side, rmain = st.columns([1, 2.6], gap="medium")

    with side:
        clicked_idx = None
        if not companies:
            st.info("발송 대상이 없습니다. 먼저 초안 작성을 실행하세요.")
        else:
            lcol, bcol = st.columns([3.4, 1], gap="small")

            with lcol:
                # 업체명 목록: 하나의 박스, 구분선으로 행 구분
                with st.container(border=True, key="company_list", gap=None):
                    for i, c in enumerate(companies):
                        row_key = (
                            f"co_rowsel_{i}"
                            if st.session_state.get("current") == i
                            else f"co_row_{i}"
                        )

                        with st.container(key=row_key, gap=None):
                            if st.button(
                                c["name"],
                                key=f"co_{i}",
                                use_container_width=True,
                            ):
                                clicked_idx = i

            with bcol:
                # 각 플래그를 고유한 key의 컨테이너에 배치
                with st.container(key="flag_col", gap=None):
                    for i, c in enumerate(companies):
                        with st.container(key=f"flag_row_{i}", gap=None):
                            cell = (
                                status_badge(c["status"])
                                if c.get("status")
                                else ""
                            )

                            st.markdown(
                                f'<div class="flag-cell">{cell}</div>',
                                unsafe_allow_html=True,
                            )
        if clicked_idx is not None:
            i = clicked_idx
            st.session_state["current"] = i
            st.session_state["detail"] = None
            try:
                with st.spinner("답장을 가져와 분석하는 중…"):
                    comp = load_companies(creds(), cfg)[i]
                    from_email = test_email() or comp["email"]
                    reply_text = fetch_latest_reply(
                        creds(), from_email, subject_query=comp["name"])
                    reply_subject = _reply_subject(comp["subject"])
                    if not reply_text:
                        d = {"name": comp["name"], "reply_status": "no_reply",
                             "reply_text": "", "follow_up": "",
                             "reply_subject": reply_subject}
                    else:
                        sender_name, _se = sender_info()
                        decision = classify_reply(comp["name"], reply_text,
                                                  sender_name, llm())
                        d = {"name": comp["name"],
                             "reply_status": decision["reply_status"],
                             "reply_text": reply_text,
                             "follow_up": decision["follow_up"],
                             "reply_subject": reply_subject}
                st.session_state["detail"] = d
                st.session_state["detail_seq"] = \
                    st.session_state.get("detail_seq", 0) + 1
                companies[i]["status"] = d["reply_status"]
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.session_state["detail"] = {"error": str(e)}

    with rmain:
        d = st.session_state.get("detail")
        cur = st.session_state.get("current")
        if d is None:
            st.markdown('<div style="color:#9098a3;text-align:center;margin-top:70px;'
                        'font-size:15px">왼쪽에서 업체를 선택하세요.</div>',
                        unsafe_allow_html=True)
        elif d.get("error"):
            st.error("오류: " + d["error"])
        else:
            seq = st.session_state.get("detail_seq", 0)
            has_reply = bool(d["reply_text"])
            st.markdown(f'#### {esc(d["name"])} ' + status_badge(d["reply_status"]),
                        unsafe_allow_html=True)
            st.markdown("**받은 답장**")
            if has_reply:
                st.markdown(f'<div class="reply-box">{esc(d["reply_text"])}</div>',
                            unsafe_allow_html=True)
            else:
                st.markdown('<div class="reply-box" style="color:#9098a3">아직 이 '
                            '업체에서 온 답장이 없습니다.</div>', unsafe_allow_html=True)
            st.markdown("**후속 메일 제목**")
            st.text_input("후속 메일 제목", value=d["reply_subject"],
                          key=f"r_subj_{seq}", label_visibility="collapsed")
            st.markdown("**후속 메일 초안 (수정 가능)**")
            st.text_area("후속 메일 초안", value=d["follow_up"], height=180,
                         key=f"r_body_{seq}", label_visibility="collapsed")
            s1, _sp2 = st.columns([1, 5])
            send_clicked = s1.button("발송", type="primary", disabled=not has_reply)
            t = test_email()
            nm = d["name"]
            st.caption(f"테스트 모드: {t} 로 발송됩니다." if t
                       else f"실제 발송: {nm} 의 답장 주소로 보냅니다.")
            if send_clicked:
                subject = (st.session_state.get(f"r_subj_{seq}") or "").strip()
                body = (st.session_state.get(f"r_body_{seq}") or "").strip()
                if not subject or not body:
                    st.error("제목과 본문을 모두 채워주세요.")
                else:
                    try:
                        with st.spinner("발송 중…"):
                            comp = load_companies(creds(), cfg)[cur]
                            recipient = t or comp["email"]
                            sender_name, sender_email = sender_info()
                            # 원본 답장 스레드에 '답장'으로 묶기
                            meta = fetch_latest_reply_meta(
                                creds(), recipient, subject_query=comp["name"]) or {}
                            send_email(creds(), recipient, subject, body,
                                       sender_name=sender_name,
                                       sender_email=sender_email,
                                       label=_label(cfg),
                                       thread_id=meta.get("thread_id"),
                                       in_reply_to=meta.get("message_id"))
                        st.success(f"발송 완료 → {recipient}")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"실패: {e}")
