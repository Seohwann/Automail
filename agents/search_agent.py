"""검색 에이전트 (ReAct — LLM 이 도구를 스스로 골라 탐색 전략을 결정).

기존에는 '도메인 식별 → 도메인 한정 검색 → 직접 조회 → 통합 검색' 4단계 폴백이
코드에 고정돼 있었다. 이제 에이전트가 web_search / open_website 도구를 보고
업체 상황에 맞는 전략을 스스로 선택·반복한다 (max_steps 로 상한만 강제).

grounding(환각 방지)은 코드가 결정적으로 보장한다 (agents/tools.py 참조):
- 도구가 원문에서 정규식으로 후보를 추출해 CandidateStore 에 목격 도메인과 함께 기록
- 최종 이메일이 후보와 '정확 일치'하지 않으면 폐기 (환각/변형 방지)
- 검증 O(HIGH)는 후보가 '공식 도메인에서 실제 목격'된 경우에만 — LLM 주장만으론 불가

등급(tier):
  HIGH  : 공식 도메인 페이지/도메인 한정 검색에서 실제 발견 → 검증 O
  REVIEW: 공식 홈페이지 밖(디렉토리/기사 등)에서 찾은 후보 → 출력하되 사람 확인 필요
  NONE  : 실제 등장한 이메일이 없음 → 미발견
"""
from pydantic import BaseModel, Field

from agents.react import finalize, run_react
from agents.tools import CandidateStore, make_search_tools, split_hint

MAX_STEPS = 6   # 에이전트 도구 호출 반복 상한 (비용/지연 통제)


class SearchDecision(BaseModel):
    """에이전트 탐색 종료 후 최종 판단 (구조화 출력)."""
    email: str = Field(description="도구 결과에 '실제로 등장한' 협찬/제휴/마케팅 문의 "
                                   "이메일 하나. 없으면 빈 문자열. 절대 추측 금지.")
    official_domain: str = Field(description="업체 공식 홈페이지 도메인 (예: 'example.com'). "
                                             "포털/SNS/블로그 등 플랫폼 도메인 금지. "
                                             "확인 못 했으면 빈 문자열.")
    is_target_business: bool = Field(description="찾은 업체가 힌트(업종/키워드)에 맞는 그 "
                                                 "업체면 true. 이름만 같은 다른 회사면 false.")
    confidence: float = Field(description="0~1 사이 신뢰도.")
    company_summary: str = Field(description="업체 소개 2~3문장 (작성 에이전트가 참고).")
    reasoning: str = Field(description="판단 근거를 한국어 한두 문장으로.")


_SYSTEM = """당신은 기업의 협찬/제휴 문의 이메일을 찾는 리서치 에이전트입니다.
web_search(웹 검색, include_domain 으로 도메인 한정 가능)와 open_website(페이지 직접
조회) 도구를 자유롭게 조합해 목표를 달성하세요.

목표: 업체의 '공식' 문의 이메일을 찾고, 그 이메일이 공식 홈페이지에 실제 게시돼
있는지 확인하는 것.

권장 전략 (상황에 맞게 스스로 조정하세요):
1) 먼저 공식 홈페이지 도메인을 확인하세요. 네이버/인스타그램/블로그/지도/배달앱 등
   플랫폼 도메인은 공식 도메인이 아닙니다.
2) 공식 도메인을 찾으면 include_domain 으로 한정 검색하거나 open_website 로 직접
   열어 이메일을 찾으세요. 소규모 쇼핑몰은 검색 인덱스에 없을 수 있으니 직접 조회가
   효과적입니다. open_website 는 홈과 문의성 하위 페이지를 '자동으로 함께' 조회하므로
   같은 사이트에 반복 호출하지 마세요 (호출 예산 낭비).
3) 공식 도메인에서 못 찾으면 '반드시' 전체 웹 검색으로 폴백하세요
   (예: "업체명 협찬 문의 이메일", "업체명 대표 이메일", "업체명 사업자 정보").
   제3자 사이트의 주소는 신뢰도가 낮지만(REVIEW), 미발견보다는 낫습니다.

규칙:
- 한국 업체 또는 해외 브랜드의 '한국 지사/한국 공식몰'만 대상입니다. 해외 법인·해외
  소비자용 사이트(.com.au, usa/global 전용 등)의 주소는 답이 아닙니다.
- 이름만 같은 다른 업종/회사에 주의하세요 (힌트와 대조).
- 도구 결과에 실제로 등장한 이메일만 답할 수 있습니다. 추측·조합 금지.
- 충분히 확인했으면 도구 호출을 멈추고 조사 결과를 요약하세요.
- 같은 검색을 반복하지 말고, 각 호출마다 전략을 바꾸세요.
- 포기하기 전 체크리스트: 통합 웹 검색(전략 3)을 시도하지 않았다면 아직 포기하면
  안 됩니다. 실패 원인을 분석해 다른 각도의 검색어로 이어가세요."""


def _grade(store, decision):
    """결정적 등급 판정 — CandidateStore 기록만 신뢰한다."""
    email = (decision.email or "").strip().lower()
    if not email:
        return "", "NONE", False, decision.reasoning
    if email not in store.all():
        return ("", "NONE", False,
                "후보에 정확히 일치하는 주소가 아니라 폐기(환각/변형 가능). "
                + decision.reasoning)
    good = decision.is_target_business and decision.confidence >= 0.5
    if good and decision.official_domain and store.seen_on(email, decision.official_domain):
        return (email, "HIGH", True,
                "공식 홈페이지에서 확인된 이메일. " + decision.reasoning)
    if good:
        return (email, "REVIEW", False,
                "공식 홈페이지에서 확인되지 않은 주소라 사람 확인 필요. "
                + decision.reasoning)
    return email, "REVIEW", False, decision.reasoning


def run_search_agent(company_name, llm, hint="", instruction="", on_event=None):
    """업체명 하나를 처리해 {email, verified, tier, verify_reason, info, query} 반환.

    instruction 은 supervisor 가 재시도 시 내려보내는 추가 지시(예: 검색 관점 변경).
    """
    hint_text, url_domain = split_hint(hint)
    store = CandidateStore()
    tools = make_search_tools(store)

    user = f"업체명: {company_name}"
    if hint_text:
        user += f"\n업종/키워드 힌트: {hint_text}"
    if url_domain:
        user += (f"\n공식 도메인(시트에 기재됨): {url_domain} — 도메인 탐색을 건너뛰고 "
                 "이 도메인에서 바로 이메일을 찾으세요.")
    if instruction:
        user += f"\n[관리자 추가 지시] {instruction}"
    user += "\n\n이 업체의 공식 협찬/제휴 문의 이메일을 찾으세요."

    messages, trace = run_react(llm, tools, _SYSTEM, user,
                                max_steps=MAX_STEPS, on_event=on_event)
    decision = finalize(
        llm, SearchDecision, messages,
        "조사를 종료합니다. 지금까지의 도구 결과에 근거해 최종 판단을 지정된 형식으로만 "
        "출력하세요. 도구 결과에 실제로 등장한 이메일만 email 에 적을 수 있습니다.")
    email, tier, verified, reason = _grade(store, decision)
    return {"email": email, "tier": tier, "verified": verified,
            "verify_reason": reason, "info": decision.company_summary,
            "query": " → ".join(trace) if trace else "(도구 호출 없음)"}
