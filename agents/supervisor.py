"""Supervisor 에이전트 — 멀티 에이전트 오케스트레이션의 관리자.

기존 graph.py 의 고정 파이프라인과 달리, supervisor(LLM)가 매 턴 업체별 상태를
보고 '다음에 어떤 에이전트가 무엇을 할지'를 동적으로 결정한다:

  supervisor ─┬→ search  (검색 에이전트: 이메일 미발견/미검증 업체 재탐색, 지시 가능)
              ├→ write   (작성 에이전트: 초안 생성/재작성)
              ├→ approve_send (사람 승인 interrupt → 승인분만 발송 도구 실행)
              └→ finish

답장 확인/후속 대응은 supervisor 범위가 아니다 — 답장은 실행이 끝난 며칠 뒤에
도착하므로, 대시보드 '후속 대응' 탭에서 사람이 답장 에이전트를 실행한다.

안전장치(결정적 코드 — LLM 판단에 맡기지 않음):
- 발송은 반드시 interrupt() 로 사람 승인을 거친 업체만 실행
- REVIEW/미검증 이메일 업체는 발송 후보에서 제외하지 않되 승인 화면에 등급 표시
- 검색 재시도 상한(MAX_SEARCH_ATTEMPTS), supervisor 턴 상한(MAX_STEPS)
- supervisor 가 고른 대상이 액션 전제조건에 안 맞으면 코드가 필터링,
  유효 대상이 없으면 결정적 폴백 규칙으로 다음 액션 결정
"""
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from agents.google_clients import send_email
from agents.search_agent import run_search_agent
from agents.state import WorkflowState
from agents.writer_agent import run_writer_agent

MAX_STEPS = 12            # supervisor 판단 턴 상한
MAX_SEARCH_ATTEMPTS = 2   # 업체당 검색 시도 상한


class SupervisorDecision(BaseModel):
    """supervisor 의 다음 행동 결정 (구조화 출력)."""
    action: Literal["search", "write", "approve_send", "finish"] = Field(
        description="다음 행동. search=이메일 탐색, write=초안 작성, "
                    "approve_send=사람 승인 후 발송, finish=종료")
    targets: list[int] = Field(default_factory=list,
                               description="처리할 업체 인덱스 목록 (표의 # 값)")
    instruction: str = Field(default="", description="서브에이전트에 전달할 추가 지시. "
                             "예: 검색 재시도라면 이전 실패 원인을 반영한 새 탐색 관점.")
    reason: str = Field(default="", description="이 결정의 근거 (로그용, 한국어 한 문장)")


_SYSTEM = """당신은 협찬 제안 메일 자동화 팀의 관리자(supervisor)입니다.
팀원: 검색 에이전트(이메일 탐색), 작성 에이전트(제안 초안), 발송 도구(사람 승인 필수).

업체별 상태표를 보고 다음 행동 하나를 결정하세요.

행동별 전제조건과 용도:
- search: 이메일이 없거나(NONE) 미검증(REVIEW)인 업체의 재탐색. 시도 {max_attempts}회
  초과 업체는 불가. 이메일 미발견 업체는 시도 횟수가 남아 있는 한 포기하지 말고
  '재검색을 우선' 고려하세요. 재시도 시 instruction 에 실패 사유를 반영한 다른
  접근을 구체적으로 지시하세요 (예: 다른 키워드, 통합 웹 검색, 사업자 정보
  디렉토리, 프랜차이즈라면 본사 홈페이지).
- write: 이메일이 있고 초안이 없는 업체의 제안 초안 생성.
- approve_send: 초안이 준비됐고 아직 발송 안 된 업체를 사람에게 승인받아 발송.
  건너뛴(skipped) 업체는 다시 올리지 마세요.
- finish: 더 진행할 유효한 작업이 없으면 종료. 발송(또는 건너뛰기)까지 끝났으면
  종료하세요. 답장 확인은 이 시스템 범위 밖입니다(며칠 뒤 사람이 후속 대응 탭에서
  진행). 검색 실패가 상한에 달했고 나머지가 모두 처리됐으면 미련 없이 종료하세요.

원칙:
- 같은 행동을 같은 대상에 의미 없이 반복하지 마세요 (직전 행동이 표시됩니다).
- 발송 여부의 최종 결정권은 사람에게 있습니다. 당신은 승인 요청까지만 합니다.
- REVIEW 등급 이메일은 발송 대상에 올리되, 사람이 등급을 보고 판단하게 됩니다."""


def _status_table(companies):
    rows = []
    for i, c in enumerate(companies):
        if not c.get("name"):
            continue
        rows.append(
            f"#{i} {c['name']} | 이메일:{c.get('email') or '없음'}"
            f"({c.get('tier') or '-'}) | 검색시도:{c.get('search_attempts', 0)}"
            f" | 초안:{'있음' if c.get('subject') else '없음'}"
            f" | 발송:{'완료' if c.get('sent') else '안됨'}"
            f"{'(건너뜀)' if c.get('skipped') else ''}"
            + (f" | 실패사유:{(c.get('verify_reason') or '')[:60]}"
               if not c.get('email') else "")
        )
    return "\n".join(rows)


def _valid_targets(action, targets, companies):
    """액션 전제조건에 맞게 대상 인덱스를 결정적으로 필터링."""
    ok = []
    for i in targets:
        if not isinstance(i, int) or not (0 <= i < len(companies)):
            continue
        c = companies[i]
        if not c.get("name"):
            continue
        if action == "search":
            if (not c.get("verified")) and c.get("search_attempts", 0) < MAX_SEARCH_ATTEMPTS:
                ok.append(i)
        elif action == "write":
            if c.get("email") and not c.get("sent"):
                ok.append(i)
        elif action == "approve_send":
            if c.get("subject") and c.get("email") and not c.get("sent") \
                    and not c.get("skipped"):
                ok.append(i)
    return ok


def _fallback(companies):
    """LLM 결정이 무효일 때의 결정적 다음 단계 (기존 파이프라인 순서)."""
    idx = [i for i, c in enumerate(companies) if c.get("name")]
    t = [i for i in idx if not companies[i].get("email")
         and companies[i].get("search_attempts", 0) < MAX_SEARCH_ATTEMPTS]
    if t:
        return "search", t
    t = [i for i in idx if companies[i].get("email")
         and not companies[i].get("subject") and not companies[i].get("sent")]
    if t:
        return "write", t
    t = [i for i in idx if companies[i].get("subject") and companies[i].get("email")
         and not companies[i].get("sent") and not companies[i].get("skipped")]
    if t:
        return "approve_send", t
    return "finish", []


def build_supervisor_graph(creds, llm, on_event=print):
    """supervisor 멀티 에이전트 그래프를 컴파일해 반환 (checkpointer 포함)."""

    def supervisor_node(state: WorkflowState):
        companies = state["companies"]
        steps = state.get("steps", 0) + 1
        if steps > MAX_STEPS:
            on_event(f"[관리자] 판단 턴 상한({MAX_STEPS}) 도달 → 종료")
            return Command(goto=END, update={"steps": steps})
        prompt = (
            _SYSTEM.replace("{max_attempts}", str(MAX_SEARCH_ATTEMPTS))
            + f"\n\n[업체 상태표]\n{_status_table(companies)}"
            + f"\n\n[직전 행동] {state.get('last_action') or '(없음 — 시작)'}"
            + "\n\n다음 행동을 지정된 형식으로만 출력하세요."
        )
        try:
            d = llm.with_structured_output(SupervisorDecision).invoke(prompt)
            action, targets, instruction = d.action, d.targets, d.instruction
            reason = d.reason
        except Exception as e:  # noqa: BLE001 - LLM 실패 시 결정적 폴백
            action, targets, instruction, reason = "", [], "", f"LLM 오류 폴백: {e}"
        valid = _valid_targets(action, targets, companies) if action else []
        if action != "finish" and not valid:
            fb_action, fb_targets = _fallback(companies)
            if action:
                on_event(f"[관리자] '{action}' 대상 없음 → 폴백: {fb_action}")
            action, valid = fb_action, fb_targets
        if action != "finish" and not valid:
            action = "finish"
        on_event(f"[관리자] 결정: {action}"
                 + (f" (대상 {len(valid)}곳)" if valid else "")
                 + (f" — {reason}" if reason else ""))
        if action == "finish":
            return Command(goto=END, update={"steps": steps, "last_action": "finish"})
        return Command(goto=action, update={
            "steps": steps,
            "last_action": f"{action} → {[companies[i]['name'] for i in valid]}",
            "_targets": valid, "_instruction": instruction,
        })

    def search_node(state: WorkflowState):
        companies = state["companies"]
        instruction = state.get("_instruction", "")
        for i in state.get("_targets", []):
            c = companies[i]
            c["search_attempts"] = c.get("search_attempts", 0) + 1
            try:
                c.update(run_search_agent(c["name"], llm, c.get("hint", ""),
                                          instruction=instruction, on_event=on_event))
                on_event(f"[검색] {c['name']} → {c.get('email') or '미발견'} "
                         f"({c.get('tier')})")
            except Exception as e:  # noqa: BLE001 - 한 업체 오류로 배치 중단 방지
                c["verified"] = False
                c["verify_reason"] = f"검색 오류: {e}"
                on_event(f"[검색] {c['name']} 오류: {e}")
        return Command(goto="supervisor", update={"companies": companies})

    def write_node(state: WorkflowState):
        companies = state["companies"]
        for i in state.get("_targets", []):
            c = companies[i]
            try:
                proposal = run_writer_agent(
                    c, state.get("sponsor_items", ""), state.get("sender_name", ""),
                    llm, campus=state.get("campus", ""),
                    name=state.get("writer_name", ""),
                    event=state.get("event_name", ""),
                    phone=state.get("writer_phone", ""),
                    event_date=state.get("event_date", ""))
                c["subject"], c["body"] = proposal.subject, proposal.body
                on_event(f"[작성] {c['name']} 초안 완료")
            except Exception as e:  # noqa: BLE001
                on_event(f"[작성] {c['name']} 오류: {e}")
        return Command(goto="supervisor", update={"companies": companies})

    def approve_send_node(state: WorkflowState):
        """사람 승인(interrupt) 후, 승인된 업체만 발송 도구 실행."""
        companies = state["companies"]
        targets = state.get("_targets", [])
        drafts = [{"i": i, "name": companies[i]["name"],
                   "email": companies[i].get("email", ""),
                   "tier": companies[i].get("tier", ""),
                   "subject": companies[i].get("subject", ""),
                   "body": companies[i].get("body", "")} for i in targets]
        test_email = (state.get("test_email") or "").strip()
        # ---- 여기서 그래프가 멈추고 사람의 결정을 기다린다 ----
        decision = interrupt({"type": "approval", "drafts": drafts,
                              "test_email": test_email}) or {}
        approved = {int(a["i"]): a for a in decision.get("approved", [])}
        for i in targets:
            c = companies[i]
            if i not in approved:
                c["skipped"] = True
                on_event(f"[발송] {c['name']} — 사람이 건너뜀")
                continue
            a = approved[i]
            subject = (a.get("subject") or c.get("subject", "")).strip()
            body = (a.get("body") or c.get("body", "")).strip()
            c["subject"], c["body"] = subject, body
            recipient = test_email or c.get("email", "")
            try:
                result = send_email(
                    creds, recipient, subject, body,
                    sender_name=state.get("sender_name", ""),
                    sender_email=state.get("sender_email", ""),
                    attachment_path=state.get("attachment_path") or None,
                    label=state.get("label") or None)
                c["message_id"] = result["id"]
                c["sent"] = True
                on_event(f"[발송] {c['name']} → {recipient} 전송")
            except Exception as e:  # noqa: BLE001
                c["sent"] = False
                on_event(f"[발송] {c['name']} 실패: {e}")
        return Command(goto="supervisor", update={"companies": companies})

    graph = StateGraph(WorkflowState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("search", search_node)
    graph.add_node("write", write_node)
    graph.add_node("approve_send", approve_send_node)
    graph.add_edge(START, "supervisor")
    return graph.compile(checkpointer=InMemorySaver())
