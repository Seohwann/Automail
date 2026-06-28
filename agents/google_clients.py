"""Google OAuth + Sheets 읽기/쓰기 + Gmail 발송/답장 조회.

기존 app.py 의 인증 패턴을 따르되, 검색 에이전트가 시트에 이메일을 '쓰기' 위해
spreadsheets 쓰기 스코프와, 답장 조회를 위해 gmail.readonly 스코프를 추가했다.
(app.py 는 readonly 시트 스코프만 사용 — token.json 은 상위 스코프로 재발급되며
 app.py 도 상위 스코프 토큰으로 정상 동작한다.)
"""
import base64
import os
import re
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",  # 읽기 + 쓰기
]


def authenticate():
    """OAuth 인증. token.json 재사용, 없으면 브라우저 로그인."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return creds


# ---------- Google Sheets ----------

def read_company_names(creds, spreadsheet_id, name_range):
    """업체명 열을 읽어 이름 리스트로 반환 (빈 칸 제외)."""
    service = build("sheets", "v4", credentials=creds)
    res = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=name_range
    ).execute()
    rows = res.get("values", [])
    return [row[0].strip() for row in rows if row and row[0].strip()]


def _row_span(rng):
    """'시트1!F5:F20' 같은 A1 범위에서 행 개수를 계산. 못 구하면 0."""
    m = re.search(r"[A-Z]+(\d+):[A-Z]+(\d+)", rng)
    return int(m.group(2)) - int(m.group(1)) + 1 if m else 0


def read_column(creds, spreadsheet_id, rng):
    """한 열을 읽어 문자열 리스트로 반환.

    Sheets API 는 뒤쪽 빈 행을 잘라서 돌려주므로, 범위가 가리키는 행 개수에 맞춰
    빈 문자열로 패딩한다. 그래야 여러 열을 인덱스로 정렬해 합칠 수 있다.
    """
    service = build("sheets", "v4", credentials=creds)
    res = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=rng
    ).execute()
    rows = res.get("values", [])
    values = [(row[0].strip() if row else "") for row in rows]
    n = _row_span(rng)
    if n:
        values = (values + [""] * n)[:n]
    return values


def write_column(creds, spreadsheet_id, rng, values):
    """리스트를 한 열에 일괄 기록 (범위 좌상단부터 값 개수만큼)."""
    service = build("sheets", "v4", credentials=creds)
    body = {"values": [[v] for v in values]}
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueInputOption="RAW",
        body=body,
    ).execute()


# ---------- Gmail ----------

def get_sender_info(creds):
    """인증된 계정의 기본 발신자 표시 이름과 이메일을 가져온다."""
    service = build("gmail", "v1", credentials=creds)
    aliases = service.users().settings().sendAs().list(userId="me").execute()
    for alias in aliases.get("sendAs", []):
        if alias.get("isPrimary"):
            return alias.get("displayName", ""), alias.get("sendAsEmail", "")
    profile = service.users().getProfile(userId="me").execute()
    return "", profile.get("emailAddress", "")


def send_email(creds, to_email, subject, body,
               sender_name=None, sender_email=None, attachment_path=None):
    """단일 메일 발송. 메시지 ID 가 담긴 결과 dict 반환."""
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEMultipart()
    if sender_email:
        if sender_name:
            msg["From"] = formataddr((str(Header(sender_name, "utf-8")), sender_email))
        else:
            msg["From"] = sender_email
    msg["to"] = to_email
    msg["subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment",
                            filename=os.path.basename(attachment_path))
            msg.attach(part)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def fetch_latest_reply(creds, from_email, newer_than_days=30):
    """해당 업체 주소에서 온 가장 최근 메일 본문(텍스트)을 반환. 없으면 None."""
    service = build("gmail", "v1", credentials=creds)
    query = f"from:{from_email} newer_than:{newer_than_days}d"
    res = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return None
    msg = service.users().messages().get(
        userId="me", id=msgs[0]["id"], format="full"
    ).execute()
    return _extract_text(msg)


def _extract_text(msg):
    """Gmail 메시지에서 평문 본문을 뽑는다. 실패 시 snippet 사용."""
    payload = msg.get("payload", {})

    def walk(part):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for sub in part.get("parts", []) or []:
            text = walk(sub)
            if text:
                return text
        return None

    return walk(payload) or msg.get("snippet", "")
