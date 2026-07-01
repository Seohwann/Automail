"""LangGraph 오케스트레이션.

3개 에이전트(검색·작성·답장)를 LangGraph 그래프로 실행한다.

automail 대시보드는 단계별 그래프를 각 탭에서 호출한다:
  build_search_graph :  START → search → END   (검색 에이전트)
  build_write_graph  :  START → write  → END   (작성 에이전트)
  build_reply_graph  :  START → reply  → END   (답장 에이전트)

배치 파이프라인은 build_prepare_graph(검색+작성) / build_send_graph(발송+답장) 로 제공한다.

각 노드는 state["companies"] 를 순회하며 자기 단계 필드를 채우고,
한 업체에서 오류가 나도 배치 전체가 멈추지 않도록 try/except 로 감싼다.
search/write/reply 는 LLM 으로 판단하는 '에이전트' 노드, send 는 발송 도구 노드다.
"""
from langgraph.graph import END, START, StateGraph

from agents.google_clients import send_email
from agents.reply_agent import run_reply_agent
from agents.search_agent import run_search_agent
from agents.state import WorkflowState
from agents.writer_agent import run_writer_agent


def build_prepare_graph(llm, on_event=print):
    """검색 + 작성 단계 (외부 발송 없음). 업체별 시트 힌트(c['hint'])를 검색에 사용."""

    def search_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("name"):
                continue
            try:
                c.update(run_search_agent(c["name"], llm, c.get("hint", "")))
                on_event(f"[검색] {c['name']} → {c.get('email') or '미발견'} "
                         f"(검증:{c.get('verified')})")
            except Exception as e:  # noqa: BLE001 - 배치 중단 방지
                c["verified"] = False
                c["verify_reason"] = f"검색 오류: {e}"
                on_event(f"[검색] {c['name']} 오류: {e}")
        return {"companies": state["companies"]}

    def write_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("verified"):
                continue
            try:
                proposal = run_writer_agent(c, state["sponsor_items"],
                                            state.get("sender_name", ""), llm)
                c["subject"], c["body"] = proposal.subject, proposal.body
                on_event(f"[작성] {c['name']} 초안 완료")
            except Exception as e:  # noqa: BLE001
                on_event(f"[작성] {c['name']} 오류: {e}")
        return {"companies": state["companies"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("search", search_node)
    graph.add_node("write", write_node)
    graph.add_edge(START, "search")
    graph.add_edge("search", "write")
    graph.add_edge("write", END)
    return graph.compile()


def build_send_graph(creds, llm, attachment_path=None, label=None, on_event=print):
    """발송 + 답장 단계. 초안(subject/body)이 채워진 상태를 입력으로 받는다."""

    def send_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("subject"):
                continue
            try:
                result = send_email(
                    creds, c["email"], c["subject"], c["body"],
                    sender_name=state.get("sender_name", ""),
                    sender_email=state.get("sender_email", ""),
                    attachment_path=attachment_path, label=label,
                )
                c["message_id"] = result["id"]
                c["sent"] = True
                on_event(f"[발송] {c['name']} → {c['email']} 전송")
            except Exception as e:  # noqa: BLE001
                c["sent"] = False
                on_event(f"[발송] {c['name']} 실패: {e}")
        return {"companies": state["companies"]}

    def reply_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("sent"):
                continue
            try:
                c.update(run_reply_agent(creds, c, state.get("sender_name", ""), llm))
                on_event(f"[답장] {c['name']} → {c.get('reply_status')}")
            except Exception as e:  # noqa: BLE001
                on_event(f"[답장] {c['name']} 오류: {e}")
        return {"companies": state["companies"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("send", send_node)
    graph.add_node("reply", reply_node)
    graph.add_edge(START, "send")
    graph.add_edge("send", "reply")
    graph.add_edge("reply", END)
    return graph.compile()


# ---------- 단계별 그래프 (automail 대시보드 탭에서 호출) ----------

def build_search_graph(llm, on_event=lambda *a: None):
    """검색 단계 그래프 (START → search → END)."""

    def search_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("name"):
                continue
            try:
                c.update(run_search_agent(c["name"], llm, c.get("hint", "")))
                on_event(f"[검색] {c['name']} → {c.get('email') or '미발견'}")
            except Exception as e:  # noqa: BLE001
                c["verified"] = False
                c["verify_reason"] = f"검색 오류: {e}"
        return {"companies": state["companies"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("search", search_node)
    graph.add_edge(START, "search")
    graph.add_edge("search", END)
    return graph.compile()


def build_write_graph(llm, on_event=lambda *a: None):
    """작성 단계 그래프 (START → write → END). 설정값은 state 에서 읽는다."""

    def write_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("name") or not c.get("email"):
                continue
            try:
                proposal = run_writer_agent(
                    c, state.get("sponsor_items", ""), state.get("sender_name", ""), llm,
                    campus=state.get("campus", ""), name=state.get("writer_name", ""),
                    event=state.get("event_name", ""), phone=state.get("writer_phone", ""),
                    event_date=state.get("event_date", ""))
                c["subject"], c["body"] = proposal.subject, proposal.body
                on_event(f"[작성] {c['name']} 완료")
            except Exception as e:  # noqa: BLE001
                on_event(f"[작성] {c['name']} 오류: {e}")
        return {"companies": state["companies"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("write", write_node)
    graph.add_edge(START, "write")
    graph.add_edge("write", END)
    return graph.compile()


def build_reply_graph(creds, llm, on_event=lambda *a: None):
    """답장 단계 그래프 (START → reply → END). 업체별 답장을 분류한다."""

    def reply_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("name"):
                continue
            try:
                c.update(run_reply_agent(creds, c, state.get("sender_name", ""), llm))
                on_event(f"[답장] {c['name']} → {c.get('reply_status')}")
            except Exception as e:  # noqa: BLE001
                c["reply_status"] = "no_reply"
        return {"companies": state["companies"]}

    graph = StateGraph(WorkflowState)
    graph.add_node("reply", reply_node)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    return graph.compile()
