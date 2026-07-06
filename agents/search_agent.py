"""검색 에이전트 (2단계: 공식 도메인 식별 → 도메인 한정 이메일 추출).

1단계: 업체명+힌트로 '공식 홈페이지' 검색 → LLM 이 공식 도메인을 확정.
       힌트에 URL 이 포함돼 있으면(예: "수제쿠키, https://example.com") 그 도메인을
       그대로 쓰고 1단계를 건너뛴다.
2단계: 확정된 도메인으로 검색 범위를 좁혀(include_domains) 이메일을 추출.
2-1단계: 도메인 한정 '검색'이 실패하면 공식 사이트 페이지를 '직접' 열어(fetch)
       이메일을 추출한다 — 소규모 쇼핑몰은 검색 인덱스에 푸터/문의 페이지가
       수록되지 않은 경우가 많아서, 인덱스를 거치지 않는 경로가 필요하다.
폴백: 그래도 없으면 통합 쿼리(전체 웹)로 검색.

grounding: 검색 결과 원문에서 정규식으로 이메일 '후보'를 추출하고, LLM 은 그 후보
중에서만 고른다(정확 일치 검사 — 부분 문자열 오인 방지). 공식 홈페이지에
이메일이 없으면 통합 검색 후보 중 가장 가능성 높은 주소를 출력한다.

등급(tier):
  HIGH  : 공식 홈페이지(공식 도메인 한정 검색)에서 발견된 경우에만 → 검증 O
  REVIEW: 공식 홈페이지 밖(디렉토리/기사 등)에서 찾은 후보 → 출력하되 사람 확인 필요
          (웹에는 추정 주소를 게재하는 사이트가 많아 실재 문자열이어도 신뢰 불가)
  NONE  : 검색 결과에 실제 이메일이 하나도 없음 → 미발견
"""
import re
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse

from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 이미지 파일명 등이 이메일 패턴에 오인 매칭되는 것 방지 (예: icon@2x.png)
_ASSET_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico")
# 직접 조회 시 홈에서 추적할 문의성 하위 페이지 링크
_CONTACT_LINK_RE = re.compile(
    r"contact|about|company|support|guide|agreement|privacy|문의|회사|고객", re.I)


def _search(query, domains=None):
    """Tavily 검색. advanced 깊이 + 페이지 원문(raw_content) 포함, 필요시 도메인 한정."""
    tool = TavilySearch(max_results=8, include_answer=True,
                        search_depth="advanced", include_raw_content=True,
                        include_domains=domains)
    return tool.invoke({"query": query})


class DomainFinding(BaseModel):
    """1단계: 공식 도메인 식별용 구조화 출력."""
    official_domain: str = Field(description="업체 공식 홈페이지 도메인 (예: 'example.com'). "
                                             "포털/블로그/SNS/지도 사이트가 아닌 업체 자체 도메인만. "
                                             "검색 결과에서 확인 안 되면 빈 문자열.")
    is_target_business: bool = Field(description="찾은 홈페이지가 힌트(업종/키워드)에 맞는 그 "
                                                 "업체면 true. 이름만 같은 다른 업종/회사면 false.")
    confidence: float = Field(description="0~1 사이 신뢰도.")
    reasoning: str = Field(description="판단 근거를 한국어 한 문장으로.")


class EmailFinding(BaseModel):
    """2단계: 검색 결과로부터 이메일을 채우는 구조화 출력."""
    email: str = Field(description="아래 검색 결과 텍스트에 '실제로 등장하는' 협찬/제휴/마케팅 "
                                   "문의 이메일만. 없으면 빈 문자열. 절대 추측해서 만들지 말 것.")
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
    for r in _results_list(raw)[:8]:
        if isinstance(r, dict):
            title = r.get("title", "")
            url = r.get("url", "")
            content = (r.get("raw_content") or r.get("content") or "")[:800]
            lines.append(f"- {title} ({url})\n  {content}")
    return "\n".join(lines) if lines else str(raw)


def _haystack(raw):
    """이메일 grounding 용 원문 뭉치: 답변 + 모든 결과의 내용/페이지 원문/URL/제목."""
    parts = []
    a = _extract_answer(raw)
    if a:
        parts.append(a)
    for r in _results_list(raw):
        if isinstance(r, dict):
            parts.append(r.get("content", "") or "")
            parts.append(r.get("raw_content", "") or "")
            parts.append(r.get("url", "") or "")
            parts.append(r.get("title", "") or "")
    return "\n".join(parts)


def _normalize_domain(s):
    """'https://www.example.com/path' → 'example.com'."""
    s = (s or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    host = (urlparse(s).netloc or "").split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _candidates(hay):
    """원문 뭉치에서 정규식으로 이메일 후보를 추출 (소문자, 순서 유지 중복 제거)."""
    seen = []
    for e in _EMAIL_RE.findall(hay):
        e = e.lower().rstrip(".")
        if e.endswith(_ASSET_EXT):   # 파일명 오인 매칭 제거
            continue
        if e not in seen:
            seen.append(e)
    return seen[:20]


def _fetch_page(url, timeout=8):
    """페이지 HTML 을 직접 받는다 (macOS 인증서/한국 인코딩 폴백 포함)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except (ssl.SSLError, urllib.error.URLError) as e:
        # macOS 기본 파이썬은 루트 인증서가 없어 SSL 검증에 실패하는 경우가 흔하다.
        # 공개 페이지를 읽기만 하므로 검증 없이 1회 재시도한다.
        if "SSL" not in str(e) and "certificate" not in str(e).lower():
            raise
        ctx = ssl._create_unverified_context()  # noqa: S323
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = r.read()
    for enc in ("utf-8", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def _fetch_site_text(domain):
    """공식 사이트 홈 + 문의성 하위 페이지(최대 2개)의 HTML 을 합쳐 반환. 실패 시 빈 문자열."""
    home = ""
    for scheme in ("https", "http"):
        try:
            home = _fetch_page(f"{scheme}://{domain}")
            break
        except Exception:  # noqa: BLE001 - 접속 실패 시 다음 스킴/단계로
            continue
    if not home:
        return ""
    texts = [home]
    picked = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', home):
        if len(picked) >= 2 or not _CONTACT_LINK_RE.search(href):
            continue
        url = href if href.startswith("http") else f"https://{domain}/{href.lstrip('/')}"
        if domain in url and url not in picked:
            picked.append(url)
    for url in picked:
        try:
            texts.append(_fetch_page(url))
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(texts)


def _split_hint(hint):
    """힌트에서 URL 을 분리해 (업종 텍스트, 도메인) 반환. URL 이 없으면 도메인은 빈 문자열."""
    m = _URL_RE.search(hint or "")
    if not m:
        return (hint or "").strip(), ""
    url = m.group(0).rstrip(".,)")
    text = (hint[:m.start()] + hint[m.end():]).strip(" ,;·")
    return text, _normalize_domain(url)


def _find_domain(company_name, hint, llm):
    """1단계: 공식 홈페이지 검색으로 도메인 확정. (도메인, 사용한 쿼리) 반환."""
    query = (f"{company_name} {hint} 공식 홈페이지" if hint
             else f"{company_name} 공식 홈페이지")
    raw = _search(query)
    hint_line = (f"이 업체는 '{hint}' 관련 업체입니다. 이름만 같은 다른 업종/회사면 "
                 "is_target_business=false 로 두세요.\n" if hint else "")
    prompt = (
        "당신은 기업 정보를 조사하는 리서치 에이전트입니다.\n"
        f"아래는 '{company_name}' 의 공식 홈페이지를 찾기 위한 웹 검색 결과입니다.\n\n"
        f"{_format_results(raw)}\n\n"
        f"{hint_line}"
        "규칙:\n"
        "1) 업체 '자체' 공식 도메인만 official_domain 에 적으세요. 네이버/인스타그램/블로그/"
        "지도/배달앱 등 플랫폼 도메인은 안 됩니다. 확인 안 되면 빈 문자열.\n"
        "2) 한국 업체 또는 해외 브랜드의 '한국 지사/한국 공식' 사이트만 인정하세요. "
        "해외 법인·해외 소비자용 사이트(예: .com.au, .us, usa/eng/global 전용)는 "
        "official_domain 으로 삼지 마세요.\n"
        "3) 그 홈페이지가 힌트에 맞는 그 업체인지(is_target_business) 판단하세요.\n"
        "지정된 형식으로만 출력하세요."
    )
    f = llm.with_structured_output(DomainFinding).invoke(prompt)
    domain = _normalize_domain(f.official_domain)
    if not (domain and f.is_target_business and f.confidence >= 0.5):
        domain = ""
    return domain, query


def _extract(company_name, hint, snippets, llm, cands, official_site=False):
    """검색 결과 텍스트에서 LLM 으로 이메일 선택 + 판단. 후보(cands) 중에서만 고른다.

    official_site=True 면 텍스트가 업체 공식 사이트 원문이므로 제3자 제외 규칙을
    완화한다 — 브랜드몰은 운영사(수입·유통사) 명의 주소가 곧 공식 문의 창구다.
    """
    hint_line = (
        f"이 업체는 '{hint}' 관련 업체입니다. 결과가 이름만 같은 다른 업종/회사면 "
        "is_target_business=false 로 두고 confidence 를 낮추세요.\n" if hint else ""
    )
    if cands:
        cand_block = "발견된 이메일 후보:\n" + "\n".join(f"- {e}" for e in cands) + "\n\n"
        if official_site:
            rule1 = ("1) 위 텍스트는 이 업체 '공식 사이트'에서 직접 가져온 원문입니다. "
                     "후보 중 대표/고객/협찬 문의용으로 가장 적합한 주소 '하나'를 반드시 "
                     "고르세요. 사이트 하단 사업자 정보의 상호가 업체명과 달라도(운영사·"
                     "수입사·유통사 명의) 공식 문의 창구로 인정합니다. 개인정보보호책임자 "
                     "주소보다 대표 문의 주소를 우선하세요.\n")
        else:
            rule1 = ("1) 위 후보 중에서 이 업체의 협찬/제휴/마케팅/대표 문의용으로 가장 적합한 "
                     "주소 '하나만' email 에 적으세요. 후보에 없는 주소를 만들지 마세요. "
                     "기자·제3자·무관한 회사의 주소라면 고르지 말고 빈 문자열로 두세요. "
                     "한국에서 협찬 문의가 가능한 창구여야 하므로 해외 법인·해외 지사 주소"
                     "(예: .com.au, usa·global 법인 등)도 고르지 마세요.\n")
    else:
        cand_block = ""
        rule1 = "1) 검색 결과에 이메일이 없습니다. email 은 빈 문자열로 두세요.\n"
    prompt = (
        "당신은 기업 연락처를 조사하는 리서치 에이전트입니다.\n"
        f"아래는 '{company_name}' 에 대한 웹 검색 결과입니다.\n\n"
        f"{snippets}\n\n"
        f"{cand_block}"
        f"{hint_line}"
        "규칙:\n"
        f"{rule1}"
        "2) 검색된 업체가 힌트에 맞는 그 업체인지(is_target_business) 판단하세요.\n"
        "3) 작성 에이전트가 참고할 수 있도록 업체를 2~3문장으로 요약하세요.\n"
        "지정된 형식으로만 출력하세요."
    )
    return llm.with_structured_output(EmailFinding).invoke(prompt)


def _grade(finding, cands):
    """후보 정확 일치(grounding) + 업종/신뢰도로 (email, tier, verified, reason) 산출."""
    email = (finding.email or "").strip().lower()
    if not email or email not in cands:
        reason = finding.reasoning
        if email:
            reason = "후보에 정확히 일치하는 주소가 아니라 폐기(환각/변형 가능). " + reason
        return "", "NONE", False, reason
    if finding.is_target_business and finding.confidence >= 0.5:
        return email, "HIGH", True, finding.reasoning
    return email, "REVIEW", False, finding.reasoning


def _attempt(company_name, hint, query, llm, domains=None):
    """검색 1회 + 후보 추출 + LLM 선택 + 등급. 결과 dict 반환.

    domains 가 주어지면 공식 도메인 안에서만 검색한 것이므로, 발견된 이메일은
    '공식 홈페이지에 게시된 주소'로 보고 HIGH(검증 O)로 확정한다.
    """
    raw = _search(query, domains)
    cands = _candidates(_haystack(raw))
    finding = _extract(company_name, hint, _format_results(raw), llm, cands)
    email, tier, verified, reason = _grade(finding, cands)
    if email and domains:
        tier, verified = "HIGH", True
        reason = "공식 홈페이지에서 확인된 이메일. " + reason
    elif tier == "HIGH":
        # 공식 홈페이지 밖에서 찾은 주소는 추정 주소일 수 있어 검증 O 를 주지 않는다.
        tier, verified = "REVIEW", False
        reason = "공식 홈페이지에서 확인되지 않은 주소라 사람 확인 필요. " + reason
    return {"email": email, "tier": tier, "verified": verified,
            "verify_reason": reason, "info": finding.company_summary}


def _attempt_direct(company_name, hint, domain, llm):
    """공식 사이트를 직접 열어 이메일 추출 (검색 인덱스 미수록 대비).

    (결과 dict | None, 실패 사유) 를 반환한다. 실패 사유는 검색어 체인에 기록되어
    디버깅에 쓰인다. 여기서 찾은 주소는 공식 홈페이지에 실제 게시된 것이므로
    HIGH(검증 O)로 확정한다.
    """
    try:
        text = _fetch_site_text(domain)
    except Exception as e:  # noqa: BLE001 - 조회 실패는 폴백으로 이어가되 사유 기록
        return None, f"접속 오류({type(e).__name__})"
    if not text:
        return None, "접속 실패"
    cands = _candidates(text)
    if not cands:
        return None, "페이지에 이메일 없음"
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
    # 이메일은 보통 페이지 '끝'(푸터)에 있어 앞부분만 자르면 문맥이 잘린다.
    # 각 후보 주변 ±200자를 발췌해 근거 문맥을 반드시 포함시킨다.
    low = plain.lower()
    windows = []
    for e in cands[:10]:
        i = low.find(e)
        if i >= 0:
            windows.append(plain[max(0, i - 200): i + len(e) + 200])
    snippets = (f"[공식 사이트(https://{domain}) 페이지 원문 앞부분]\n{plain[:1500]}\n\n"
                "[이메일 후보 주변 문맥 발췌]\n" + "\n---\n".join(windows))
    finding = _extract(company_name, hint, snippets, llm, cands, official_site=True)
    email, _, _, reason = _grade(finding, cands)
    if not email:
        return None, "적합한 주소 없음(LLM 보류)"
    return {"email": email, "tier": "HIGH", "verified": True,
            "verify_reason": "공식 홈페이지에서 확인된 이메일(직접 조회). " + reason,
            "info": finding.company_summary}, ""


def run_search_agent(company_name, llm, hint=""):
    """업체명 하나를 처리해 {email, verified, tier, verify_reason, info, query} 반환."""
    hint_text, domain = _split_hint(hint)
    queries = []
    if not domain:
        domain, q1 = _find_domain(company_name, hint_text, llm)
        queries.append(q1)

    result = None
    if domain:
        q2 = f"{company_name} 이메일 연락처 협찬 제휴 문의"
        queries.append(f"{q2} (site:{domain})")
        result = _attempt(company_name, hint_text, q2, llm, domains=[domain])
        if result["tier"] == "NONE":
            direct, fail_note = _attempt_direct(company_name, hint_text, domain, llm)
            if direct:
                queries.append(f"공식 사이트 직접 조회({domain})")
                direct["query"] = " → ".join(queries)
                return direct
            queries.append(f"공식 사이트 직접 조회({domain}) 실패: {fail_note}")

    if result is None or result["tier"] == "NONE":
        h = f" {hint_text}" if hint_text else ""
        q3 = f"{company_name}{h} 협찬 제휴 문의 이메일"
        queries.append(q3)
        fallback = _attempt(company_name, hint_text, q3, llm)
        if result and not fallback.get("info"):
            fallback["info"] = result.get("info", "")
        result = fallback

    result["query"] = " → ".join(queries)
    return result


def debug_search(company_name, llm, hint=""):
    """발견 로직 확인용(CLI inspect). 단계별 쿼리 + 최종 판단/등급."""
    hint_text, domain = _split_hint(hint)
    res = run_search_agent(company_name, llm, hint)
    return {
        "hint_text": hint_text,
        "url_domain": domain,
        "query": res["query"],
        "email": res["email"],
        "tier": res["tier"],
        "verified": res["verified"],
        "verify_reason": res["verify_reason"],
        "info": res["info"],
    }
