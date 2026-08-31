"""ReAct Agent 内核（手写，~120 行，不依赖 LangChain）。

为什么手写而不是用框架：面试里「ReAct 循环你会写吗」是高频题。手写能让每一步
（思考→选工具→执行→观察→再思考）完全透明、可断点、可讲解，也更好测。

主循环：
    Thought → Action(tool+args) → Observation(tool result) → ... → Finish

护栏：
- ``max_steps``：防止模型死循环（如反复调同一个工具）。
- 共享 ``state``：工具把关键产物写回 state（如搜索得到的 uid 列表），下一步决策可读。
- ``Trace``：每一步结构化落盘，UI 可逐帧回放 Agent 在干嘛（面试最值钱的能力）。

两种决策来源：
- MockLLM：离线确定性路由（演示用）。
- OllamaLLM：真实 function-calling（接本地大模型用）。
两者都实现同一个 ``call_tools`` 契约，Agent 内核一行不改。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from llm import LLMClient, ToolDecision, get_llm
from tools import dispatch, get_tool_schemas

MAX_STEPS = 8


@dataclass
class TraceStep:
    index: int
    thought: str
    tool: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    observation: str = ""


@dataclass
class AgentResult:
    answer: str
    steps: List[TraceStep] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)


_SYSTEM_PROMPT = (
    "你是一个邮件处理 Agent。你可以调用工具来：检索邮件、生成摘要、过滤垃圾邮件、"
    "导出 Excel、发送邮件。请根据用户指令，逐步调用最合适的工具，直到任务完成，"
    "然后给出简洁的最终回答。一次只调用一个工具。"
)


def _update_state(state: Dict[str, Any], name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if "uids" in data:
        state["search_uids"] = data["uids"]
    if "uid" in data:
        state["last_summary_uid"] = data["uid"]
    if "spam_uids" in data:
        state["spam_uids"] = data["spam_uids"]
    if "path" in data:
        state["export_path"] = data["path"]
    if "to" in data:
        state["last_send_to"] = data["to"]
    return state


class MailAgent:
    def __init__(self, llm: LLMClient = None):
        self.llm = llm or get_llm()
        self.tools = get_tool_schemas()

    def run(self, instruction: str) -> AgentResult:
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]
        state: Dict[str, Any] = {}
        trace: List[TraceStep] = []

        for step in range(MAX_STEPS):
            try:
                decision: ToolDecision = self.llm.call_tools(messages, self.tools, state)
            except Exception as exc:                       # LLM 调用失败也不崩
                return AgentResult(answer=f"LLM 调用异常：{exc}", steps=trace, state=state)

            if decision.action == "finish":
                trace.append(TraceStep(index=step, thought="任务完成，输出最终回答。"))
                return AgentResult(answer=decision.answer, steps=trace, state=state)

            # 执行工具（带异常隔离，单工具失败不影响整体）
            thought = f"决定调用工具 `{decision.name}`，参数：{decision.args}"
            try:
                result = dispatch(decision.name, decision.args)
            except Exception as exc:
                result = type("R", (), {"content": f"工具执行出错：{exc}", "data": {}})()
            state = _update_state(state, decision.name, getattr(result, "data", {}))
            trace.append(TraceStep(index=step, thought=thought,
                                   tool=decision.name, args=decision.args,
                                   observation=result.content))

            # 把工具调用与结果回写对话，供下一轮决策（含真实 LLM）使用
            messages.append({"role": "assistant", "tool_calls": [
                {"name": decision.name, "args": decision.args}]})
            messages.append({"role": "tool", "name": decision.name,
                             "content": result.content})

        return AgentResult(answer="已达到最大步数仍未完成，请简化指令或提高 MAX_STEPS。",
                           steps=trace, state=state)


def run_agent(instruction: str) -> AgentResult:
    """便捷入口：用全局配置构造 Agent 并跑一条指令。"""
    return MailAgent().run(instruction)
