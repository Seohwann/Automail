"""협찬 메일 자동화 대시보드 (독립 실행 웹 UI).

상단 설정 패널에서 스프레드시트 ID·범위·협찬 품목을 입력하고 제안서 PDF를 첨부한 뒤,
세 개의 탭에서 에이전트를 단계별로 실행한다.

  1. 검색      : 시트의 업체명+힌트로 웹 검색 → 이메일 추출/검증 → 시트(이메일 열) 저장 + 표시
  2. 초안 작성 : 업체 정보+협찬 품목으로 제안 메일 초안 생성 → 시트(제목/본문) 저장 + 표시(편집 가능)
  3. 후속 대응 : 받은 답장을 수락/거절/문의로 분류 → 후속 초안 생성 → 편집 후 발송

설정 기본값은 이 파일 상단의 DEFAULTS 에서 가져오며, UI 에서 바꾼 값은 실행 중인 세션에만 적용된다.

  실행:  python automail.py   →  http://localhost:5002
"""
import json
import os
import re
import time

from flask import Flask, jsonify, request

from agents.config import get_llm
from agents.google_clients import (authenticate, fetch_latest_reply,
                                   fetch_latest_reply_meta, get_sender_info,
                                   read_column, send_email, write_column)
from agents.graph import (build_reply_graph, build_search_graph,
                          build_write_graph)
from agents.reply_agent import classify_reply

app = Flask(__name__)

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
CONFIG_DIR = "config"   # 설정 스냅샷 저장 폴더 (타임스탬프 파일명, 최근 것을 복원)


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


cfg = {k: DEFAULTS.get(k, "") for k in CFG_KEYS}
cfg.update({k: v for k, v in _load_latest_config().items() if k in CFG_KEYS and v})
if not cfg.get("campus"):
    cfg["campus"] = "자연과학캠퍼스"
if not (cfg.get("attachment_path") and os.path.exists(cfg["attachment_path"])):
    cfg["attachment_path"] = ""

pipeline = {"rows": []}   # 행 정렬을 유지한 업체 상태 (검색→작성 누적)

_creds = None
_llm = None
_sender = None


def creds():
    global _creds
    if _creds is None:
        _creds = authenticate()
    return _creds


def llm():
    global _llm
    if _llm is None:
        _llm = get_llm()
    return _llm


def sender_info():
    global _sender
    if _sender is None:
        _sender = get_sender_info(creds())
    return _sender


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


def subject_range():
    """제목 열 = 이메일 열의 오른쪽 한 칸."""
    return _shift_column(cfg["email_range"], 1)


def body_range():
    """본문 열 = 이메일 열의 오른쪽 두 칸."""
    return _shift_column(cfg["email_range"], 2)


def read_aligned():
    """업체명/힌트/이메일/제목/본문을 행 정렬해 dict 리스트로 반환."""
    c = creds()
    ranges = [cfg["name_range"], cfg["hint_range"], cfg["email_range"],
              subject_range(), body_range()]
    cols = [read_column(c, cfg["spreadsheet_id"], rng) for rng in ranges]
    n = max((len(x) for x in cols), default=0)
    cols = [(x + [""] * n)[:n] for x in cols]
    rows = []
    for name, hint, email, subject, body in zip(*cols):
        rows.append({"name": name, "hint": hint, "email": email,
                     "subject": subject, "body": body})
    return rows


def _reply_subject(subject):
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _label():
    """설정의 행사명/이름으로 Gmail 라벨 문자열('{행사명}/{이름}')을 만든다."""
    ev = (cfg.get("event_name") or "").strip()
    nm = (cfg.get("writer_name") or "").strip()[1:]
    if ev and nm:
        return f"{ev}/{nm}"
    return ev or nm or None


# ---------- 페이지 ----------

@app.route("/")
def index():
    return HTML


# ---------- 설정 ----------

@app.route("/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        d = request.get_json(force=True) or {}
        for k in CFG_KEYS:
            if k == "attachment_path":
                continue
            v = (d.get(k) or "").strip()
            if v:
                cfg[k] = v
        os.makedirs(CONFIG_DIR, exist_ok=True)
        snap = os.path.join(CONFIG_DIR, time.strftime("%Y%m%d_%H%M%S") + ".json")
        with open(snap, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})
    out = dict(cfg)
    out["attachment_name"] = os.path.basename(cfg["attachment_path"]) if cfg["attachment_path"] else ""
    out["has_saved"] = bool(_load_latest_config())
    return jsonify(out)


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "파일이 없습니다."}), 400
    os.makedirs("uploads", exist_ok=True)
    name = os.path.basename(f.filename)
    path = os.path.join("uploads", name)
    f.save(path)
    cfg["attachment_path"] = os.path.abspath(path)
    return jsonify({"ok": True, "filename": name})


# ---------- 1. 검색 ----------

@app.route("/search", methods=["POST"])
def search():
    limit = int((request.get_json(force=True) or {}).get("limit") or 0)
    try:
        rows = read_aligned()
        targets, considered = [], 0
        for i, r in enumerate(rows):
            if not r["name"]:
                continue
            if limit and considered >= limit:
                break
            considered += 1
            targets.append((i, r))
        # LangGraph 검색 그래프로 오케스트레이션
        result = build_search_graph(llm()).invoke({"companies": [r for _, r in targets]})
        for (i, _), c in zip(targets, result["companies"]):
            rows[i].update(c)
        write_column(creds(), cfg["spreadsheet_id"], cfg["email_range"],
                     [r.get("email", "") for r in rows])
        pipeline["rows"] = rows
        out = [{"i": i, "name": rows[i]["name"], "email": rows[i].get("email", ""),
                "tier": rows[i].get("tier", ""), "hint": rows[i].get("hint", ""),
                "query": rows[i].get("query", ""), "verified": bool(rows[i].get("verified")),
                "reason": rows[i].get("verify_reason", ""), "info": rows[i].get("info", "")}
               for i, _ in targets]
        return jsonify({"results": out})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------- 2. 초안 작성 ----------

@app.route("/write", methods=["POST"])
def write():
    limit = int((request.get_json(force=True) or {}).get("limit") or 0)
    try:
        rows = pipeline["rows"] or read_aligned()
        sender_name, _ = sender_info()
        targets, considered = [], 0
        for i, r in enumerate(rows):
            if not r["name"]:
                continue
            if limit and considered >= limit:
                break
            considered += 1
            if not r.get("email"):
                continue
            targets.append((i, r))
        # LangGraph 작성 그래프로 오케스트레이션 (설정값은 state 로 전달)
        result = build_write_graph(llm()).invoke({
            "companies": [r for _, r in targets],
            "sponsor_items": cfg["sponsor_items"],
            "sender_name": sender_name,
            "campus": cfg.get("campus", ""),
            "writer_name": cfg.get("writer_name", ""),
            "event_name": cfg.get("event_name", ""),
            "writer_phone": cfg.get("writer_phone", ""),
            "event_date": cfg.get("event_date", ""),
        })
        for (i, _), c in zip(targets, result["companies"]):
            rows[i].update(c)
        write_column(creds(), cfg["spreadsheet_id"], subject_range(),
                     [r.get("subject", "") for r in rows])
        write_column(creds(), cfg["spreadsheet_id"], body_range(),
                     [r.get("body", "") for r in rows])
        pipeline["rows"] = rows
        out = [{"i": i, "name": rows[i]["name"], "subject": rows[i].get("subject", ""),
                "body": rows[i].get("body", "")}
               for i, _ in targets if rows[i].get("subject")]
        return jsonify({"results": out})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/write/save", methods=["POST"])
def write_save():
    d = request.get_json(force=True)
    i = int(d["index"])
    try:
        rows = pipeline["rows"]
        if not rows:
            return jsonify({"error": "먼저 검색 또는 초안 작성을 실행하세요."}), 400
        rows[i]["subject"] = (d.get("subject") or "").strip()
        rows[i]["body"] = (d.get("body") or "").strip()
        write_column(creds(), cfg["spreadsheet_id"], subject_range(),
                     [r.get("subject", "") for r in rows])
        write_column(creds(), cfg["spreadsheet_id"], body_range(),
                     [r.get("body", "") for r in rows])
        return jsonify({"ok": True})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/proposal/send", methods=["POST"])
def proposal_send():
    """작성한 제안 초안을 실제(또는 테스트) 수신자에게 발송. PDF 첨부 + 라벨 적용."""
    d = request.get_json(force=True)
    i = int(d["index"])
    test_email = (d.get("test_email") or "").strip()
    subject = (d.get("subject") or "").strip()
    body = (d.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"error": "제목과 본문을 채워주세요."}), 400
    try:
        rows = pipeline["rows"]
        if not rows:
            return jsonify({"error": "먼저 검색 또는 초안 작성을 실행하세요."}), 400
        r = rows[i]
        recipient = test_email or r.get("email", "")
        if not recipient:
            return jsonify({"error": "수신 이메일이 없습니다."}), 400
        sender_name, sender_email = sender_info()
        attach = cfg.get("attachment_path") or None
        result = send_email(creds(), recipient, subject, body,
                            sender_name=sender_name, sender_email=sender_email,
                            attachment_path=attach, label=_label())
        return jsonify({"ok": True, "to": recipient, "id": result.get("id")})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ---------- 3. 후속 대응 ----------

def load_companies():
    c = creds()
    names = read_column(c, cfg["spreadsheet_id"], cfg["name_range"])
    emails = read_column(c, cfg["spreadsheet_id"], cfg["email_range"])
    subjects = read_column(c, cfg["spreadsheet_id"], subject_range())
    bodies = read_column(c, cfg["spreadsheet_id"], body_range())
    companies = []
    for name, email, subject, body in zip(names, emails, subjects, bodies):
        if email and subject and body:
            companies.append({"name": name, "email": email,
                              "subject": subject, "body": body})
    return companies


@app.route("/companies")
def companies():
    try:
        return jsonify({"companies": [{"name": c["name"]} for c in load_companies()]})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/classify_all", methods=["POST"])
def classify_all():
    """전체 업체의 답장을 조회·분류해 상태 목록을 반환 (배지 일괄 표시용)."""
    test_email = ((request.get_json(force=True) or {}).get("test_email") or "").strip()
    try:
        comps = load_companies()
        sender_name, _ = sender_info()
        if test_email:
            for c in comps:
                c["email"] = test_email
        # LangGraph 답장 그래프로 오케스트레이션
        result = build_reply_graph(creds(), llm()).invoke(
            {"companies": comps, "sender_name": sender_name})
        out = [{"i": i, "status": c.get("reply_status", "no_reply")}
               for i, c in enumerate(result["companies"])]
        return jsonify({"results": out})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/load", methods=["POST"])
def load():
    data = request.get_json(force=True)
    idx = int(data["index"])
    test_email = (data.get("test_email") or "").strip()
    try:
        c = load_companies()[idx]
        from_email = test_email or c["email"]
        reply_text = fetch_latest_reply(creds(), from_email, subject_query=c["name"])
        reply_subject = _reply_subject(c["subject"])
        if not reply_text:
            return jsonify({"name": c["name"], "reply_status": "no_reply",
                            "reply_text": "", "follow_up": "", "reply_subject": reply_subject})
        sender_name, _ = sender_info()
        decision = classify_reply(c["name"], reply_text, sender_name, llm())
        return jsonify({"name": c["name"], "reply_status": decision["reply_status"],
                        "reply_text": reply_text, "follow_up": decision["follow_up"],
                        "reply_subject": reply_subject})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.route("/send", methods=["POST"])
def send():
    data = request.get_json(force=True)
    idx = int(data["index"])
    test_email = (data.get("test_email") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"error": "제목과 본문을 모두 채워주세요."}), 400
    try:
        c = load_companies()[idx]
        recipient = test_email or c["email"]
        sender_name, sender_email = sender_info()
        # 원본 답장 스레드에 '답장'으로 묶기
        meta = fetch_latest_reply_meta(creds(), recipient, subject_query=c["name"]) or {}
        result = send_email(creds(), recipient, subject, body,
                            sender_name=sender_name, sender_email=sender_email,
                            label=_label(), thread_id=meta.get("thread_id"),
                            in_reply_to=meta.get("message_id"))
        return jsonify({"ok": True, "to": recipient, "id": result.get("id")})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>협찬 메일 자동화 대시보드</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif; color:#1a1a1a; background:#f5f6f8; }
  header { background:#2b2d42; color:#fff; padding:14px 24px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  .testbar { margin-left:auto; display:flex; align-items:center; gap:10px; font-size:13px; }
  .testbar input[type=email]{ padding:6px 9px; border:1px solid #555; border-radius:6px; width:220px; font-size:13px; background:#1f2133; color:#fff; }
  header h1 { font-size:18px; margin:0; font-weight:700; }
  .config { background:#fff; border-bottom:1px solid #e3e5e9; }
  .config-head { padding:12px 24px; cursor:pointer; font-weight:700; font-size:14px; display:flex; align-items:center; gap:8px; }
  .config-head .st { font-weight:400; font-size:12px; color:#8a909b; margin-left:6px; }
  .config-body { padding:6px 24px 20px; }
  .config-body.hide { display:none; }
  .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  label.f { display:flex; flex-direction:column; font-size:12px; color:#6b727d; gap:4px; }
  label.f input, label.f textarea { padding:8px 10px; border:1px solid #ccc; border-radius:7px; font-size:13px; font-family:inherit; }
  label.full { display:flex; flex-direction:column; font-size:12px; color:#6b727d; gap:4px; margin-top:12px; }
  label.full textarea { min-height:60px; resize:vertical; padding:8px 10px; border:1px solid #ccc; border-radius:7px; font-size:13px; font-family:inherit; }
  .pdfrow { display:flex; align-items:center; gap:12px; margin-top:14px; }
  .pdfbtn { background:#eef1ff; color:#3a4ad0; border:1px solid #d4dafc; padding:8px 14px; border-radius:7px; font-size:13px; cursor:pointer; font-weight:600; }
  .pdfname { font-size:13px; color:#6b727d; }
  button.primary { background:#4361ee; color:#fff; border:none; padding:9px 18px; border-radius:7px; font-size:13px; font-weight:600; cursor:pointer; }
  button.primary:disabled { background:#aab2d8; cursor:default; }
  button.ghost { background:#fff; color:#4a4f5a; border:1px solid #ccc; padding:8px 14px; border-radius:7px; font-size:13px; cursor:pointer; }
  .save-row { margin-top:16px; display:flex; align-items:center; gap:12px; }
  .tabs { display:flex; gap:4px; padding:0 24px; background:#f5f6f8; border-bottom:1px solid #e3e5e9; }
  .tab { padding:13px 20px; cursor:pointer; font-size:14px; font-weight:600; color:#7a808a; border-bottom:3px solid transparent; }
  .tab.active { color:#2b2d42; border-bottom-color:#4361ee; }
  .panel { display:none; padding:22px 24px; }
  .panel.active { display:block; }
  .bar { display:flex; align-items:center; gap:14px; margin-bottom:18px; flex-wrap:wrap; }
  .bar label { font-size:13px; color:#4a4f5a; }
  .bar input[type=text], .bar input[type=email], .bar input.num { padding:7px 9px; border:1px solid #ccc; border-radius:7px; font-size:13px; }
  .msg { font-size:13px; }
  .msg.ok { color:#1c7c3a; }
  .msg.err { color:#c0322b; }
  .card { background:#fff; border:1px solid #e3e5e9; border-radius:11px; padding:16px 18px; margin-bottom:14px; }
  .card h3 { margin:0 0 8px; font-size:15px; display:flex; align-items:center; gap:9px; }
  .badge { font-size:11px; padding:2px 9px; border-radius:10px; font-weight:600; white-space:nowrap; }
  .b-ok{ background:#e3f6e8; color:#1c7c3a; } .b-no{ background:#fde4e4; color:#c0322b; }
  .b-accepted{ background:#e3f6e8; color:#1c7c3a; } .b-rejected{ background:#fde4e4; color:#c0322b; }
  .b-question{ background:#fff2d8; color:#9a6b00; } .b-no_reply{ background:#eceef1; color:#7a808a; }
  .b-loading{ background:#eef1ff; color:#4a5bd0; }
  .meta { font-size:12.5px; color:#6b727d; line-height:1.5; }
  .meta b { color:#3a3f49; }
  input.subj { width:100%; padding:9px 11px; border:1px solid #ccc; border-radius:8px; font-size:14px; margin-bottom:8px; }
  textarea.body { width:100%; min-height:150px; padding:11px 13px; border:1px solid #ccc; border-radius:8px; font-size:14px; line-height:1.6; resize:vertical; font-family:inherit; }
  .reply-wrap { display:flex; border:1px solid #e3e5e9; border-radius:11px; overflow:hidden; height:560px; background:#fff; }
  .side { width:280px; border-right:1px solid #e3e5e9; overflow-y:auto; }
  .side .item { padding:12px 15px; border-bottom:1px solid #f0f1f3; cursor:pointer; display:flex; align-items:center; gap:8px; }
  .side .item:hover { background:#f7f8fa; }
  .side .item.active { background:#eef1ff; border-left:3px solid #4361ee; padding-left:12px; }
  .side .item .nm { flex:1; font-size:13.5px; line-height:1.35; }
  .rmain { flex:1; overflow-y:auto; padding:20px 22px; }
  .empty { color:#9098a3; text-align:center; margin-top:70px; font-size:15px; }
  .label { font-size:12px; color:#6b727d; margin:14px 0 6px; font-weight:600; }
  .reply { background:#f7f8fa; border:1px solid #e7e9ed; border-radius:8px; padding:12px 14px; font-size:14px; white-space:pre-wrap; line-height:1.55; }
  .hint { font-size:12px; color:#9098a3; margin-top:10px; }
</style>
</head>
<body>
<header>
  <h1>협찬 메일 자동화 대시보드</h1>
  <div class="testbar">
    <label><input type="checkbox" id="testToggle" checked> 테스트 모드</label>
    <input type="email" id="testEmail" value="seohwan3549@gmail.com" placeholder="테스트 수신 주소">
  </div>
</header>

<div class="config">
  <div class="config-head" onclick="toggleConfig()"><span id="cfgArrow">▾</span> 설정 / 첨부 </div>
  <div class="config-body" id="cfgBody">
    <div class="grid">
      <label class="f">Spreadsheet ID<input type="text" id="c_spreadsheet_id" placeholder="예: 1AbCdEf... (URL의 /d/ 뒤 문자열)"></label>
      <label class="f">업체명 범위<input type="text" id="c_name_range" placeholder="예: 실험용!B5:B13"></label>
      <label class="f">힌트 범위<input type="text" id="c_hint_range" placeholder="예: 실험용!C5:C13"></label>
      <label class="f">이메일 범위<input type="text" id="c_email_range" placeholder="예: 실험용!F5:F13"></label>
      <label class="f" style="grid-column:span 2">제안 내용<textarea id="c_sponsor_items" style="min-height:38px;resize:vertical" placeholder="예: 제안 내용: 협찬 가능한 제품 500개, 홍보 효과: 부스 배너 노출·공식 SNS 홍보"></textarea></label>
    </div>
    <div class="pdfrow" style="gap:20px;flex-wrap:wrap;align-items:flex-start">
      <label class="f" style="min-width:180px">행사명<input type="text" id="c_event_name" placeholder="예: 문행대동제"></label>
      <label class="f" style="min-width:150px">행사 일자<input type="date" id="c_event_date"></label>
      <label class="f" style="min-width:150px">담당자 이름<input type="text" id="c_writer_name" placeholder="예: 김서환"></label>
      <label class="f" style="min-width:170px">연락처(Mobile)<input type="text" id="c_writer_phone" placeholder="예: 010-1234-5678"></label>
      <span style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:#6b727d">캠퍼스
        <span style="display:flex;gap:14px;font-size:13px;color:#1a1a1a;padding-top:3px">
          <label style="cursor:pointer"><input type="radio" name="campus" value="자연과학캠퍼스"> 자연과학캠퍼스</label>
          <label style="cursor:pointer"><input type="radio" name="campus" value="인문사회과학캠퍼스"> 인문사회과학캠퍼스</label>
        </span>
      </span>
    </div>
    <div class="pdfrow">
      <label class="pdfbtn">제안서 PDF 첨부<input type="file" id="c_pdf" accept="application/pdf" style="display:none"></label>
      <span class="pdfname" id="pdfName">첨부 없음</span>
    </div>
    <div class="save-row">
      <button class="primary" onclick="saveConfig()">설정 저장</button>
      <span class="msg" id="cfgMsg"></span>
    </div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-tab="search" onclick="switchTab('search')">1 · 검색</div>
  <div class="tab" data-tab="write" onclick="switchTab('write')">2 · 초안 작성</div>
  <div class="tab" data-tab="reply" onclick="switchTab('reply')">3 · 후속 대응</div>
</div>

<div class="panel active" id="panel-search">
  <div class="bar">
    <label>처리 개수 (빈칸=전체) <input class="num" id="searchLimit" value="3" style="width:64px"></label>
    <button class="primary" id="searchBtn" onclick="runSearch()">검색 실행</button>
    <span class="msg" id="searchMsg"></span>
  </div>
  <div id="searchResults"></div>
</div>

<div class="panel" id="panel-write">
  <div class="bar">
    <label>처리 개수 (빈칸=전체) <input class="num" id="writeLimit" value="3" style="width:64px"></label>
    <button class="primary" id="writeBtn" onclick="runWrite()">초안 작성 실행</button>
    <button class="ghost" id="sendAllBtn" onclick="sendAllProposals()">표시된 초안 전체 발송</button>
    <span class="msg" id="writeMsg"></span>
  </div>
  <div id="writeResults"></div>
</div>

<div class="panel" id="panel-reply">
  <div class="bar">
    <button class="ghost" onclick="loadCompanies()">목록 새로고침</button>
    <button class="primary" onclick="classifyAll()">전체 분류</button>
  </div>
  <div class="reply-wrap">
    <div class="side" id="side"></div>
    <div class="rmain" id="rmain"><div class="empty">왼쪽에서 업체를 선택하세요.</div></div>
  </div>
</div>

<script>
function esc(s){ return (s||'').replace(/[&<>]/g, function(m){ return ({'&':'&amp;','<':'&lt;','>':'&gt;'})[m]; }); }
function escAttr(s){ return (s||'').replace(/[&<>"]/g, function(m){ return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[m]; }); }
function statusLabel(s){ return ({accepted:'수락', rejected:'거절', question:'문의', no_reply:'무응답', loading:'분류중…'})[s] || ''; }
function tierBadge(t){ if(t==='HIGH') return '<span class="badge b-ok">검증 O</span>'; if(t==='REVIEW') return '<span class="badge b-question">검토 필요</span>'; return '<span class="badge b-no">미발견</span>'; }

// ---- 탭 ----
let replyLoaded = false;
function switchTab(name){
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.toggle('active', t.dataset.tab===name); });
  document.querySelectorAll('.panel').forEach(function(p){ p.classList.toggle('active', p.id==='panel-'+name); });
  if (name==='reply' && !replyLoaded){ replyLoaded = true; loadCompanies(); }
}
function toggleConfig(){
  const b = document.getElementById('cfgBody');
  b.classList.toggle('hide');
  document.getElementById('cfgArrow').textContent = b.classList.contains('hide') ? '▸' : '▾';
}

// ---- 설정 ----
const CFG_FIELDS = ['spreadsheet_id','name_range','hint_range','email_range','sponsor_items','event_name','event_date','writer_name','writer_phone'];
async function loadConfig(){
  try {
    const d = await (await fetch('/config')).json();
    if (d.has_saved){
      CFG_FIELDS.forEach(function(k){ const el = document.getElementById('c_'+k); if (el && d[k]) el.value = d[k]; });
    }
    const camp = document.querySelector('input[name="campus"][value="'+(d.campus||'자연과학캠퍼스')+'"]'); if(camp) camp.checked = true;
    document.getElementById('pdfName').textContent = d.attachment_name ? ('첨부: '+d.attachment_name) : '첨부 없음';
    const st1=document.getElementById('cfgStatus'); if(st1) st1.textContent = d.spreadsheet_id ? ('시트 '+d.spreadsheet_id.slice(0,8)+'…') : '';
  } catch(e){}
}
async function saveConfig(){
  const msg = document.getElementById('cfgMsg');
  const payload = {};
  CFG_FIELDS.forEach(function(k){ payload[k] = document.getElementById('c_'+k).value.trim(); });
  const camp = document.querySelector('input[name="campus"]:checked');
  payload.campus = camp ? camp.value : '자연과학캠퍼스';
  msg.className='msg'; msg.textContent='저장 중…';
  try {
    const d = await (await fetch('/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})).json();
    if (d.error){ msg.className='msg err'; msg.textContent='실패: '+d.error; return; }
    msg.className='msg ok'; msg.textContent='저장됨';
    const st2=document.getElementById('cfgStatus'); if(st2) st2.textContent = payload.spreadsheet_id ? ('시트 '+payload.spreadsheet_id.slice(0,8)+'…') : '';
  } catch(e){ msg.className='msg err'; msg.textContent='실패: '+e; }
}
document.getElementById('c_pdf').onchange = async function(ev){
  const f = ev.target.files[0];
  if (!f) return;
  const pn = document.getElementById('pdfName');
  pn.textContent = '업로드 중…';
  const fd = new FormData(); fd.append('file', f);
  try {
    const d = await (await fetch('/upload', {method:'POST', body:fd})).json();
    pn.textContent = d.error ? ('오류: '+d.error) : ('첨부: '+d.filename);
  } catch(e){ pn.textContent = '오류: '+e; }
};

// ---- 1. 검색 ----
async function runSearch(){
  const btn = document.getElementById('searchBtn');
  const msg = document.getElementById('searchMsg');
  const out = document.getElementById('searchResults');
  const limit = document.getElementById('searchLimit').value.trim();
  btn.disabled=true; msg.className='msg'; msg.textContent='검색 중… (업체당 수 초)';
  try {
    const d = await (await fetch('/search', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({limit:limit})})).json();
    if (d.error){ msg.className='msg err'; msg.textContent='실패: '+d.error; btn.disabled=false; return; }
    msg.className='msg ok'; msg.textContent=d.results.length+'개 처리 완료 (시트 이메일 열 저장됨)';
    out.innerHTML = d.results.map(function(r){
      const q = r.query ? ('<br><b>검색어</b> '+esc(r.query)+(r.hint?'  (힌트 반영됨)':'  (힌트 없음)')) : '';
      return '<div class="card"><h3>'+esc(r.name)+' '+tierBadge(r.tier)+'</h3>'+
        '<div class="meta"><b>이메일</b> '+(esc(r.email)||'(미발견)')+q+'<br>'+
        '<b>근거</b> '+esc(r.reason)+'<br>'+
        '<b>요약</b> '+esc(r.info)+'</div></div>';
    }).join('');
  } catch(e){ msg.className='msg err'; msg.textContent='실패: '+e; }
  btn.disabled=false;
}

// ---- 2. 초안 작성 ----
async function runWrite(){
  const btn = document.getElementById('writeBtn');
  const msg = document.getElementById('writeMsg');
  const out = document.getElementById('writeResults');
  const limit = document.getElementById('writeLimit').value.trim();
  btn.disabled=true; msg.className='msg'; msg.textContent='작성 중… (업체당 수 초)';
  try {
    const d = await (await fetch('/write', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({limit:limit})})).json();
    if (d.error){ msg.className='msg err'; msg.textContent='실패: '+d.error; btn.disabled=false; return; }
    msg.className='msg ok'; msg.textContent=d.results.length+'개 초안 생성 (시트 제목/본문 저장됨)';
    out.innerHTML = d.results.map(function(r){
      return '<div class="card"><h3>'+esc(r.name)+'</h3>'+
        '<input class="subj" id="ws_'+r.i+'" value="'+escAttr(r.subject)+'">'+
        '<textarea class="body" id="wb_'+r.i+'">'+esc(r.body)+'</textarea>'+
        '<div class="save-row"><button class="primary" onclick="saveDraft('+r.i+')">시트에 저장</button>'+
        '<button class="ghost" onclick="sendProposal('+r.i+')">발송</button>'+
        '<span class="msg" id="wm_'+r.i+'"></span></div></div>';
    }).join('');
  } catch(e){ msg.className='msg err'; msg.textContent='실패: '+e; }
  btn.disabled=false;
}
async function saveDraft(i){
  const m = document.getElementById('wm_'+i);
  const subject = document.getElementById('ws_'+i).value.trim();
  const body = document.getElementById('wb_'+i).value.trim();
  m.className='msg'; m.textContent='저장 중…';
  try {
    const d = await (await fetch('/write/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({index:i, subject:subject, body:body})})).json();
    if (d.error){ m.className='msg err'; m.textContent='실패: '+d.error; return; }
    m.className='msg ok'; m.textContent='저장됨';
  } catch(e){ m.className='msg err'; m.textContent='실패: '+e; }
}
async function sendProposal(i){
  const m = document.getElementById('wm_'+i);
  const subject = document.getElementById('ws_'+i).value.trim();
  const body = document.getElementById('wb_'+i).value.trim();
  m.className='msg'; m.textContent='발송 중…';
  try {
    const d = await (await fetch('/proposal/send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({index:i, test_email:testEmail(), subject:subject, body:body})})).json();
    if (d.error){ m.className='msg err'; m.textContent='실패: '+d.error; return false; }
    m.className='msg ok'; m.textContent='발송됨 → '+d.to;
    return true;
  } catch(e){ m.className='msg err'; m.textContent='실패: '+e; return false; }
}
async function sendAllProposals(){
  const ids = Array.from(document.querySelectorAll('#writeResults [id^="ws_"]')).map(function(el){ return parseInt(el.id.slice(3)); });
  if (!ids.length){ return; }
  const t = testEmail();
  const note = t ? ('테스트 주소('+t+')로 '+ids.length+'건 발송합니다.') : ('테스트 모드 꺼짐 — 실제 업체 '+ids.length+'곳에 발송합니다.');
  if (!confirm(note+' 계속할까요?')) return;
  const btn = document.getElementById('sendAllBtn');
  const msg = document.getElementById('writeMsg');
  btn.disabled=true;
  let ok=0, fail=0;
  for (const i of ids){ if (await sendProposal(i)) ok++; else fail++; }
  msg.className = fail ? 'msg err' : 'msg ok'; msg.textContent = '발송 완료: 성공 '+ok+' / 실패 '+fail;
  btn.disabled=false;
}

// ---- 3. 후속 대응 ----
let companies = [];
let current = null;
const sideEl = document.getElementById('side');
const rmainEl = document.getElementById('rmain');
const testToggle = document.getElementById('testToggle');
const testEmailEl = document.getElementById('testEmail');
function testEmail(){ return testToggle.checked ? testEmailEl.value.trim() : ''; }

async function loadCompanies(){
  sideEl.innerHTML = '<div class="item">불러오는 중…</div>';
  try {
    const d = await (await fetch('/companies')).json();
    if (d.error){ sideEl.innerHTML = '<div class="item">오류: '+esc(d.error)+'</div>'; return; }
    companies = d.companies;
    if (!companies.length){ sideEl.innerHTML = '<div class="item">발송 대상이 없습니다. 먼저 초안 작성을 실행하세요.</div>'; return; }
    renderSide();
  } catch(e){ sideEl.innerHTML = '<div class="item">오류: '+e+'</div>'; }
}
async function classifyAll(){
  if (!companies.length){ return; }
  companies.forEach(function(c){ c.status = 'loading'; });
  renderSide();
  try {
    const d = await (await fetch('/classify_all', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({test_email:testEmail()})})).json();
    if (d.error){ companies.forEach(function(c){ if(c.status==='loading') c.status=null; }); renderSide(); return; }
    d.results.forEach(function(r){ if (companies[r.i]) companies[r.i].status = r.status; });
    renderSide();
  } catch(e){ companies.forEach(function(c){ if(c.status==='loading') c.status=null; }); renderSide(); }
}
function renderSide(){
  sideEl.innerHTML = '';
  companies.forEach(function(c, i){
    const div = document.createElement('div');
    div.className = 'item' + (current===i ? ' active' : '');
    let badge = '';
    if (c.status){ badge = '<span class="badge b-'+c.status+'">'+statusLabel(c.status)+'</span>'; }
    div.innerHTML = '<span class="nm">'+esc(c.name)+'</span>'+badge;
    div.onclick = function(){ selectCompany(i); };
    sideEl.appendChild(div);
  });
}
async function selectCompany(i){
  current = i; renderSide();
  rmainEl.innerHTML = '<div class="empty">답장을 가져와 분석하는 중…</div>';
  try {
    const r = await fetch('/load', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({index:i, test_email:testEmail()})});
    const d = await r.json();
    if (d.error){ rmainEl.innerHTML = '<div class="empty">오류: '+esc(d.error)+'</div>'; return; }
    companies[i].status = d.reply_status; renderSide(); renderDetail(d);
  } catch(e){ rmainEl.innerHTML = '<div class="empty">오류: '+e+'</div>'; }
}
function renderDetail(d){
  const hasReply = !!d.reply_text;
  const replyBlock = hasReply
    ? '<div class="label">받은 답장</div><div class="reply">'+esc(d.reply_text)+'</div>'
    : '<div class="label">받은 답장</div><div class="reply" style="color:#9098a3">아직 이 업체에서 온 답장이 없습니다.</div>';
  rmainEl.innerHTML =
    '<h3 style="margin:0 0 4px;font-size:17px">'+esc(d.name)+' <span class="badge b-'+d.reply_status+'">'+statusLabel(d.reply_status)+'</span></h3>'+
    replyBlock+
    '<div class="label">후속 메일 제목</div><input class="subj" id="r_subj" value="'+escAttr(d.reply_subject)+'">'+
    '<div class="label">후속 메일 초안 (수정 가능)</div><textarea class="body" id="r_body">'+esc(d.follow_up)+'</textarea>'+
    '<div class="save-row"><button class="primary" id="r_send"'+(hasReply?'':' disabled')+'>발송</button><span class="msg" id="r_msg"></span></div>'+
    '<div class="hint" id="r_hint"></div>';
  const btn = document.getElementById('r_send');
  if (btn && hasReply) btn.onclick = doSend;
  updateHint();
}
function updateHint(){
  const h = document.getElementById('r_hint');
  if (!h) return;
  const t = testEmail();
  const nm = (current!==null && companies[current]) ? companies[current].name : '';
  h.textContent = t ? ('테스트 모드: '+t+' 로 발송됩니다.') : ('실제 발송: '+nm+' 의 답장 주소로 보냅니다.');
}
testToggle.onchange = updateHint;
testEmailEl.oninput = updateHint;
async function doSend(){
  const btn = document.getElementById('r_send');
  const msg = document.getElementById('r_msg');
  const subject = document.getElementById('r_subj').value.trim();
  const body = document.getElementById('r_body').value.trim();
  btn.disabled=true; msg.className='msg'; msg.textContent='발송 중…';
  try {
    const d = await (await fetch('/send', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({index:current, test_email:testEmail(), subject:subject, body:body})})).json();
    if (d.error){ msg.className='msg err'; msg.textContent='실패: '+d.error; btn.disabled=false; return; }
    msg.className='msg ok'; msg.textContent='발송 완료 → '+d.to;
  } catch(e){ msg.className='msg err'; msg.textContent='실패: '+e; btn.disabled=false; }
}

loadConfig();
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print("  협찬 메일 자동화 대시보드")
    print("  http://localhost:5002")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5002, debug=False)
