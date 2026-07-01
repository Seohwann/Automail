"""작성 에이전트.

검색 에이전트가 만든 업체 정보(info)와 협찬 품목을 참고해 업체별 맞춤 제안 '본문 내용'을 생성한다.
제목은 고정 템플릿으로 만들고, 본문 맨 앞에는 담당자 소개(캠퍼스/이름)를 붙인 뒤 문장마다 줄바꿈한다.

  제목: [{업체명}] 성균관대학교 {캠퍼스} 2026 {행사명} 제안서 송부의 건
  본문: 성균관대학교 총학생회 {캠퍼스} 프로모션 담당자 {이름} 입니다.\n\n{LLM 제안 내용(문장별 줄바꿈)}
"""
import re
from datetime import date

from pydantic import BaseModel, Field


class BodyDraft(BaseModel):
    intro: str = Field(description="이메일 도입부: 업체 인사 → 협업 제안 → 행사 소개 → 이 제안이 "
                                   "업체에 주는 의미. 존댓말, 약 200~400자. 라벨·맺음말 제외.")


class Proposal(BaseModel):
    subject: str = Field(description="이메일 제목")
    body: str = Field(description="이메일 본문")


def _one_sentence_per_line(text: str) -> str:
    """문장 종결부호(. ? !) 뒤에서 줄을 나눠 한 문장씩 한 줄로 만든다."""
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.?!])\s+", text)
    return "\n".join(p.strip() for p in parts if p.strip())


def _format_kdate(iso: str) -> str:
    """'2026-06-10' → '2026년 6월 10일(수)'. 앞자리 0 제거, 한글 요일."""
    if not iso:
        return ""
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        wd = "월화수목금토일"[date(y, m, d).weekday()]
        return f"{y}년 {m}월 {d}일({wd})"
    except Exception:  # noqa: BLE001
        return iso


def run_writer_agent(company: dict, sponsor_items: str, sender_name: str, llm,
                     campus: str = "", name: str = "", event: str = "",
                     phone: str = "", event_date: str = "") -> Proposal:
    """업체 한 건에 대한 맞춤 제안서 생성 (제목 고정 + 담당자 소개 + 문장별 줄바꿈)."""
    who = name or sender_name or "성균관대학교 총학생회 대외협력국"
    campus = campus or "자연과학캠퍼스"
    prompt = (
        "당신은 대학 총학생회 대외협력국의 제안서 작성 담당자입니다.\n"
        "아래 업체에 보낼 협업 제안 이메일의 '도입부'만 담백하고 정중하게 작성하세요. "
        "제안 항목과 맺음말은 시스템이 별도로 붙이니 절대 쓰지 마세요.\n\n"
        f"[업체명] {company['name']}\n"
        f"[업체 정보] {company.get('info', '(정보 없음)')}\n"
        f"[행사명] {event or '행사'}\n"
        f"[제안 내용(참고)] {sponsor_items}\n\n"
        "규칙(담백하고 정중하게):\n"
        "- 자기소개 문장('성균관대학교 총학생회 ...입니다')은 넣지 마세요 (맨 앞에 별도로 붙습니다).\n"
        "- 첫 문장: 행사를 소개하며 '귀사와 함께하고자 연락드립니다' 취지로 협업을 제안하세요.\n"
        "- 이어서 행사의 규모·대상·의미를 사실 위주로 담백하게 소개하세요. "
        "제공된 정보에 없는 날짜·인원 등 구체 수치는 지어내지 마세요.\n"
        "- 이 제안이 업체에 주는 의미(학우들에게 남을 좋은 인상, 브랜드 이미지 제고·제품 홍보)를 "
        "한두 문장으로 밝히고 도입부를 끝내세요.\n"
        "- '제안 사항은', '제안 내용:', '홍보 효과:', '더 자세한', '감사합니다' 같은 항목·맺음말은 "
        "쓰지 마세요 (시스템이 붙입니다). 과장된 미사여구와 이모지는 금지.\n"
        "지정된 형식으로만 출력하세요."
    )
    draft = llm.with_structured_output(BodyDraft).invoke(prompt)
    intro = f"성균관대학교 {campus} 총학생회 프로모션 담당자 {who}입니다."
    signature = f"\n\n---\n성균관대학교 제58대 총학생회 S'PEAK\n대외협력국 {who}"
    if phone:
        signature += f"\nMobile: {phone}"
    items = (sponsor_items or "").strip()
    items = re.sub(r"\s+:", ":", items)  # 콜론 앞 공백 제거 → 앞 텍스트에 붙임
    m = re.search(r"홍보\s*효과\s*:", items)
    if m:
        items = items[:m.start()].rstrip().rstrip(",").rstrip() + "\n" + items[m.start():].strip()
    lines = ([f"행사 일자: {_format_kdate(event_date)}"] if event_date else []) + items.split("\n")
    # 제안 사항 전체(행사 일자·제안 내용·홍보 효과)를 굵게: 각 줄을 ** ** 로 감쌈
    bold_block = "\n".join(f"**{ln.replace('**', '').strip()}**"
                           for ln in lines if ln.strip())
    block = (
        "제안 사항은 다음과 같습니다.\n\n"
        f"{bold_block}\n\n"
        "더 자세한 내용은 첨부된 제안서를 확인해 주시면 감사하겠습니다.\n"
        "귀사의 무궁한 번영을 기원하며, 긍정적인 검토와 함께해 주시길 기다리겠습니다.\n"
        "감사합니다."
    )
    body = intro + "\n\n" + _one_sentence_per_line(draft.intro) + "\n\n" + block + signature
    subject = f"[{company['name']}] 성균관대학교 {campus} {event} 프로모션 제안서 송부의 건"
    return Proposal(subject=subject, body=body)
