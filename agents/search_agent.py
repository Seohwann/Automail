"""검색 에이전트 (1단계, 시트 힌트 활용).

시트의 '힌트' 열(업종/키워드, 예: "초콜릿", "광고대행사")을 업체명과 함께 검색해
동명이의("갤러" vs "갤러리")를 사람이 미리 구분해 준다. 이렇게 하면 검색을 1번만 하고도
정확도가 높아진다. LLM 은 검색 결과에서 이메일을 추출하고 업종 일치/신뢰도로 검증한다.
"""
from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field

_SEARCH_TOOL = None


def _tool():
    """Tavily 검색 도구(지연 초기화). 종합 답변(answer)을 함께 받는다."""
    global _SEARCH_TOOL
    if _SEARCH_TOOL is None:
        _SEARCH_TOOL = TavilySearch(max_results=5, include_answer=True)
    return _SEARCH_TOOL


class EmailFinding(BaseModel):
    """LLM 이 검색 결과로부터 채우는 구조화 출력."""
    email: str = Field(description="찾은 협찬/제휴/마케팅 문의 이메일. 못 찾으면 빈 문자열.")
    official_domain: str = Field(description="업체 공식 도메인. 모르면 빈 문자열.")
    domain_match: bool = Field(description="이메일 도메인이 공식 도메인/공식 수입원과 부합하면 true.")
    is_target_business: bool = Field(
        description="검색된 업체가 힌트(업종/키워드)에 맞는 그 업체면 true. "
        "이름만 같은 다른 업종/해외 기업이면 false."
    )
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


def _format_results(raw) -> str:
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


def _search_query(company_name: str, hint: str) -> str:
    """검색어: 업체명 + 시트 힌트 + 핵심 키워드."""
    h = f" {hint}" if hint else ""
    return f"{company_name}{h} 협찬 제휴 문의 이메일"


def _extract(company_name: str, hint: str, snippets: str, llm) -> EmailFinding:
    """검색 결과 텍스트에서 LLM 으로 이메일 추출 + 검증."""
    hint_line = (
        f"이 업체는 '{hint}' 관련 업체입니다. 검색 결과가 그게 아니라 이름만 같은 다른 업종/해외 "
        "기업이면 is_target_business=false 로 두고 confidence 를 낮추세요.\n"
        if hint else ""
    )
    prompt = (
        "당신은 기업 연락처를 조사하는 리서치 에이전트입니다.\n"
        f"아래는 '{company_name}' 에 대한 웹 검색 결과입니다. (맨 위 '[종합 답변]' 우선 참고)\n\n"
        f"{snippets}\n\n"
        f"{hint_line}"
        "작업:\n"
        "1) 협찬/제휴/마케팅 문의용 이메일 주소를 찾으세요.\n"
        "2) 공식 수입원/대행사가 대신 받는 메일(개인 naver/gmail 등)이라도 그 업체의 공식 문의처면 "
        "인정하고, 도메인이 회사/수입원과 부합하면 domain_match=true 로 두세요.\n"
        "3) 검색된 업체가 힌트에 맞는 그 업체인지(is_target_business) 판단하세요.\n"
        "4) 작성 에이전트가 참고할 수 있도록 업체를 2~3문장으로 요약하세요.\n"
        "지정된 형식으로만 출력하세요."
    )
    return llm.with_structured_output(EmailFinding).invoke(prompt)


def _run(company_name: str, llm, hint: str):
    """검색 + LLM 추출. (query, raw, snippets, finding) 반환."""
    query = _search_query(company_name, hint)
    raw = _tool().invoke({"query": query})
    snippets = _format_results(raw)
    finding = _extract(company_name, hint, snippets, llm)
    return query, raw, snippets, finding


def _is_verified(finding: EmailFinding) -> bool:
    """이메일이 있고, 힌트 업종이 맞고, 신뢰도가 충분하면 통과."""
    return bool(finding.email) and finding.is_target_business and finding.confidence >= 0.5


def run_search_agent(company_name: str, llm, hint: str = "") -> dict:
    """업체명 하나를 처리해 {email, verified, verify_reason, info} 를 반환."""
    _, _, _, finding = _run(company_name, llm, hint)
    return {
        "email": finding.email,
        "verified": _is_verified(finding),
        "verify_reason": finding.reasoning,
        "info": finding.company_summary,
    }


def debug_search(company_name: str, llm, hint: str = "") -> dict:
    """발견 로직 확인용. 검색어 + 종합답 + 원본 결과 + LLM 판단을 그대로 돌려준다."""
    query, raw, snippets, finding = _run(company_name, llm, hint)
    return {
        "query": query,
        "answer": _extract_answer(raw),
        "results": _results_list(raw),
        "snippets": snippets,
        "finding": finding,
        "verified": _is_verified(finding),
    }
