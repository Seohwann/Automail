"""워크플로우에서 에이전트 사이를 흐르는 상태 정의.

Company 한 건이 search → write → send → reply 를 거치며 필드가 누적된다.
"""
from typing import TypedDict


class Company(TypedDict, total=False):
    name: str           # 업체명 (시트에서 읽음)
    hint: str           # 업종/키워드 힌트 (시트에서 읽음, 검색 모호성 제거용)
    email: str          # 검색 에이전트가 채움
    verified: bool      # 도메인 일치 검증 통과 여부
    verify_reason: str  # 검증 판단 근거
    info: str           # 검색 에이전트가 만든 업체 요약 (작성 에이전트가 참고)
    subject: str        # 작성 에이전트가 채움
    body: str           # 작성 에이전트가 채움
    sent: bool          # 발송 성공 여부
    message_id: str     # Gmail 메시지 ID
    reply_status: str   # 답장 분류: accepted | rejected | question | no_reply
    follow_up: str      # 답장 에이전트가 만든 후속 대응 초안


class WorkflowState(TypedDict, total=False):
    companies: list[Company]
    sponsor_items: str   # 제안 내용 (작성 에이전트 입력)
    sender_name: str
    sender_email: str
    campus: str          # 캠퍼스 (작성 에이전트 입력)
    writer_name: str     # 담당자 이름
    event_name: str      # 행사명
    writer_phone: str    # 담당자 연락처
    event_date: str      # 행사 일자 (YYYY-MM-DD)
