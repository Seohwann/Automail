"""멀티 에이전트 공용 도구 + grounding 후보 저장소.

에이전트(LLM)가 스스로 골라 호출하는 도구들을 정의한다.
- web_search   : Tavily 웹 검색 (도메인 한정 가능)
- open_website : 페이지 직접 조회 (검색 인덱스 미수록 대비)

grounding(환각 방지)은 도구 계층에서 결정적으로 보장한다:
모든 도구는 원문에서 정규식으로 이메일 후보를 추출해 CandidateStore 에
'어느 도메인에서 실제로 봤는지'와 함께 기록한다. 에이전트가 최종 답을 내면
코드는 (1) 후보 집합과 정확 일치하는지, (2) 공식 도메인에서 실제로 목격됐는지를
검사해 등급(HIGH/REVIEW/NONE)을 결정한다 — LLM 의 주장만으로 검증 O 를 주지 않는다.
"""
import re
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse

from langchain_core.tools import tool
from langchain_tavily import TavilySearch

URL_RE = re.compile(r"https?://\S+|www\.\S+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 이미지 파일명 등이 이메일 패턴에 오인 매칭되는 것 방지 (예: icon@2x.png)
_ASSET_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".ico")
# 직접 조회 시 홈에서 추적할 문의성 하위 페이지 링크
_CONTACT_LINK_RE = re.compile(
    r"contact|about|company|support|guide|agreement|privacy|문의|회사|고객", re.I)


def normalize_domain(s):
    """'https://www.example.com/path' → 'example.com'."""
    s = (s or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    host = (urlparse(s).netloc or "").split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def split_hint(hint):
    """힌트에서 URL 을 분리해 (업종 텍스트, 도메인) 반환. URL 이 없으면 도메인은 빈 문자열."""
    m = URL_RE.search(hint or "")
    if not m:
        return (hint or "").strip(), ""
    url = m.group(0).rstrip(".,)")
    text = (hint[:m.start()] + hint[m.end():]).strip(" ,;·")
    return text, normalize_domain(url)


class CandidateStore:
    """이메일 후보와 '실제로 목격된 도메인'을 기록하는 결정적 저장소.

    에이전트 실행 1회당 하나를 만들어 도구들과 공유한다. 최종 검증은
    이 저장소의 기록만 신뢰한다 (LLM 의 자기 보고는 검증에 쓰지 않음).
    """

    def __init__(self):
        self.sources = {}   # email -> {발견 도메인}

    def add(self, email, domain=""):
        e = (email or "").lower().rstrip(".")
        if not e or e.endswith(_ASSET_EXT):
            return
        self.sources.setdefault(e, set())
        d = normalize_domain(domain)
        if d:
            self.sources[e].add(d)

    def add_from_text(self, text, domain=""):
        for e in EMAIL_RE.findall(text or ""):
            self.add(e, domain)

    def all(self):
        return list(self.sources)

    def seen_on(self, email, domain):
        """이 이메일이 해당 도메인(또는 그 서브도메인) 페이지에서 실제 목격됐는가."""
        d = normalize_domain(domain)
        if not d:
            return False
        return any(s == d or s.endswith("." + d)
                   for s in self.sources.get((email or "").lower(), ()))


# ---------- 저수준 조회 (도구 내부에서 사용) ----------

def _fetch_page(url, timeout=8):
    """페이지 HTML 을 직접 받는다 (macOS 인증서/한국 인코딩 폴백 포함)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except (ssl.SSLError, urllib.error.URLError) as e:
        if "SSL" not in str(e) and "certificate" not in str(e).lower():
            raise
        ctx = ssl._create_unverified_context()  # noqa: S323 - 공개 페이지 읽기 전용
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = r.read()
    for enc in ("utf-8", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def _fetch_site_text(domain):
    """공식 사이트 홈 + 문의성 하위 페이지(최대 2개)의 HTML 을 합쳐 반환."""
    home = ""
    for scheme in ("https", "http"):
        try:
            home = _fetch_page(f"{scheme}://{domain}")
            break
        except Exception:  # noqa: BLE001 - 접속 실패 시 다음 스킴으로
            continue
    if not home:
        return ""
    texts = [home]
    picked = []
    for href in re.findall(r'href=["\x27]([^"\x27]+)["\x27]', home):
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


def _tavily(query, domains=None):
    tool_ = TavilySearch(max_results=8, include_answer=True,
                         search_depth="advanced", include_raw_content=True,
                         include_domains=domains)
    return tool_.invoke({"query": query})


def _results_list(raw):
    if isinstance(raw, dict):
        return raw.get("results", [])
    if isinstance(raw, list):
        return raw
    return []


# ---------- 에이전트 도구 팩토리 ----------

def make_search_tools(store):
    """검색 에이전트용 도구 목록. 모든 결과의 이메일 후보를 store 에 기록한다."""

    @tool
    def web_search(query: str, include_domain: str = "") -> str:
        """웹을 검색해 결과 요약과 발견된 이메일 후보를 돌려준다.

        include_domain 에 'example.com' 처럼 도메인을 주면 그 도메인 안에서만
        검색한다 (공식 홈페이지에 게시된 이메일을 찾을 때 사용).
        """
        domains = [normalize_domain(include_domain)] if include_domain.strip() else None
        try:
            raw = _tavily(query, domains)
        except Exception as e:  # noqa: BLE001 - 도구 실패는 관찰로 반환
            return f"검색 실패: {e}"
        lines = []
        answer = raw.get("answer") if isinstance(raw, dict) else None
        if answer:
            store.add_from_text(answer, "")   # 출처 불명 → 도메인 없이 기록
            lines.append(f"[종합 답변] {answer}")
        found = []
        for r in _results_list(raw)[:8]:
            if not isinstance(r, dict):
                continue
            url = r.get("url", "")
            dom = normalize_domain(url)
            text = " ".join([r.get("title", ""), r.get("content", "") or "",
                             r.get("raw_content", "") or ""])
            store.add_from_text(text, dom)
            store.add_from_text(url, dom)
            for e in EMAIL_RE.findall(text + " " + url):
                e = e.lower().rstrip(".")
                if not e.endswith(_ASSET_EXT) and (e, dom) not in found:
                    found.append((e, dom))
            content = (r.get("raw_content") or r.get("content") or "")[:700]
            lines.append(f"- {r.get('title', '')} ({url})\n  {content}")
        if found:
            lines.append("\n[이 검색에서 발견된 이메일 후보]")
            lines += [f"- {e}  (출처: {d or '불명'})" for e, d in found[:15]]
        else:
            lines.append("\n[이 검색에서 발견된 이메일 후보 없음]")
        return "\n".join(lines) if lines else "결과 없음"

    @tool
    def open_website(url_or_domain: str) -> str:
        """웹페이지(도메인 또는 URL)를 직접 열어 본문과 이메일 후보를 가져온다.

        검색 인덱스에 없는 페이지(소규모 쇼핑몰 푸터·문의 페이지 등)에서
        이메일을 찾을 때 사용한다. 홈과 문의성 하위 페이지까지 함께 조회한다.
        """
        domain = normalize_domain(url_or_domain)
        if not domain:
            return "유효한 도메인이 아닙니다."
        try:
            text = _fetch_site_text(domain)
        except Exception as e:  # noqa: BLE001
            return f"접속 오류({type(e).__name__}): {e}"
        if not text:
            return f"{domain} 접속 실패"
        store.add_from_text(text, domain)
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
        cands = []
        for e in EMAIL_RE.findall(text):
            e = e.lower().rstrip(".")
            if not e.endswith(_ASSET_EXT) and e not in cands:
                cands.append(e)
        low = plain.lower()
        windows = []
        for e in cands[:10]:
            i = low.find(e)
            if i >= 0:
                # 이메일은 보통 푸터에 있어 주변 ±200자 문맥을 함께 제공
                windows.append(plain[max(0, i - 200): i + len(e) + 200])
        out = [f"[{domain} 페이지 원문 앞부분]\n{plain[:1200]}"]
        if cands:
            out.append("[발견된 이메일 후보] " + ", ".join(cands[:15]))
            out.append("[후보 주변 문맥]\n" + "\n---\n".join(windows))
        else:
            out.append("[이 페이지에서 이메일 후보 없음]")
        return "\n\n".join(out)

    return [web_search, open_website]


def make_research_tool():
    """답장 에이전트용 보조 검색 도구 (문의 답변에 필요한 정보 조사)."""

    @tool
    def web_search(query: str) -> str:
        """웹을 검색해 결과 요약을 돌려준다. 업체 문의에 답하기 위한 정보 조사용."""
        try:
            raw = _tavily(query)
        except Exception as e:  # noqa: BLE001
            return f"검색 실패: {e}"
        lines = []
        answer = raw.get("answer") if isinstance(raw, dict) else None
        if answer:
            lines.append(f"[종합 답변] {answer}")
        for r in _results_list(raw)[:5]:
            if isinstance(r, dict):
                lines.append(f"- {r.get('title', '')} ({r.get('url', '')})\n"
                             f"  {(r.get('content') or '')[:500]}")
        return "\n".join(lines) or "결과 없음"

    return web_search
