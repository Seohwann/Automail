"""Google OAuth + Sheets 읽기/쓰기 + Gmail 발송/답장 조회.

기존 app.py 의 인증 패턴을 따르되, 검색 에이전트가 시트에 이메일을 '쓰기' 위해
spreadsheets 쓰기 스코프와, 답장 조회를 위해 gmail.readonly 스코프를 추가했다.
(app.py 는 readonly 시트 스코프만 사용 — token.json 은 상위 스코프로 재발급되며
 app.py 도 상위 스코프 토큰으로 정상 동작한다.)
"""
import base64
import html
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


def _markdown_to_html(text):
    """**굵게** 마크다운 + 줄바꿈을 간단한 HTML 로 변환한다."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return out.replace("\n", "<br>")


def get_or_create_label(creds, label_name):
    """라벨 이름(계층은 '/')으로 라벨 ID 를 반환. 각 계층을 없으면 생성한다."""
    if not label_name:
        return None
    service = build("gmail", "v1", credentials=creds)
    existing = {lb["name"]: lb["id"] for lb in
                service.users().labels().list(userId="me").execute().get("labels", [])}
    label_id = None
    path = ""
    for part in label_name.split("/"):
        part = part.strip()
        if not part:
            continue
        path = f"{path}/{part}" if path else part
        if path in existing:
            label_id = existing[path]
        else:
            created = service.users().labels().create(
                userId="me",
                body={"name": path, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"}).execute()
            existing[path] = created["id"]
            label_id = created["id"]
    return label_id


def send_email(creds, to_email, subject, body, sender_name=None, sender_email=None,
               attachment_path=None, label=None, thread_id=None, in_reply_to=None):
    """발송 도구: 단일 메일 발송 + 선택적 라벨/스레드 연결. 결과 dict 반환.

    thread_id / in_reply_to 를 주면 해당 답장 스레드에 '답장'으로 묶어서 보낸다.
    """
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEMultipart()
    if sender_email:
        if sender_name:
            msg["From"] = formataddr((str(Header(sender_name, "utf-8")), sender_email))
        else:
            msg["From"] = sender_email
    msg["to"] = to_email
    msg["subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if "**" in body:
        msg.attach(MIMEText(_markdown_to_html(body), "html", "utf-8"))
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment",
                            filename=os.path.basename(attachment_path))
            msg.attach(part)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_body = {"raw": raw}
    if thread_id:
        send_body["threadId"] = thread_id
    result = service.users().messages().send(userId="me", body=send_body).execute()
    if label:
        try:
            label_id = get_or_create_label(creds, label)
            if label_id:
                service.users().messages().modify(
                    userId="me", id=result["id"],
                    body={"addLabelIds": [label_id]}).execute()
        except Exception:  # noqa: BLE001 - 라벨 실패해도 발송은 성공 처리
            pass
    return result


def fetch_latest_reply(creds, from_email, newer_than_days=30, subject_query=None):
    """해당 업체 주소에서 온 가장 최근 메일 본문(텍스트)을 반환. 없으면 None.

    subject_query 를 주면 제목에 그 문구가 든 메일로 좁힌다. 테스트처럼 여러 업체를
    한 주소로 보낸 경우, 업체명을 넘겨 업체별 답장을 구분하는 데 쓴다.
    """
    service = build("gmail", "v1", credentials=creds)
    query = f"from:{from_email} newer_than:{newer_than_days}d"
    if subject_query:
        query += f' subject:"{subject_query}"'
    res = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return None
    msg = service.users().messages().get(
        userId="me", id=msgs[0]["id"], format="full"
    ).execute()
    return _extract_text(msg)


def fetch_latest_reply_meta(creds, from_email, newer_than_days=30, subject_query=None):
    """최신 답장의 본문·threadId·Message-ID 헤더를 반환(후속 메일 스레드 연결용). 없으면 None."""
    service = build("gmail", "v1", credentials=creds)
    query = f"from:{from_email} newer_than:{newer_than_days}d"
    if subject_query:
        query += f' subject:"{subject_query}"'
    res = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
    msgs = res.get("messages", [])
    if not msgs:
        return None
    msg = service.users().messages().get(
        userId="me", id=msgs[0]["id"], format="full"
    ).execute()
    headers = {h["name"].lower(): h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
    return {"text": _extract_text(msg), "thread_id": msg.get("threadId"),
            "message_id": headers.get("message-id", "")}


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
