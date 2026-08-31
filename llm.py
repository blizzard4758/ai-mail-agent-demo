"""LLM 层。

设计要点（面试常问）：
1. **可插拔 Provider**：业务代码只依赖 ``LLMClient`` 协议，不关心背后是 Ollama 还是
   Mock。切模型/换厂商只改 config，不动 agent 与 tools。
2. **离线优先**：``MockLLM`` 不联网、确定性强，保证 ``--demo`` 断网也能完整演示。
   它的"思考"是脚本化的，但 **ReAct 循环、工具派发、结果聚合、Trace 落盘全是真的**——
   也就是架构真实、可讲解，只是把"大脑"换成了确定性路由。
3. **JSON 守卫**：摘要这类结构化输出，用 Pydantic 校验 + 最多 2 次重试；最终仍失败则
   降级到规则摘要，绝不把脏数据写进 Excel。

两种能力：
- ``call_tools``：Agent 编排时用，决定"下一步调哪个工具、参数是什么"。
- ``summarize``：单封邮件产出结构化摘要（Agent 在需要时通过 summarize_email 工具调用）。
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from models import EmailMessage, EmailSummary

from config import settings


# --------------------------------------------------------------------------- #
# 决策结构：LLM 告诉 Agent "下一步干什么"
# --------------------------------------------------------------------------- #
@dataclass
class ToolDecision:
    action: str                                  # "call" 或 "finish"
    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    answer: str = ""

    @classmethod
    def call(cls, name: str, args: Dict[str, Any]) -> "ToolDecision":
        return cls(action="call", name=name, args=args)

    @classmethod
    def finish(cls, answer: str) -> "ToolDecision":
        return cls(action="finish", answer=answer)


# --------------------------------------------------------------------------- #
# Provider 协议
# --------------------------------------------------------------------------- #
class LLMClient:
    def call_tools(self, messages: List[Dict[str, str]], tools: List[dict],
                   state: Dict[str, Any]) -> ToolDecision:
        raise NotImplementedError

    def summarize(self, email: EmailMessage) -> EmailSummary:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# 规则摘要（Mock 用，也作为真实 LLM 失败时的降级）
# --------------------------------------------------------------------------- #
_SPAM_WORDS = ["免费", "中奖", "发票", "代开", "赌博", "优惠", "点击领取", "限时", "返利"]
_URGENT_WORDS = ["紧急", "尽快", " today", "立刻", "逾期", "到期", "确认付款"]
_CATEGORY_RULES = [
    ("invoice", ["发票", "付款", "账单", "invoice", "payment", "账单"]),
    ("meeting", ["会议", "参会", "周会", "meeting", "面试", "约"]),
    ("notification", ["通知", "提醒", "系统", "验证码", "notification", "alert"]),
    ("personal", ["你好", "在吗", "周末", "朋友", "hi ", "hello"]),
]


def heuristic_summary(email: EmailMessage) -> EmailSummary:
    text = (email.body_text or "").strip()
    first_line = text.split("\n")[0][:80] if text else "(无正文)"
    cat = "other"
    for c, kws in _CATEGORY_RULES:
        if any(k in (email.subject + text).lower() for k in kws):
            cat = c
            break
    priority = "high" if any(w in (email.subject + text).lower() for w in _URGENT_WORDS) else "normal"
    action = cat in ("invoice", "meeting") or priority == "high"
    points = [ln.strip() for ln in text.split("\n") if ln.strip()][:3]
    return EmailSummary(
        email_uid=email.uid,
        subject=email.subject,
        summary=f"【{email.sender}】{first_line}",
        category=cat,
        priority=priority,
        action_needed=action,
        key_points=points,
    )


# --------------------------------------------------------------------------- #
# MockLLM：确定性、离线、驱动演示
# --------------------------------------------------------------------------- #
class MockLLM(LLMClient):
    """不联网的"假大脑"。根据指令 + 已执行步骤 + 共享 state，决定下一步调哪个工具。"""

    def summarize(self, email: EmailMessage) -> EmailSummary:
        return heuristic_summary(email)

    def call_tools(self, messages, tools, state) -> ToolDecision:
        instruction = _first_user_text(messages)
        invoked = _invoked_tool_names(messages)
        low = instruction.lower()

        # 1) 永远先取邮件语料
        if "search_emails" not in invoked:
            return ToolDecision.call("search_emails", {
                "keyword": _extract_keyword(instruction),
                "unread_only": ("未读" in instruction) or ("unread" in low),
                "limit": 20,
            })

        # 2) 垃圾过滤
        if ("垃圾" in instruction or "spam" in low) and "filter_spam" not in invoked:
            return ToolDecision.call("filter_spam", {"strategy": "rule"})

        # 3) 摘要（取第一封，演示用）
        if ("摘要" in instruction or "总结" in instruction or "summary" in low or "summar" in low):
            uids = state.get("search_uids") or []
            done = state.get("last_summary_uid")
            if uids and uids[0] != done:
                return ToolDecision.call("summarize_email", {"email_uid": uids[0]})

        # 4) 导出 Excel
        if ("导出" in instruction or "excel" in low or "export" in low) and "export_excel" not in invoked:
            return ToolDecision.call("export_excel", {
                "email_uids": state.get("search_uids") or [],
                "path": "out/emails.xlsx",
            })

        # 5) 发送（默认草稿态）
        if ("发送" in instruction or "send" in low) and "send_email" not in invoked:
            return ToolDecision.call("send_email", {
                "to": state.get("last_send_to", "boss@example.com"),
                "subject": "邮件跟进",
                "body": "您好，这是通过 AI Mail Agent 自动生成的跟进邮件。",
                "draft": True,
            })

        return ToolDecision.finish(_mock_final_answer(instruction, state))


def _first_user_text(messages: List[Dict[str, str]]) -> str:
    for m in messages:
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


def _invoked_tool_names(messages: List[Dict[str, str]]) -> set:
    names = set()
    for m in messages:
        if m.get("role") == "assistant" and "tool_calls" in m:
            for tc in m["tool_calls"]:
                names.add(tc.get("name", ""))
    return names


def _extract_keyword(instruction: str) -> str:
    # 支持 "包含XXX" / "关于XXX" / 引号包裹
    m = re.search(r"(?:包含|关于|keyword[=: ])\s*[\"']?([^\"']+)[\"']?", instruction)
    if m:
        return m.group(1).strip().strip("\"'")
    return ""


def _mock_final_answer(instruction: str, state: Dict[str, Any]) -> str:
    n = len(state.get("search_uids") or [])
    return (f"已完成指令：「{instruction}」。\n"
            f"共检索 {n} 封邮件，已执行过滤/摘要/导出等步骤，"
            f"详细过程见左侧执行轨迹（Trace）。")


# --------------------------------------------------------------------------- #
# OllamaLLM：真实可调用的本地大模型（OpenAI 兼容 /v1 接口）
# --------------------------------------------------------------------------- #
class OllamaLLM(LLMClient):
    def __init__(self):
        self.base = settings.ollama_base_url.rstrip("/")   # http://localhost:11434
        self.model = settings.model

    # ---- 底层：OpenAI 兼容 chat/completions ----
    def _chat(self, messages, tools=None, tool_choice=None) -> dict:
        payload: Dict[str, Any] = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def call_tools(self, messages, tools, state) -> ToolDecision:
        data = self._chat(messages, tools=tools, tool_choice="auto")
        msg = data["choices"][0]["message"]
        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]["function"]
            args = _safe_json_loads(tc.get("arguments", "{}"))
            return ToolDecision.call(tc["name"], args)
        # 没有工具调用 -> 视为最终回答
        return ToolDecision.finish(msg.get("content", "") or "(无回答)")

    def summarize(self, email: EmailMessage) -> EmailSummary:
        prompt = (
            "请用 JSON 总结下面这封邮件，字段："
            '{"email_uid","subject","summary","category","priority","action_needed","key_points"}。'
            "category 取值 invoice/meeting/notification/personal/spam/other；"
            "priority 取值 high/normal/low；action_needed 为布尔；"
            "key_points 为字符串数组（最多 3 条）。只输出 JSON。\n\n"
            f"主题：{email.subject}\n发件人：{email.sender}\n正文：{email.body_text[:1500]}"
        )
        for attempt in range(2):
            try:
                raw = self._chat(
                    [{"role": "user", "content": prompt}],
                )["choices"][0]["message"].get("content", "")
                return EmailSummary.model_validate(_safe_json_loads(raw))
            except Exception:
                if attempt == 1:
                    return heuristic_summary(email)   # 降级
        return heuristic_summary(email)


# --------------------------------------------------------------------------- #
# JSON 守卫 & 工厂
# --------------------------------------------------------------------------- #
def _safe_json_loads(s: str) -> Any:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        return json.loads(m.group(0)) if m else {}


def get_llm() -> LLMClient:
    """按配置返回 LLM 实现。DEMO_MODE 下强制 Mock，保证离线可演示。"""
    if settings.demo_mode:
        return MockLLM()
    if settings.llm_provider == "ollama":
        return OllamaLLM()
    # 预留：未来可加 OpenAI / 通义等
    return MockLLM()
