"""Automail (독립 실행 웹 UI).

상단 설정 패널에서 스프레드시트 ID·범위·협찬 품목을 입력하고 제안서 PDF를 첨부한 뒤,
세 개의 탭에서 에이전트를 단계별로 실행한다.

  1. 검색      : 시트의 업체명+힌트로 웹 검색 → 이메일 추출/검증 → 시트(이메일 열) 저장 + 표시
  2. 초안 작성 : 업체 정보+협찬 품목으로 제안 메일 초안 생성 → 시트(제목/본문) 저장 + 표시(편집 가능)
  3. 후속 대응 : 받은 답장을 수락/거절/대기로 분류 → 후속 초안 생성 → 편집 후 발송

설정 기본값은 이 파일 상단의 DEFAULTS 에서 가져오며, UI 에서 바꾼 값은 실행 중인 세션에만 적용된다.

  실행:  python automail.py   →  http://localhost:5002
"""
import json
import os
import re
import threading
import time

from flask import Flask, jsonify, request

from agents.config import get_llm
from agents.google_clients import (authenticate, fetch_latest_reply,
                                   fetch_latest_reply_meta, get_sender_info,
                                   read_column, send_email, write_column)
from agents.graph import build_reply_graph
from agents.reply_agent import classify_reply
from agents.supervisor import build_supervisor_graph
from langgraph.types import Command

app = Flask(__name__)

CFG_KEYS = ["spreadsheet_id", "name_range", "hint_range", "email_range",
            "sponsor_items", "event_name", "event_date", "writer_name",
            "writer_phone", "campus", "attachment_path"]
# 기본 설정값 (UI에서 비워두면 이 값이 사용됨). 본인 스프레드시트에 맞게 수정하세요.
DEFAULTS = {
    "spreadsheet_id": "d/ 뒤에 있는 Spreadsheet ID를 입력하세요",
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



# ---------- 4. 자동 실행 (supervisor 멀티 에이전트) ----------

AUTO = {
    "running": False, "done": False, "error": "",
    "log": [], "pending": None, "results": [],
    "indices": [], "rows": [],
    "event": threading.Event(), "resume": None,
}


def _auto_log(msg):
    AUTO["log"].append(str(msg))


def _persist_auto_rows(companies):
    """supervisor 결과를 전체 행에 반영하고 시트(이메일/제목/본문 열)에 저장."""
    rows = AUTO["rows"]
    for local, c in zip(AUTO["indices"], companies):
        rows[local].update(c)
    write_column(creds(), cfg["spreadsheet_id"], cfg["email_range"],
                 [r.get("email", "") for r in rows])
    write_column(creds(), cfg["spreadsheet_id"], subject_range(),
                 [r.get("subject", "") for r in rows])
    write_column(creds(), cfg["spreadsheet_id"], body_range(),
                 [r.get("body", "") for r in rows])


def _auto_worker(limit, test_email, mode="fresh"):
    """백그라운드에서 supervisor 그래프를 실행. interrupt(발송 승인) 시 사람을 기다린다."""
    try:
        rows = read_aligned()
        targets, considered = [], 0
        for i, r in enumerate(rows):
            if not r["name"]:
                continue
            if limit and considered >= limit:
                break
            considered += 1
            targets.append(i)
        if not targets:
            AUTO["error"] = "처리할 업체가 없습니다."
            return
        AUTO["rows"], AUTO["indices"] = rows, targets
        if mode == "skip":
            # 검색 건너뛰기: 시트의 업체명+이메일을 그대로 쓰고 '작성'부터 진행.
            # 검색 시도 횟수를 상한으로 채워 supervisor 가 검색을 고를 수 없게 한다.
            _auto_log("[검색 건너뛰기] 시트의 이메일을 그대로 사용해 초안 작성부터 진행합니다.")
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
                _auto_log("[검색 건너뛰기] 이메일이 없어 제외되는 업체: "
                          + ", ".join(no_email))
        else:
            # 전체 새로 실행: 시트에 있던 이메일/등급/초안을 비우고 검색부터 진행
            _auto_log("[재검색] 시트의 기존 이메일·초안을 무시하고 처음부터 실행합니다.")
            for i in targets:
                for k in ("email", "tier", "verified", "verify_reason", "query",
                          "info", "subject", "body", "sent", "message_id",
                          "reply_status", "follow_up", "search_attempts"):
                    rows[i].pop(k, None)
                rows[i]["email"] = rows[i]["subject"] = rows[i]["body"] = ""
        sender_name, sender_email = sender_info()
        graph = build_supervisor_graph(creds(), llm(), on_event=_auto_log)
        config = {"configurable": {"thread_id": f"auto-{time.time()}"},
                  "recursion_limit": 100}
        state = {
            "companies": [dict(rows[i]) for i in targets],
            "sponsor_items": cfg["sponsor_items"],
            "sender_name": sender_name, "sender_email": sender_email,
            "campus": cfg.get("campus", ""), "writer_name": cfg.get("writer_name", ""),
            "event_name": cfg.get("event_name", ""),
            "writer_phone": cfg.get("writer_phone", ""),
            "event_date": cfg.get("event_date", ""),
            "test_email": test_email,
            "attachment_path": cfg.get("attachment_path", ""),
            "label": _label() or "",
        }
        result = graph.invoke(state, config)
        while "__interrupt__" in result:
            # 발송 승인 대기: 현재까지의 초안을 시트에 저장해 두고 사람을 기다린다
            try:
                _persist_auto_rows(result.get("companies") or [])
            except Exception as e:  # noqa: BLE001 - 시트 저장 실패해도 계속
                _auto_log(f"[경고] 시트 저장 실패: {e}")
            AUTO["pending"] = result["__interrupt__"][0].value
            _auto_log("[관리자] 발송 승인 대기 — 대시보드에서 승인해 주세요.")
            AUTO["event"].clear()
            AUTO["event"].wait()
            AUTO["pending"] = None
            result = graph.invoke(Command(resume=AUTO["resume"]), config)
        comps = result.get("companies", [])
        try:
            _persist_auto_rows(comps)
        except Exception as e:  # noqa: BLE001
            _auto_log(f"[경고] 시트 저장 실패: {e}")
        AUTO["results"] = comps
        AUTO["done"] = True
        _auto_log("[완료] 자동 실행 종료")
    except Exception as e:  # noqa: BLE001
        AUTO["error"] = str(e)
        _auto_log(f"[오류] {e}")
    finally:
        AUTO["running"] = False


@app.route("/auto/start", methods=["POST"])
def auto_start():
    d = request.get_json(force=True) or {}
    if AUTO["running"]:
        return jsonify({"error": "이미 실행 중입니다."}), 409
    AUTO.update({"running": True, "done": False, "error": "", "log": [],
                 "pending": None, "results": [], "resume": None})
    limit = int(d.get("limit") or 0)
    test_email = (d.get("test_email") or "").strip()
    mode = "skip" if d.get("mode") == "skip" else "fresh"
    threading.Thread(target=_auto_worker, args=(limit, test_email, mode),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/auto/status")
def auto_status():
    return jsonify({
        "running": AUTO["running"], "done": AUTO["done"], "error": AUTO["error"],
        "log": AUTO["log"], "pending": AUTO["pending"],
        "results": [{"name": c.get("name", ""), "email": c.get("email", ""),
                     "tier": c.get("tier", ""), "sent": bool(c.get("sent")),
                     "skipped": bool(c.get("skipped")),
                     "reply_status": c.get("reply_status", ""),
                     "follow_up": c.get("follow_up", "")}
                    for c in AUTO["results"]],
    })


@app.route("/auto/approve", methods=["POST"])
def auto_approve():
    d = request.get_json(force=True) or {}
    if not AUTO["running"] or not AUTO["pending"]:
        return jsonify({"error": "승인 대기 중인 작업이 없습니다."}), 400
    AUTO["resume"] = {"approved": d.get("approved") or []}
    AUTO["event"].set()
    return jsonify({"ok": True})


HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Automail</title>
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
  .tab { padding:13px 20px; cursor:pointer; font-size:16px; font-weight:600; color:#7a808a; border-bottom:3px solid transparent; }
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
  <h1>Automail</h1>
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
  <div class="tab active" data-tab="auto" onclick="switchTab('auto')">자동 실행 (에이전트)</div>
  <div class="tab" data-tab="reply" onclick="switchTab('reply')">후속 대응</div>
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

<div class="panel active" id="panel-auto">
  <div class="bar">
    <label>처리 개수 (빈칸=전체) <input class="num" id="autoLimit" value="3" style="width:64px"></label>
    <button class="primary" id="autoBtn" onclick="startAuto('fresh')">자동 실행 시작</button>
    <button class="ghost" id="autoFreshBtn" onclick="startAuto('skip')">재검색 건너뛰기</button>
    <span class="msg" id="autoMsg"></span>
  </div>
  <div class="meta" style="margin-bottom:12px"><b>자동 실행 시작</b>: 시트의 기존 이메일·초안을 무시하고 검색 → 초안 작성 → (사람 승인) → 발송 → 답장 확인을 처음부터 진행합니다. <b>재검색 건너뛰기</b>: 시트에 기재된 업체명·이메일을 그대로 사용해 초안 작성부터 진행합니다. 두 경우 모두 발송 전에는 반드시 아래에서 승인해야 합니다.</div>
  <div id="autoApproval"></div>
  <div class="label">진행 로그</div>
  <pre id="autoLog" style="background:#1f2133;color:#d7dbe8;border-radius:9px;padding:14px 16px;font-size:12.5px;line-height:1.55;max-height:320px;overflow-y:auto;white-space:pre-wrap"></pre>
  <div id="autoResults"></div>
</div>

<script>
function esc(s){ return (s||'').replace(/[&<>]/g, function(m){ return ({'&':'&amp;','<':'&lt;','>':'&gt;'})[m]; }); }
function escAttr(s){ return (s||'').replace(/[&<>"]/g, function(m){ return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[m]; }); }
function statusLabel(s){ return ({accepted:'수락', rejected:'거절', question:'대기', no_reply:'무응답', loading:'분류중…'})[s] || ''; }
function tierBadge(t){ if(t==='HIGH') return '<span class="badge b-ok">검증 O</span>'; if(t==='REVIEW') return '<span class="badge b-question">검토 필요</span>'; return '<span class="badge b-no">미발견</span>'; }
function emailBadge(tier, email){ if(tier) return tierBadge(tier); if(email) return '<span class="badge b-no_reply">시트 입력·미검증</span>'; return tierBadge(''); }

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


// ---- 4. 자동 실행 (supervisor) ----
let autoTimer = null;
function setAutoButtons(disabled){
  document.getElementById('autoBtn').disabled = disabled;
  document.getElementById('autoFreshBtn').disabled = disabled;
}
async function startAuto(mode){
  const msg = document.getElementById('autoMsg');
  const limit = document.getElementById('autoLimit').value.trim();
  const t = testEmail();
  const note = t ? ('테스트 주소('+t+')로 발송됩니다.') : '테스트 모드 꺼짐 — 실제 업체에 발송될 수 있습니다!';
  const head = (mode === 'skip')
    ? '시트에 기재된 이메일을 그대로 사용해 초안 작성부터 진행합니다. '
    : '시트의 기존 이메일·초안을 무시하고 처음부터 재검색·재작성합니다. ';
  if (!confirm(head+note+' 계속할까요?')) return;
  setAutoButtons(true); msg.className='msg'; msg.textContent='실행 중…';
  document.getElementById('autoResults').innerHTML='';
  const box = document.getElementById('autoApproval'); box.dataset.shown='0'; box.innerHTML='';
  try {
    const d = await (await fetch('/auto/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({limit:limit, test_email:t, mode:mode})})).json();
    if (d.error){ msg.className='msg err'; msg.textContent=d.error; setAutoButtons(false); return; }
    autoTimer = setInterval(refreshAuto, 1500);
  } catch(e){ msg.className='msg err'; msg.textContent='실패: '+e; setAutoButtons(false); }
}
async function refreshAuto(){
  try {
    const d = await (await fetch('/auto/status')).json();
    document.getElementById('autoLog').textContent = d.log.join(String.fromCharCode(10));
    const box = document.getElementById('autoApproval');
    if (d.pending){ renderApproval(d.pending); }
    else { box.dataset.shown='0'; box.innerHTML=''; }
    if (!d.running){
      clearInterval(autoTimer); autoTimer=null;
      setAutoButtons(false);
      const msg = document.getElementById('autoMsg');
      if (d.error){ msg.className='msg err'; msg.textContent='오류: '+d.error; }
      else { msg.className='msg ok'; msg.textContent='완료'; }
      renderAutoResults(d.results);
    }
  } catch(e){}
}
function renderApproval(p){
  const box = document.getElementById('autoApproval');
  if (box.dataset.shown === '1') return;   // 편집 중인 승인 화면 보존
  box.dataset.shown = '1';
  box.dataset.ids = JSON.stringify(p.drafts.map(function(d){ return d.i; }));
  const note = p.test_email ? ('테스트 모드: '+p.test_email+' 로 발송됩니다.') : '실제 업체 주소로 발송됩니다!';
  box.innerHTML = '<div class="card" style="border-color:#f0c36d;background:#fffbf0">'+
    '<h3>발송 승인 대기 <span class="badge b-question">사람 확인 필요</span></h3>'+
    '<div class="meta">'+esc(note)+' 발송할 업체를 선택하고 필요하면 수정한 뒤 승인하세요.</div>'+
    p.drafts.map(function(d){
      return '<div class="card" style="margin:10px 0 0">'+
        '<h3><label style="cursor:pointer"><input type="checkbox" id="ap_'+d.i+'" checked> '+esc(d.name)+'</label> '+emailBadge(d.tier, d.email)+'</h3>'+
        '<div class="meta"><b>수신</b> '+esc(d.email)+'</div>'+
        '<input class="subj" id="as_'+d.i+'" value="'+escAttr(d.subject)+'">'+
        '<textarea class="body" id="ab_'+d.i+'">'+esc(d.body)+'</textarea></div>';
    }).join('')+
    '<div class="save-row"><button class="primary" onclick="submitApproval(false)">선택한 업체 발송 승인</button>'+
    '<button class="ghost" onclick="submitApproval(true)">발송 건너뛰기</button>'+
    '<span class="msg" id="apMsg"></span></div></div>';
}
async function submitApproval(skipAll){
  const box = document.getElementById('autoApproval');
  const ids = JSON.parse(box.dataset.ids || '[]');
  const approved = [];
  if (!skipAll){
    ids.forEach(function(i){
      const cb = document.getElementById('ap_'+i);
      if (cb && cb.checked){
        approved.push({i:i, subject:document.getElementById('as_'+i).value.trim(), body:document.getElementById('ab_'+i).value.trim()});
      }
    });
  }
  const m = document.getElementById('apMsg');
  m.className='msg'; m.textContent='전송 중…';
  try {
    const d = await (await fetch('/auto/approve', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({approved:approved})})).json();
    if (d.error){ m.className='msg err'; m.textContent=d.error; return; }
    box.dataset.shown='0'; box.innerHTML='';
  } catch(e){ m.className='msg err'; m.textContent='실패: '+e; }
}
function renderAutoResults(results){
  if (!results || !results.length) return;
  document.getElementById('autoResults').innerHTML =
    '<div class="label">결과 요약</div>'+
    results.map(function(c){
      const sent = c.sent ? '<span class="badge b-ok">발송됨</span>' : (c.skipped ? '<span class="badge b-no_reply">건너뜀</span>' : '<span class="badge b-no">미발송</span>');
      const rep = c.reply_status ? (' <span class="badge b-'+c.reply_status+'">'+statusLabel(c.reply_status)+'</span>') : '';
      const fu = c.follow_up ? ('<div class="label">후속 초안</div><div class="reply">'+esc(c.follow_up)+'</div>') : '';
      return '<div class="card"><h3>'+esc(c.name)+' '+emailBadge(c.tier, c.email)+' '+sent+rep+'</h3>'+
        '<div class="meta"><b>이메일</b> '+(esc(c.email)||'(미발견)')+'</div>'+fu+'</div>';
    }).join('');
}

loadConfig();
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print("  Automail")
    print("  http://localhost:5002")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5002, debug=False)
