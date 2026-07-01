"""검색 에이전트 (근거 검증 + 등급).

시트의 힌트(업종/키워드)로 검색을 좁히고, LLM 이 뽑은 이메일이 검색 결과 원문에 실제로
등장하는지(grounding) 확인해 환각을 거른다. 업종 일치/신뢰도로 등급을 매긴다.

등급(tier):
  HIGH  : 근거에 실재하는 이메일 + 힌트 업종 일치 + 신뢰도 충분 → 검증 O
  REVIEW: 이메일은 실재하나 업종 불일치/신뢰도 낮음 → 사람 확인 필요
  NONE  : 근거에 실제 이메일이 없음(추측 폐기 포함) → 미발견
"""
from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field

_SEARCH_TOOL = None


def _tool():
    """Tavily 검색 도구(지연 초기화)."""
    global _SEARCH_TOOL
    if _SEARCH_TOOL is None:
        _SEARCH_TOOL = TavilySearch(max_results=5, include_answer=True)
    return _SEARCH_TOOL


class EmailFinding(BaseModel):
    """LLM 이 검색 결과로부터 채우는 구조화 출력."""
    email: str = Field(description="아래 검색 결과 텍스트에 '실제로 등장하는' 협찬/제휴/마케팅 "
                                   "문의 이메일만. 없으면 빈 문자열. 절대 추측해서 만들지 말 것.")
    official_domain: str = Field(description="업체 공식 도메인. 모르면 빈 문자열.")
    domain_match: bool = Field(description="이메일 도메인이 공식 도메인과 부합하면 true.")
    is_target_business: bool = Field(description="검색된 업체가 힌트(업종/키워드)에 맞는 그 업체면 "
                                                 "true. 이름만 같은 다른 업종/회사면 false.")
    confidence: float = Field(description="0~1 사이 신뢰도.")
    company_summary: str = Field(description="업체 소개 2~3문장 (작성 에이전트가 참고).")
    reasoning: str = Field(description="판단 근거를 한국어 한 문장으로.")


def _extract_answer(raw):
    return raw.get("answer") if isinstance(raw, dict) else None


def _results_list(raw):
    if isinstance(raw, dict):
        return raw.get("results", [])
    if isinstance(raw, list):
        return raw
    return []


def _format_results(raw):
    """Tavily 응답을 LLM 프롬프트용 텍스트로 정규화. 종합 답을 맨 앞에 둔다."""
    if not isinstance(raw, (dict, list)):
        return str(raw)
    lines = []
    answer = _extract_answer(raw)
    if answer:
        lines.append(f"[종합 답변] {answer}")
    for r in _results_list(raw)[:5]:
        if isinstance(r, dict):
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("content", "") or "")[:400]
            lines.append(f"- {title} ({url})\n  {content}")
    return "\n".join(lines) if lines else str(raw)


def _haystack(raw):
    """이메일 grounding 용 원문 뭉치: 답변 + 모든 결과의 전체 내용/URL/제목."""
    parts = []
    a = _extract_answer(raw)
    if a:
        parts.append(a)
    for r in _results_list(raw):
        if isinstance(r, dict):
            parts.append(r.get("content", "") or "")
            parts.append(r.get("url", "") or "")
            parts.append(r.get("title", "") or "")
    return "\n".join(parts)


def _search_query(company_name, hint):
    h = f" {hint}" if hint else ""
    return f"{company_name}{h} 협찬 제휴 문의 이메일"


def _extract(company_name, hint, snippets, llm):
    """검색 결과 텍스트에서 LLM 으로 이메일 추출 + 판단."""
    hint_line = (
        f"이 업체는 '{hint}' 관련 업체입니다. 결과가 이름만 같은 다른 업종/회사면 "
        "is_target_business=false 로 두고 confidence 를 낮추세요.\n" if hint else ""
    )
    prompt = (
        "당신은 기업 연락처를 조사하는 리서치 에이전트입니다.\n"
        f"아래는 '{company_name}' 에 대한 웹 검색 결과입니다.\n\n"
        f"{snippets}\n\n"
        f"{hint_line}"
        "규칙:\n"
        "1) 협찬/제휴/마케팅 문의용 이메일을 찾되, 위 결과 텍스트에 '실제로 등장하는' 주소만 "
        "출력하세요. 결과에 이메일이 없으면 email 을 빈 문자열로 두세요. 추측해서 만들지 마세요.\n"
        "2) 검색된 업체가 힌트에 맞는 그 업체인지(is_target_business) 판단하세요.\n"
        "3) 작성 에이전트가 참고할 수 있도록 업체를 2~3문장으로 요약하세요.\n"
        "지정된 형식으로만 출력하세요."
    )
    return llm.with_structured_output(EmailFinding).invoke(prompt)


def _grade(finding, hay):
    """grounding(원문 실재) + 업종/신뢰도로 (email, tier, verified, reason) 산출."""
    email = (finding.email or "").strip()
    grounded = bool(email) and email in hay
    if not grounded:
        reason = finding.reasoning
        if finding.email:
            reason = "검색 결과 원문에 없는 이메일이라 폐기(환각 가능). " + reason
        return "", "NONE", False, reason
    if finding.is_target_business and finding.confidence >= 0.5:
        return email, "HIGH", True, finding.reasoning
    return email, "REVIEW", False, finding.reasoning


def run_search_agent(company_name, llm, hint=""):
    """업체명 하나를 처리해 {email, verified, tier, verify_reason, info, query} 반환."""
    raw = _tool().invoke({"query": _search_query(company_name, hint)})
    hay = _haystack(raw)
    snippets = _format_results(raw)
    finding = _extract(company_name, hint, snippets, llm)
    email, tier, verified, reason = _grade(finding, hay)
    return {
        "email": email,
        "verified": verified,
        "tier": tier,
        "verify_reason": reason,
        "info": finding.company_summary,
        "query": _search_query(company_name, hint),
    }


def debug_search(company_name, llm, hint=""):
    """발견 로직 확인용(CLI inspect). 검색 원본 + LLM 판단 + 최종 등급."""
    raw = _tool().invoke({"query": _search_query(company_name, hint)})
    snippets = _format_results(raw)
    finding = _extract(company_name, hint, snippets, llm)
    res = run_search_agent(company_name, llm, hint)
    return {
        "query": _search_query(company_name, hint),
        "answer": _extract_answer(raw),
        "results": _results_list(raw),
        "snippets": snippets,
        "finding": finding,
        "verified": res["verified"],
        "tier": res["tier"],
    }
