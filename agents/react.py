"""경량 ReAct 루프 실행기.

LLM 이 도구를 스스로 골라 호출하는 에이전트 루프. LLM 이 도구 호출을 멈추면
(= 최종 판단에 도달하면) 종료한다. 프레임워크 의존을 줄이기 위해 직접 구현:
- max_steps 로 반복(비용) 상한을 강제
- 도구 예외는 관찰(ToolMessage)로 되돌려 에이전트가 전략을 바꾸게 함
- trace 에 도구 호출 내역을 남겨 관측 가능성(검색어 체인 표시 등)을 유지
"""
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


def _short(v, n=80):
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"


def run_react(llm, tools, system_prompt, user_prompt, max_steps=6, on_event=None):
    """(messages, trace) 반환. trace 는 '도구(인자)' 문자열 목록."""
    llm_tools = llm.bind_tools(tools)
    tool_map = {t.name: t for t in tools}
    messages = [SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt)]
    trace = []
    for _ in range(max_steps):
        ai = llm_tools.invoke(messages)
        messages.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            break
        for tc in calls:
            name, args = tc.get("name", ""), tc.get("args", {}) or {}
            arg_str = ", ".join(f"{k}={_short(v, 60)}" for k, v in args.items())
            trace.append(f"{name}({arg_str})")
            if on_event:
                on_event(f"  · 도구 호출: {name}({arg_str})")
            try:
                if name in tool_map:
                    out = tool_map[name].invoke(args)
                else:
                    out = f"알 수 없는 도구: {name}"
            except Exception as e:  # noqa: BLE001 - 도구 실패는 관찰로 반환
                out = f"도구 실행 오류: {e}"
            messages.append(ToolMessage(content=str(out)[:12000],
                                        tool_call_id=tc.get("id", "")))
    return messages, trace


def finalize(llm, schema, messages, instruction):
    """에이전트 대화 기록에 최종 지시를 붙여 구조화 출력을 뽑는다."""
    return llm.with_structured_output(schema).invoke(
        messages + [HumanMessage(content=instruction)])
