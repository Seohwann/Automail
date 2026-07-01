"""답장 에이전트.

발송한 업체로부터 온 답장을 가져와:
  - accepted(수락) / rejected(거절) / question(추가 문의) / no_reply(무응답) 로 분류하고,
  - 분류에 맞는 후속 대응 이메일 초안(follow_up)을 자율적으로 생성한다.

프로토타입에서는 후속 메일을 '초안'으로만 만들어 사람이 확인 후 보내도록 한다.
분류+초안 생성 로직은 classify_reply 로 분리해 CLI/웹 UI 가 함께 쓴다.
"""
from pydantic import BaseModel, Field

from agents.google_clients import fetch_latest_reply


class ReplyDecision(BaseModel):
    status: str = Field(description="accepted | rejected | question 중 하나")
    follow_up: str = Field(
        description="후속으로 보낼 이메일 본문 초안. "
        "거절이면 정중한 감사 인사, 문의면 답변, 수락이면 다음 절차 안내."
    )


def classify_reply(company_name: str, reply_text: str, sender_name: str, llm) -> dict:
    """답장 본문 하나를 수락/거절/문의로 분류하고 후속 초안을 만든다 (UI/CLI 공용)."""
    sender = sender_name or "성균관대학교 총학생회 대외협력국"
    prompt = (
        f"'{company_name}' 업체로부터 받은 답장입니다:\n"
        "---\n"
        f"{reply_text}\n"
        "---\n"
        "이 답장을 accepted(협찬 수락) / rejected(거절) / question(추가 문의) 중 하나로 분류하고,\n"
        "그에 맞는 후속 이메일 본문 초안을 한국어 존댓말로 작성하세요.\n"
        f"보내는 사람: {sender}"
    )
    decision: ReplyDecision = llm.with_structured_output(ReplyDecision).invoke(prompt)
    return {"reply_status": decision.status, "follow_up": decision.follow_up}


def run_reply_agent(creds, company: dict, sender_name: str, llm) -> dict:
    """업체 한 건의 답장을 분류하고 후속 초안을 반환.

    한 주소로 여러 업체에 보낸 테스트 상황에서도 업체별 답장을 구분하도록
    제목(업체명)으로 답장을 좁혀 조회한다.
    """
    reply_text = fetch_latest_reply(creds, company["email"],
                                    subject_query=company.get("name"))
    if not reply_text:
        return {"reply_status": "no_reply", "follow_up": ""}
    return classify_reply(company["name"], reply_text, sender_name, llm)
