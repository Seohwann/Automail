"""LangGraph 오케스트레이션.

워크플로우를 두 단계로 나눈다 (초안을 구글 시트에 저장해두고 검토 후 보내기 위함):

  prepare 그래프:  START → search → write → END
  send 그래프:     START → send  → reply → END

prepare 가 끝나면 main 이 검색 이메일·제목·본문을 시트에 저장하고,
send 는 main 이 시트에서 읽어온 초안으로 상태를 채워 실행한다.

각 노드는 state["companies"] 를 순회하며 자기 단계 필드를 채우고,
한 업체에서 오류가 나도 배치 전체가 멈추지 않도록 try/except 로 감싼다.
"""
from langgraph.graph import END, START, StateGraph

from agents.reply_agent import run_reply_agent
from agents.search_agent import run_search_agent
from agents.sender_agent import send_one
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


def build_send_graph(creds, llm, attachment_path=None, on_event=print):
    """발송 + 답장 단계. 초안(subject/body)이 채워진 상태를 입력으로 받는다."""

    def send_node(state: WorkflowState):
        for c in state["companies"]:
            if not c.get("subject"):
                continue
            try:
                c["message_id"] = send_one(creds, c, state.get("sender_name", ""),
                                           state.get("sender_email", ""), attachment_path)
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
