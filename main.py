"""
멀티 에이전트 협찬 메일 자동화 — CLI 프로토타입
검색 → 작성 → 발송 → 답장 을 LangGraph 로 오케스트레이션한다.

워크플로우는 2단계다 (초안을 시트에 저장해두고 검토 후 발송):
  prepare : 검색 + 작성 → 이메일/제목/본문을 구글 시트에 저장 (발송 안 함)
  send    : 시트에서 초안을 읽어 Gmail 발송 + 답장 분류
  inspect : 업체 하나의 검색 발견 로직을 그대로 보여줌 (디버그)

[준비]
  1. pip install -r requirements.txt
  2. .env 에 GOOGLE_API_KEY, TAVILY_API_KEY 입력
  3. credentials.json 을 이 파일과 같은 폴더에 두기 (Sheets/Gmail OAuth)
  4. 아래 CONFIG 를 본인 스프레드시트에 맞게 수정

시트 구성(예): B=업체명, C=힌트(업종/키워드), F=이메일, G=제목, H=본문
  힌트 열(C)에 "초콜릿", "광고대행사" 같은 키워드를 적으면 동명이의 검색 오류가 줄어든다.

[실행]
  python main.py prepare --limit 3      # 앞 3개만 검색+작성 후 시트 저장 (테스트 권장)
  python main.py prepare                # 전체 검색+작성 후 시트 저장
  python main.py send --limit 3         # 시트의 앞 3개 초안을 실제 발송 + 답장 분류
  python main.py inspect "갤러" --hint 초콜릿   # '갤러(초콜릿)' 검색 발견 로직 확인
"""
import argparse

from agents.config import get_llm
from agents.google_clients import (authenticate, get_sender_info,
                                   read_column, write_column)
from agents.graph import build_prepare_graph, build_send_graph
from agents.search_agent import debug_search

# ===== 본인 환경에 맞게 수정 =====
CONFIG = {
    "spreadsheet_id": "16BkIPzlETSdzbMtENtqgr0eBi0BO14UkJthKZ9uU7A8",
    "name_range": "실험용!B5:B13",      # 업체명 열
    "hint_range": "실험용!C5:C13",      # 업종/키워드 힌트 열 (예: "초콜릿", "광고대행사")
    "email_range": "실험용!F5:F13",     # 검색한 이메일을 써넣을 열
    "subject_range": "실험용!G5:G13",   # 작성한 제목 초안을 써넣을 열
    "body_range": "실험용!H5:H13",      # 작성한 본문 초안을 써넣을 열
    "sponsor_items": "문행대동제 부스 협찬: 제품 샘플 500개, 부스 배너 노출, 공식 SNS 홍보 1회",
    "attachment_path": r"C:\Users\kksh3\Downloads\2026 성균관대학교 문행대동제 프로모션 제안서.pdf",
}
# =================================


def cmd_prepare(args):
    """검색 + 작성 → 시트에 이메일/제목/본문 저장."""
    creds = authenticate()
    llm = get_llm()
    sender_name, _ = get_sender_info(creds)

    # 업체명과 힌트를 같은 방식(행 패딩)으로 읽어 행 정렬을 맞춘다
    names = read_column(creds, CONFIG["spreadsheet_id"], CONFIG["name_range"])
    hints = read_column(creds, CONFIG["spreadsheet_id"], CONFIG["hint_range"])
    companies = [{"name": n, "hint": h} for n, h in zip(names, hints)]
    if args.limit:
        companies = companies[:args.limit]
    print(f"업체 {sum(1 for c in companies if c['name'])}개 로드")

    state = {
        "companies": companies,
        "sponsor_items": CONFIG["sponsor_items"],
        "sender_name": sender_name,
    }
    graph = build_prepare_graph(llm)
    result = graph.invoke(state)

    companies = result["companies"]
    write_column(creds, CONFIG["spreadsheet_id"], CONFIG["email_range"],
                 [c.get("email", "") for c in companies])
    write_column(creds, CONFIG["spreadsheet_id"], CONFIG["subject_range"],
                 [c.get("subject", "") for c in companies])
    write_column(creds, CONFIG["spreadsheet_id"], CONFIG["body_range"],
                 [c.get("body", "") for c in companies])
    print("시트에 이메일/제목/본문 저장 완료")

    print("\n===== 요약 =====")
    for c in companies:
        print(f"- {c['name']}: 이메일={c.get('email', '-')}, 검증={c.get('verified')}, "
              f"초안={'O' if c.get('subject') else 'X'}")


def cmd_send(args):
    """시트에서 초안을 읽어 발송 + 답장 분류."""
    creds = authenticate()
    llm = get_llm()
    sender_name, sender_email = get_sender_info(creds)

    # 발송 단계는 네 열을 같은 방식(행 패딩)으로 읽어 행 정렬을 보장
    names = read_column(creds, CONFIG["spreadsheet_id"], CONFIG["name_range"])
    emails = read_column(creds, CONFIG["spreadsheet_id"], CONFIG["email_range"])
    subjects = read_column(creds, CONFIG["spreadsheet_id"], CONFIG["subject_range"])
    bodies = read_column(creds, CONFIG["spreadsheet_id"], CONFIG["body_range"])

    companies = []
    for name, email, subject, body in zip(names, emails, subjects, bodies):
        if email and subject and body:  # 초안이 다 있는 행만 발송 대상
            companies.append({"name": name, "email": email,
                              "subject": subject, "body": body})
    if args.limit:
        companies = companies[:args.limit]
    print(f"발송 대상 {len(companies)}개")

    state = {
        "companies": companies,
        "sender_name": sender_name,
        "sender_email": sender_email,
    }
    graph = build_send_graph(creds, llm, attachment_path=CONFIG["attachment_path"])
    result = graph.invoke(state)

    print("\n===== 요약 =====")
    for c in result["companies"]:
        print(f"- {c['name']}: 발송={c.get('sent')}, 답장={c.get('reply_status', '-')}")


def cmd_inspect(args):
    """업체 하나의 검색 발견 로직을 그대로 출력. --hint 로 업종/키워드를 줄 수 있다."""
    llm = get_llm()
    d = debug_search(args.company, llm, hint=args.hint)
    f = d["finding"]

    print(f"\n[검색어] {d['query']}\n")
    if d.get("answer"):
        print(f"[Tavily 종합답변] {d['answer']}\n")
    print(f"[Tavily 원본 결과] {len(d['results'])}건")
    for i, r in enumerate(d["results"][:5], 1):
        if not isinstance(r, dict):
            continue
        content = (r.get("content", "") or "")[:160]
        print(f"  {i}. {r.get('title', '')}\n     {r.get('url', '')}\n     {content}")

    print("\n[LLM 추출/판단]")
    print(f"  이메일      : {f.email or '(없음)'}")
    print(f"  공식 도메인  : {f.official_domain or '(모름)'}")
    print(f"  도메인 일치  : {f.domain_match}")
    print(f"  업종 일치    : {f.is_target_business}")
    print(f"  신뢰도      : {f.confidence}")
    print(f"  근거        : {f.reasoning}")
    print(f"  업체 요약    : {f.company_summary}")
    print(f"\n[최종 검증] verified = {d['verified']}")
    print("  (verified = 이메일 있음 AND 업종 일치 AND 신뢰도>=0.5)")


def main():
    parser = argparse.ArgumentParser(description="멀티 에이전트 협찬 메일 자동화")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prep = sub.add_parser("prepare", help="검색+작성 후 시트에 저장")
    p_prep.add_argument("--limit", type=int, default=None, help="앞 N개 업체만")
    p_prep.set_defaults(func=cmd_prepare)

    p_send = sub.add_parser("send", help="시트 초안을 읽어 발송+답장")
    p_send.add_argument("--limit", type=int, default=None, help="앞 N개만 발송")
    p_send.set_defaults(func=cmd_send)

    p_insp = sub.add_parser("inspect", help="업체 하나 검색 로직 확인")
    p_insp.add_argument("company", help="확인할 업체명")
    p_insp.add_argument("--hint", default="", help="업종/키워드 힌트 (예: 초콜릿)")
    p_insp.set_defaults(func=cmd_inspect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
