"""답장 에이전트 (ReAct — 필요하면 웹 검색으로 조사해 답변 초안을 만든다).

발송한 업체로부터 온 답장을 가져와:
  - accepted(수락) / rejected(거절) / question(대기 — 추가 문의) / no_reply(무응답) 로 분류하고,
  - 분류에 맞는 후속 대응 이메일 초안(follow_up)을 생성한다.

기존과 달리 에이전트가 web_search 도구를 갖는다: 업체가 행사 정보·학교 규모 등
사실 확인이 필요한 문의를 보냈을 때 스스로 검색해 근거 있는 답변 초안을 쓸 수 있다.
답장이 없으면 LLM 을 호출하지 않고 결정적으로 no_reply 처리한다 (비용 절약).
후속 메일은 '초안'으로만 만들어 사람이 확인 후 보낸다.
"""
from pydantic import BaseModel, Field

from agents.google_clients import fetch_latest_reply
from agents.react import finalize, run_react
from agents.tools import make_research_tool


class ReplyDecision(BaseModel):
    status: str = Field(description="accepted | rejected | question 중 하나")
    follow_up: str = Field(
        description="후속으로 보낼 이메일 본문 초안. "
        "거절이면 정중한 감사 인사, 대기(추가 문의)면 답변, 수락이면 다음 절차 안내."
    )


_SYSTEM = """당신은 대학 총학생회 대외협력국의 답장 대응 에이전트입니다.
업체로부터 받은 답장을 분류하고 후속 이메일 초안을 작성합니다.

- 분류: accepted(협찬 수락) / rejected(거절) / question(대기 — 추가 문의)
- 후속 초안: 한국어 존댓말. 수락이면 다음 절차 안내, 거절이면 정중한 감사 인사,
  대기(추가 문의)면 질문에 대한 답변.
- 업체가 사실 확인이 필요한 것(행사 정보, 학교 규모, 일정 등)을 물었고 당신이
  모르는 내용이면 web_search 로 조사한 뒤 답하세요. 확인 못 한 사실은 지어내지
  말고 '담당자 확인 후 회신드리겠습니다'로 처리하세요.
- 조사가 필요 없으면 도구를 쓰지 말고 바로 판단하세요."""


def classify_reply(company_name, reply_text, sender_name, llm, context=""):
    """답장 본문 하나를 분류하고 후속 초안을 만든다 (UI/CLI/그래프 공용).

    context 에 행사 정보 등을 주면 문의 답변에 참고한다.
    """
    sender = sender_name or "성균관대학교 총학생회 대외협력국"
    user = (
        f"'{company_name}' 업체로부터 받은 답장입니다:\n---\n{reply_text}\n---\n"
        + (f"\n[행사/제안 정보]\n{context}\n" if context else "")
        + f"\n보내는 사람: {sender}\n"
        "이 답장을 분류하고 후속 이메일 본문 초안을 작성하세요."
    )
    messages, _ = run_react(llm, [make_research_tool()], _SYSTEM, user, max_steps=3)
    decision = finalize(
        llm, ReplyDecision, messages,
        "판단을 종료합니다. 분류(status)와 후속 이메일 초안(follow_up)을 "
        "지정된 형식으로만 출력하세요.")
    status = decision.status if decision.status in ("accepted", "rejected", "question") \
        else "question"
    return {"reply_status": status, "follow_up": decision.follow_up}


def run_reply_agent(creds, company, sender_name, llm, context=""):
    """업체 한 건의 답장을 분류하고 후속 초안을 반환.

    한 주소로 여러 업체에 보낸 테스트 상황에서도 업체별 답장을 구분하도록
    제목(업체명)으로 답장을 좁혀 조회한다.
    """
    reply_text = fetch_latest_reply(creds, company["email"],
                                    subject_query=company.get("name"))
    if not reply_text:
        return {"reply_status": "no_reply", "follow_up": ""}
    return classify_reply(company["name"], reply_text, sender_name, llm,
                          context=context)
