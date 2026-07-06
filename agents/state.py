"""워크플로우/에이전트 사이를 흐르는 상태 정의.

Company 한 건이 search → write → send → reply 를 거치며 필드가 누적된다.
supervisor 그래프는 여기에 시도 횟수·건너뛰기 같은 오케스트레이션 필드를 더 쓴다.
"""
from typing import TypedDict


class Company(TypedDict, total=False):
    name: str           # 업체명 (시트에서 읽음)
    hint: str           # 업종/키워드 힌트 (시트에서 읽음, 검색 모호성 제거용. URL 포함 가능)
    email: str          # 검색 에이전트가 채움
    tier: str           # 검색 등급: HIGH | REVIEW | NONE
    verified: bool      # 도메인 일치 검증 통과 여부
    verify_reason: str  # 검증 판단 근거
    query: str          # 검색 도구 호출 체인 (관측 가능성)
    info: str           # 검색 에이전트가 만든 업체 요약 (작성 에이전트가 참고)
    subject: str        # 작성 에이전트가 채움
    body: str           # 작성 에이전트가 채움
    sent: bool          # 발송 성공 여부
    message_id: str     # Gmail 메시지 ID
    reply_status: str   # 답장 분류: accepted | rejected | question | no_reply
    follow_up: str      # 답장 에이전트가 만든 후속 대응 초안
    # ---- supervisor 오케스트레이션 필드 ----
    search_attempts: int  # 검색 시도 횟수 (재시도 상한 통제)
    skipped: bool         # 사람이 발송 승인 단계에서 건너뛴 업체 (재승인 요청 금지)


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
    # ---- supervisor 오케스트레이션 필드 ----
    test_email: str        # 테스트 모드 수신 주소 (비어 있으면 실제 발송)
    attachment_path: str   # 제안서 PDF 경로
    label: str             # Gmail 라벨
    steps: int             # supervisor 판단 횟수 (무한 루프 방지)
    last_action: str       # 직전 supervisor 액션 (같은 액션 반복 감지)
    _targets: list[int]    # supervisor 가 이번 턴에 지정한 업체 인덱스
    _instruction: str      # supervisor 가 서브에이전트에 내린 추가 지시
