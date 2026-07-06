"""LangGraph 오케스트레이션 (후속 대응 탭용 답장 그래프).

automail 대시보드의 '후속 대응' 탭이 build_reply_graph(START → reply → END)를
호출해 업체별 답장을 일괄 분류한다. 검색·작성·발송을 포함한 전체 캠페인 실행은
agents/supervisor.py 의 멀티 에이전트 그래프('자동 실행' 탭)가 담당한다.

노드는 state["companies"] 를 순회하며 자기 단계 필드를 채우고,
한 업체에서 오류가 나도 배치 전체가 멈추지 않도록 try/except 로 감싼다.
"""
from langgraph.graph import END, START, StateGraph

from agents.reply_agent import run_reply_agent
from agents.state import WorkflowState


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
