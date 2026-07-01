"""
=============================================================
Gmail 자동 이메일 발송 웹 UI
- Flask 웹 서버로 브라우저에서 설정 입력
- Google Sheets 연동 + 템플릿 치환 + PDF 첨부 + 라벨링
=============================================================

[사전 준비]
1. pip install flask google-auth google-auth-oauthlib google-api-python-client
2. credentials.json을 이 파일과 같은 폴더에 저장
3. python app.py 실행 후 http://localhost:5001 접속
"""

import base64
import os
import re
import json
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.header import Header
from email.utils import formataddr

from flask import Flask, render_template_string, request, jsonify
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from werkzeug.utils import secure_filename

app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 발송 상태 저장
send_status = {
    "running": False,
    "results": [],
    "total": 0,
    "current": 0,
    "done": False,
    "cancelled": False,
}


def authenticate_google():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return creds


def read_spreadsheet(creds, spreadsheet_id, sheet_range):
    service = build("sheets", "v4", credentials=creds)
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=sheet_range
    ).execute()
    rows = result.get("values", [])
    
    # 범위에서 예상되는 열 개수 계산 (예: B304:C400 → 2열)
    import re as re_module
    match = re_module.search(r'([A-Z]+)\d+:([A-Z]+)\d+', sheet_range)
    expected_cols = 2  # 기본값
    if match:
        start_col, end_col = match.group(1), match.group(2)
        expected_cols = ord(end_col) - ord(start_col) + 1
    
    companies = []
    for row in rows:
        # 열 개수가 부족하면 건너뛰기 (이메일 열이 비어있는 행)
        if len(row) < expected_cols:
            continue
        if row[-1].strip():
            emails = re.split(r'[,\s\n]+', row[-1].strip())
            for email in emails:
                email = email.strip()
                if "@" in email:
                    companies.append({
                        "업체명": row[0].strip(),
                        "이메일": email,
                    })
    return companies


def get_or_create_nested_label(gmail_service, label_name):
    results = gmail_service.users().labels().list(userId="me").execute()
    labels = results.get("labels", [])
    for label in labels:
        if label["name"] == label_name:
            return label["id"]
    if "/" in label_name:
        parent_name = label_name.split("/")[0]
        parent_exists = any(l["name"] == parent_name for l in labels)
        if not parent_exists:
            gmail_service.users().labels().create(
                userId="me",
                body={"name": parent_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
            ).execute()
    new_label = gmail_service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return new_label["id"]


def get_sender_info(creds):
    """Gmail 인증된 계정의 기본 발신자 표시 이름과 이메일 주소를 가져온다."""
    service = build("gmail", "v1", credentials=creds)
    aliases = service.users().settings().sendAs().list(userId="me").execute()
    for alias in aliases.get("sendAs", []):
        if alias.get("isPrimary"):
            return alias.get("displayName", ""), alias.get("sendAsEmail", "")
    profile = service.users().getProfile(userId="me").execute()
    return "", profile.get("emailAddress", "")


def send_single_email(creds, to_email, subject, body, attachment_path=None,
                      sender_name=None, sender_email=None):
    service = build("gmail", "v1", credentials=creds)
    message = MIMEMultipart()
    if sender_email:
        if sender_name:
            message["From"] = formataddr((str(Header(sender_name, "utf-8")), sender_email))
        else:
            message["From"] = sender_email
    message["to"] = to_email
    message["subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            pdf = MIMEApplication(f.read(), _subtype="pdf")
            pdf.add_header("Content-Disposition", "attachment", filename=os.path.basename(attachment_path))
            message.attach(pdf)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    send_result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return service, send_result


def send_emails_thread(config):
    global send_status
    send_status = {"running": True, "results": [], "total": 0, "current": 0, "done": False, "cancelled": False}

    try:
        creds = authenticate_google()
        sender_name, sender_email = get_sender_info(creds)
        companies = read_spreadsheet(creds, config["spreadsheet_id"], config["sheet_range"])

        if config.get("test_mode"):
            if companies:
                companies = [{"업체명": companies[0]["업체명"], "이메일": config["test_email"]}]

        send_status["total"] = len(companies)

        for i, company in enumerate(companies):
            # 취소 확인
            if send_status["cancelled"]:
                send_status["results"].append({
                    "company": "-",
                    "email": "-",
                    "status": f"중단됨 ({i}/{len(companies)}건 발송 완료)",
                })
                break

            send_status["current"] = i + 1
            body = config["template"]
            subject = config["subject"]
            for key, value in company.items():
                if key != "이메일":
                    body = body.replace(f"{{{key}}}", value)
                    subject = subject.replace(f"{{{key}}}", value)

            try:
                gmail_service, result = send_single_email(
                    creds, company["이메일"], subject, body, config.get("attachment_path"),
                    sender_name=sender_name, sender_email=sender_email,
                )
                message_id = result["id"]

                if config.get("label_name"):
                    label_id = get_or_create_nested_label(gmail_service, config["label_name"])
                    gmail_service.users().messages().modify(
                        userId="me", id=message_id, body={"addLabelIds": [label_id]}
                    ).execute()

                send_status["results"].append({
                    "company": company["업체명"],
                    "email": company["이메일"],
                    "status": "성공",
                    "message_id": message_id,
                })
            except Exception as e:
                send_status["results"].append({
                    "company": company["업체명"],
                    "email": company["이메일"],
                    "status": f"실패: {str(e)[:80]}",
                })
    except Exception as e:
        send_status["results"].append({"company": "-", "email": "-", "status": f"시스템 오류: {str(e)[:80]}"})

    send_status["running"] = False
    send_status["done"] = True


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gmail 자동 발송 시스템</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-primary: #0a0a0a;
    --bg-secondary: #141414;
    --bg-card: #1a1a1a;
    --bg-input: #0f0f0f;
    --border: #2a2a2a;
    --border-focus: #4a4a4a;
    --text-primary: #e8e8e8;
    --text-secondary: #888;
    --text-muted: #555;
    --accent: #c8ff00;
    --accent-dim: rgba(200, 255, 0, 0.08);
    --success: #22c55e;
    --error: #ef4444;
    --warning: #f59e0b;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Noto Sans KR', sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
    line-height: 1.6;
  }

  .noise-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.03'/%3E%3C/svg%3E");
    pointer-events: none;
    z-index: 0;
  }

  .container {
    max-width: 860px;
    margin: 0 auto;
    padding: 48px 24px 80px;
    position: relative;
    z-index: 1;
  }

  header {
    margin-bottom: 48px;
    padding-bottom: 32px;
    border-bottom: 1px solid var(--border);
  }

  header h1 {
    font-size: 28px;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 8px;
  }

  header h1 span {
    color: var(--accent);
  }

  header p {
    color: var(--text-secondary);
    font-size: 14px;
    font-weight: 300;
  }

  .section {
    margin-bottom: 32px;
  }

  .section-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--text-muted);
    margin-bottom: 16px;
    padding-left: 2px;
  }

  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    transition: border-color 0.2s;
  }

  .card:hover {
    border-color: var(--border-focus);
  }

  .form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .form-group.full {
    grid-column: 1 / -1;
  }

  label {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
    letter-spacing: 0.3px;
  }

  input[type="text"], textarea {
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    color: var(--text-primary);
    font-family: 'Noto Sans KR', sans-serif;
    font-size: 13px;
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
  }

  input[type="text"]:focus, textarea:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-dim);
  }

  textarea {
    resize: vertical;
    min-height: 200px;
    line-height: 1.7;
  }

  .file-upload {
    position: relative;
    background: var(--bg-input);
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 20px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
  }

  .file-upload:hover {
    border-color: var(--accent);
    background: var(--accent-dim);
  }

  .file-upload input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
  }

  .file-upload .file-label {
    font-size: 13px;
    color: var(--text-secondary);
  }

  .file-upload .file-label span {
    color: var(--accent);
    font-weight: 500;
  }

  .file-name {
    margin-top: 8px;
    font-size: 12px;
    color: var(--accent);
    font-weight: 500;
  }

  .actions {
    display: flex;
    gap: 12px;
    margin-top: 40px;
  }

  .btn {
    padding: 12px 28px;
    border: none;
    border-radius: 8px;
    font-family: 'Noto Sans KR', sans-serif;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    letter-spacing: 0.3px;
  }

  .btn-primary {
    background: var(--accent);
    color: #000;
  }

  .btn-primary:hover {
    background: #d4ff33;
    transform: translateY(-1px);
    box-shadow: 0 4px 20px rgba(200, 255, 0, 0.2);
  }

  .btn-primary:disabled {
    background: #333;
    color: #666;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
  }

  .btn-secondary {
    background: transparent;
    color: var(--text-secondary);
    border: 1px solid var(--border);
  }

  .btn-secondary:hover {
    border-color: var(--text-secondary);
    color: var(--text-primary);
  }

  .toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 0;
    border-bottom: 1px solid var(--border);
  }

  .toggle-row:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }

  .toggle-row:first-child {
    padding-top: 0;
  }

  .toggle-info h3 {
    font-size: 14px;
    font-weight: 500;
    margin-bottom: 2px;
  }

  .toggle-info p {
    font-size: 12px;
    color: var(--text-muted);
  }

  .toggle {
    position: relative;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
  }

  .toggle input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    inset: 0;
    background: #333;
    border-radius: 12px;
    cursor: pointer;
    transition: background 0.2s;
  }

  .toggle-slider::before {
    content: '';
    position: absolute;
    width: 18px;
    height: 18px;
    left: 3px;
    top: 3px;
    background: #888;
    border-radius: 50%;
    transition: transform 0.2s, background 0.2s;
  }

  .toggle input:checked + .toggle-slider {
    background: var(--accent);
  }

  .toggle input:checked + .toggle-slider::before {
    transform: translateX(20px);
    background: #000;
  }

  /* Status Panel */
  #statusPanel {
    display: none;
    margin-top: 32px;
  }

  #statusPanel.active {
    display: block;
  }

  .progress-bar-container {
    background: var(--bg-input);
    border-radius: 4px;
    height: 6px;
    overflow: hidden;
    margin: 16px 0;
  }

  .progress-bar {
    height: 100%;
    background: var(--accent);
    border-radius: 4px;
    transition: width 0.3s;
    width: 0%;
  }

  .progress-text {
    font-size: 12px;
    color: var(--text-secondary);
    text-align: right;
  }

  .result-list {
    max-height: 400px;
    overflow-y: auto;
    margin-top: 16px;
  }

  .result-list::-webkit-scrollbar {
    width: 4px;
  }

  .result-list::-webkit-scrollbar-track {
    background: transparent;
  }

  .result-list::-webkit-scrollbar-thumb {
    background: #333;
    border-radius: 2px;
  }

  .result-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 0;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    animation: fadeIn 0.3s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .result-item .company {
    font-weight: 500;
    min-width: 120px;
  }

  .result-item .email {
    color: var(--text-secondary);
    flex: 1;
    margin: 0 12px;
    font-size: 12px;
  }

  .result-item .badge {
    font-size: 11px;
    padding: 2px 10px;
    border-radius: 4px;
    font-weight: 600;
  }

  .badge.success {
    background: rgba(34, 197, 94, 0.1);
    color: var(--success);
  }

  .badge.error {
    background: rgba(239, 68, 68, 0.1);
    color: var(--error);
  }

  .summary {
    display: flex;
    gap: 24px;
    margin-top: 16px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
  }

  .summary-item {
    font-size: 13px;
  }

  .summary-item .num {
    font-size: 24px;
    font-weight: 700;
    letter-spacing: -1px;
  }

  .summary-item .label {
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 1px;
  }

  @media (max-width: 640px) {
    .form-grid {
      grid-template-columns: 1fr;
    }
    .actions {
      flex-direction: column;
    }
  }
</style>
</head>
<body>
<div class="noise-overlay"></div>
<div class="container">

  <header>
    <h1>Gmail <span>Auto</span> Sender</h1>
    <p>Google Sheets 연동 · 템플릿 치환 · PDF 첨부 · 자동 라벨링</p>
  </header>

  <!-- Google Sheets 설정 -->
  <div class="section">
    <div class="section-title">Google Sheets 연동</div>
    <div class="card">
      <div class="form-grid">
        <div class="form-group">
          <label>Spreadsheet ID</label>
          <input type="text" id="spreadsheetId" placeholder="URL의 /d/ 뒤 문자열">
        </div>
        <div class="form-group">
          <label>Sheet Range</label>
          <input type="text" id="sheetRange" placeholder="예: Sheet1!B2:D100">
        </div>
      </div>
    </div>
  </div>

  <!-- 이메일 설정 -->
  <div class="section">
    <div class="section-title">이메일 설정</div>
    <div class="card">
      <div class="form-grid">
        <div class="form-group full">
          <label>제목 템플릿 <span style="color:var(--text-muted)">( {업체명} 자동 치환 )</span></label>
          <input type="text" id="emailSubject" placeholder="예: [{업체명}] 성균관대학교 문행대동제 프로모션 제안서 송부의 건">
        </div>
        <div class="form-group full">
          <label>본문 템플릿</label>
          <textarea id="emailTemplate" placeholder="이메일 본문을 입력하세요. {업체명}은 자동으로 치환됩니다."></textarea>
        </div>
      </div>
    </div>
  </div>

  <!-- 첨부파일 + 라벨 -->
  <div class="section">
    <div class="section-title">첨부파일 & 라벨</div>
    <div class="card">
      <div class="form-grid">
        <div class="form-group">
          <label>PDF 첨부파일</label>
          <div class="file-upload" id="fileUploadArea">
            <input type="file" id="pdfFile" accept=".pdf">
            <div class="file-label">클릭하여 <span>PDF 업로드</span></div>
          </div>
          <div class="file-name" id="fileName"></div>
        </div>
        <div class="form-group">
          <label>Gmail 라벨 <span style="color:var(--text-muted)">( / 로 계층 구분 )</span></label>
          <input type="text" id="labelName" placeholder="예: 2026 대동제/서환">
        </div>
      </div>
    </div>
  </div>

  <!-- 옵션 -->
  <div class="section">
    <div class="section-title">옵션</div>
    <div class="card">
      <div class="toggle-row">
        <div class="toggle-info">
          <h3>테스트 모드</h3>
          <p>활성화하면 지정한 이메일로 1통만 테스트 발송</p>
        </div>
        <label class="toggle">
          <input type="checkbox" id="testMode" checked>
          <span class="toggle-slider"></span>
        </label>
      </div>
      <div class="toggle-row" id="testEmailRow">
        <div class="toggle-info">
          <h3>테스트 이메일</h3>
          <input type="text" id="testEmail" value="kksh3549@gmail.com" style="margin-top:4px; width: 280px;">
        </div>
      </div>
    </div>
  </div>

  <!-- 버튼 -->
  <div class="actions">
    <button class="btn btn-primary" id="sendBtn" onclick="startSending()">발송 시작</button>
    <button class="btn btn-danger" id="cancelBtn" onclick="cancelSending()" style="display:none; background:#ef4444; color:#fff;">발송 중단</button>
    <button class="btn btn-secondary" onclick="previewEmail()">미리보기</button>
  </div>

  <!-- 상태 패널 -->
  <div id="statusPanel">
    <div class="section-title">발송 현황</div>
    <div class="card">
      <div class="progress-bar-container">
        <div class="progress-bar" id="progressBar"></div>
      </div>
      <div class="progress-text" id="progressText">준비 중...</div>
      <div class="result-list" id="resultList"></div>
      <div class="summary" id="summary" style="display:none">
        <div class="summary-item">
          <div class="num" id="successCount" style="color:var(--success)">0</div>
          <div class="label">성공</div>
        </div>
        <div class="summary-item">
          <div class="num" id="failCount" style="color:var(--error)">0</div>
          <div class="label">실패</div>
        </div>
        <div class="summary-item">
          <div class="num" id="totalCount" style="color:var(--text-primary)">0</div>
          <div class="label">전체</div>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
  // 파일 업로드 표시
  document.getElementById('pdfFile').addEventListener('change', function(e) {
    const name = e.target.files[0]?.name || '';
    document.getElementById('fileName').textContent = name;
  });

  // 테스트 모드 토글
  document.getElementById('testMode').addEventListener('change', function() {
    document.getElementById('testEmailRow').style.display = this.checked ? 'flex' : 'none';
  });

  function previewEmail() {
    const subject = document.getElementById('emailSubject').value || '(제목 없음)';
    const body = document.getElementById('emailTemplate').value || '(본문 없음)';
    const preview = subject.replace(/{업체명}/g, '[업체명 예시]') + '\\n\\n' + body.replace(/{업체명}/g, '[업체명 예시]');
    alert(preview);
  }

  async function startSending() {
    const btn = document.getElementById('sendBtn');
    const cancelBtn = document.getElementById('cancelBtn');
    const spreadsheetId = document.getElementById('spreadsheetId').value.trim();
    const sheetRange = document.getElementById('sheetRange').value.trim();
    const emailSubject = document.getElementById('emailSubject').value.trim();
    const emailTemplate = document.getElementById('emailTemplate').value.trim();
    const labelName = document.getElementById('labelName').value.trim();
    const testMode = document.getElementById('testMode').checked;
    const testEmail = document.getElementById('testEmail').value.trim();

    if (!spreadsheetId || !sheetRange || !emailSubject || !emailTemplate) {
      alert('Spreadsheet ID, Sheet Range, 제목, 본문은 필수 입력입니다.');
      return;
    }

    if (testMode && !testEmail) {
      alert('테스트 이메일 주소를 입력해주세요.');
      return;
    }

    // PDF 업로드
    const fileInput = document.getElementById('pdfFile');
    let attachmentFilename = '';
    if (fileInput.files.length > 0) {
      const formData = new FormData();
      formData.append('file', fileInput.files[0]);
      const uploadResp = await fetch('/upload', { method: 'POST', body: formData });
      const uploadData = await uploadResp.json();
      attachmentFilename = uploadData.filename;
    }

    // 발송 요청
    btn.disabled = true;
    btn.textContent = '발송 중...';
    cancelBtn.style.display = 'inline-block';

    const statusPanel = document.getElementById('statusPanel');
    statusPanel.classList.add('active');
    document.getElementById('resultList').innerHTML = '';
    document.getElementById('summary').style.display = 'none';

    const resp = await fetch('/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        spreadsheet_id: spreadsheetId,
        sheet_range: sheetRange,
        subject: emailSubject,
        template: emailTemplate,
        label_name: labelName,
        test_mode: testMode,
        test_email: testEmail,
        attachment_filename: attachmentFilename,
      }),
    });

    // 상태 폴링
    pollStatus();
  }

  async function cancelSending() {
    if (!confirm('발송을 중단하시겠습니까? 이미 발송된 메일은 취소되지 않습니다.')) return;
    await fetch('/cancel', { method: 'POST' });
    document.getElementById('cancelBtn').textContent = '중단 중...';
    document.getElementById('cancelBtn').disabled = true;
  }

  function pollStatus() {
    const interval = setInterval(async () => {
      const resp = await fetch('/status');
      const data = await resp.json();

      const pct = data.total > 0 ? (data.current / data.total * 100) : 0;
      document.getElementById('progressBar').style.width = pct + '%';
      document.getElementById('progressText').textContent = data.total > 0
        ? data.current + ' / ' + data.total
        : '준비 중...';

      const list = document.getElementById('resultList');
      list.innerHTML = '';
      data.results.forEach(r => {
        const isSuccess = r.status === '성공';
        list.innerHTML += '<div class="result-item">'
          + '<span class="company">' + r.company + '</span>'
          + '<span class="email">' + r.email + '</span>'
          + '<span class="badge ' + (isSuccess ? 'success' : 'error') + '">' + r.status + '</span>'
          + '</div>';
      });
      list.scrollTop = list.scrollHeight;

      if (data.done) {
        clearInterval(interval);
        document.getElementById('sendBtn').disabled = false;
        document.getElementById('sendBtn').textContent = '발송 시작';
        document.getElementById('cancelBtn').style.display = 'none';
        document.getElementById('cancelBtn').textContent = '발송 중단';
        document.getElementById('cancelBtn').disabled = false;

        const successCount = data.results.filter(r => r.status === '성공').length;
        const failCount = data.results.length - successCount;
        document.getElementById('successCount').textContent = successCount;
        document.getElementById('failCount').textContent = failCount;
        document.getElementById('totalCount').textContent = data.results.length;
        document.getElementById('summary').style.display = 'flex';
      }
    }, 1000);
  }
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "파일 없음"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "파일 없음"}), 400
    filename = secure_filename(file.filename)
    # 한글 파일명 보존
    if not filename or filename == "":
        filename = file.filename
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    return jsonify({"filename": filename, "path": filepath})


@app.route("/send", methods=["POST"])
def send_emails():
    data = request.json
    config = {
        "spreadsheet_id": data["spreadsheet_id"],
        "sheet_range": data["sheet_range"],
        "subject": data["subject"],
        "template": data["template"],
        "label_name": data.get("label_name", ""),
        "test_mode": data.get("test_mode", True),
        "test_email": data.get("test_email", ""),
        "attachment_path": os.path.join(UPLOAD_FOLDER, data["attachment_filename"]) if data.get("attachment_filename") else None,
    }
    thread = threading.Thread(target=send_emails_thread, args=(config,))
    thread.start()
    return jsonify({"status": "started"})


@app.route("/status")
def get_status():
    return jsonify(send_status)


@app.route("/cancel", methods=["POST"])
def cancel_sending():
    global send_status
    if send_status["running"]:
        send_status["cancelled"] = True
        return jsonify({"status": "cancelling"})
    return jsonify({"status": "not_running"})


if __name__ == "__main__":
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print("\n" + "=" * 50)
    print("  Gmail 자동 발송 시스템 웹 UI")
    print(f"  로컬 접속:  http://localhost:5001")
    print(f"  외부 접속:  http://{local_ip}:5001")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", debug=False, port=5001)