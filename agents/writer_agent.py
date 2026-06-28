"""작성 에이전트.

검색 에이전트가 만든 업체 정보(info)와 협찬 품목을 참고해
업체별 맞춤 제안 이메일(제목/본문)을 생성한다.
"""
from pydantic import BaseModel, Field


class Proposal(BaseModel):
    subject: str = Field(description="이메일 제목. [업체명] 을 포함할 것.")
    body: str = Field(description="이메일 본문. 존댓말, 인사-제안 이유-협찬 내용-마무리 구성, 약 350~600자.")


def run_writer_agent(company: dict, sponsor_items: str, sender_name: str, llm) -> Proposal:
    """업체 한 건에 대한 맞춤 제안서를 생성."""
    sender = sender_name or "성균관대학교 총학생회 대외협력국"
    prompt = (
        "당신은 대학 총학생회 대외협력국의 협찬 제안서 작성 담당자입니다.\n"
        "아래 업체에 보낼 협찬 제안 이메일을 맞춤형으로 작성하세요.\n\n"
        f"[업체명] {company['name']}\n"
        f"[업체 정보] {company.get('info', '(정보 없음)')}\n"
        f"[제안할 협찬 품목/내용] {sponsor_items}\n"
        f"[보내는 사람] {sender}\n\n"
        "규칙:\n"
        "- 정중한 존댓말로, 진정성 있게. 과장된 표현과 이모지는 금지.\n"
        "- 업체 정보를 반영해 '왜 이 업체와 협업하고 싶은지' 를 한 문장 포함.\n"
        "- 협찬 품목을 본문에 자연스럽게 녹여 제안할 것.\n"
        f"- 제목에 [{company['name']}] 를 넣을 것.\n"
        "지정된 형식으로만 출력하세요."
    )
    return llm.with_structured_output(Proposal).invoke(prompt)
