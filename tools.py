"""工具层（Agent 可调用的能力）。

**插件化设计**：所有工具登记在 ``REGISTRY`` 字典里。Agent 只遍历 REGISTRY 拿到
工具名、描述和参数 JSON Schema，再按 LLM 决策派发。要新增一个能力，只需在 REGISTRY
里加一项——这就是「可插拔 / 可更新」：不改动 Agent 内核一行代码。

每个工具函数返回 ``ToolResult``：
- ``content``：给 LLM 看的自然语言结果（驱动下一步推理）
- ``data``：结构化数据，Agent 用来更新共享 state（如搜索得到的 uid 列表）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from models import EmailMessage, EmailSummary, ExportResult, SendResult, SpamResult

from config import settings
from llm import get_llm
from mail import get_mail_store

# 垃圾邮件规则（确定性、可解释）
_SUSPICIOUS_DOMAINS = ("promo", "win", "lucky", "casino", "vip", "free")
_SPAM_KEYWORDS = ["免费", "中奖", "发票", "代开", "赌博", "优惠", "点击领取",
                  "限时", "返利", "贷款", "兼职日结", "无需还款"]


# --------------------------------------------------------------------------- #
# 工具返回结构
# --------------------------------------------------------------------------- #
@dataclass
class ToolResult:
    content: str
    data: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def search_emails(keyword: str = "", unread_only: bool = False, limit: int = 20) -> ToolResult:
    store = get_mail_store()
    emails = store.search(keyword=keyword, unread_only=unread_only, limit=limit)
    lines = [f"{e.uid:>3} | {'●' if not e.is_read else ' '} | {e.sender:<22} | {e.subject}"
             for e in emails]
    text = f"检索到 {len(emails)} 封邮件：\n" + "\n".join(lines)
    return ToolResult(content=text, data={"uids": [e.uid for e in emails]})


def summarize_email(email_uid: str) -> ToolResult:
    store = get_mail_store()
    email = store.get(email_uid)
    if not email:
        return ToolResult(content=f"未找到 uid={email_uid} 的邮件", data={})
    s: EmailSummary = get_llm().summarize(email)
    text = (f"摘要[{email.uid}] {s.subject}\n"
            f"  分类={s.category} 优先级={s.priority} 需跟进={'是' if s.action_needed else '否'}\n"
            f"  内容：{s.summary}\n"
            f"  要点：{'；'.join(s.key_points)}")
    return ToolResult(content=text, data={"uid": email_uid})


def filter_spam(strategy: str = "rule") -> ToolResult:
    store = get_mail_store()
    emails = store.all()
    spam_uids: List[str] = []
    rule_hits: Dict[str, int] = {"可疑发件域": 0, "垃圾关键词": 0, "可疑链接": 0}
    for e in emails:
        domain = e.from_addr.split("@")[-1].lower() if "@" in e.from_addr else ""
        hit = False
        if any(d in domain for d in _SUSPICIOUS_DOMAINS):
            rule_hits["可疑发件域"] += 1
            hit = True
        if any(k in (e.subject + e.body_text) for k in _SPAM_KEYWORDS):
            rule_hits["垃圾关键词"] += 1
            hit = True
        if "http" in e.body_text.lower() and ("点击" in e.body_text or "领取" in e.body_text):
            rule_hits["可疑链接"] += 1
            hit = True
        if hit:
            spam_uids.append(e.uid)
    res = SpamResult(spam_uids=spam_uids, total_checked=len(emails), rule_hits=rule_hits)
    text = (f"规则过滤完成：检查 {res.total_checked} 封，判定垃圾 {len(res.spam_uids)} 封。"
            f"\n命中统计：{res.rule_hits}\n垃圾 uid：{res.spam_uids}")
    return ToolResult(content=text, data={"spam_uids": spam_uids})


def export_excel(email_uids: List[str] = None, path: str = "out/emails.xlsx") -> ToolResult:
    import pandas as pd
    store = get_mail_store()
    emails = store.all()
    if email_uids:
        want = set(str(u) for u in email_uids)
        emails = [e for e in emails if e.uid in want]
    rows = [{
        "uid": e.uid, "date": e.date, "from": e.sender, "subject": e.subject,
        "snippet": e.snippet, "has_attachment": e.has_attachment, "is_read": e.is_read,
    } for e in emails]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    pd.DataFrame(rows).to_excel(path, index=False, engine="openpyxl")
    res = ExportResult(path=path, rows=len(rows), ok=True)
    return ToolResult(content=f"已导出 {res.rows} 行到 {path}", data={"path": path})


def send_email(to: str = "", subject: str = "", body: str = "", draft: bool = True) -> ToolResult:
    # 安全闸门 1：默认 dry_run / draft=True 只生成草稿不真正发送
    if settings.dry_run or draft:
        res = SendResult(sent=False, draft=True, to=to, subject=subject,
                         message="（草稿态，未实际发送）" + body)
        return ToolResult(
            content=f"[草稿] 收件人={to} 主题={subject} —— 因 DRY_RUN/draft 未实际发送。"
                    f"如需真发，关闭 DRY_RUN 并确认收件人白名单。",
            data={"to": to})
    # 安全闸门 2：收件人白名单拦截（防误发），非空时不在名单内一律拒绝
    if settings.send_whitelist and to not in settings.send_whitelist:
        res = SendResult(sent=False, draft=False, to=to, subject=subject,
                         message=f"收件人 {to} 不在白名单，已拦截（防误发）。白名单：{settings.send_whitelist}")
        return ToolResult(
            content=f"[已拦截] 收件人 {to} 不在 SEND_WHITELIST，未发送。请在 .env 把该地址加入白名单。",
            data={"to": to})
    # 真实发送：接 SMTP（见 .env：MAIL_SMTP_HOST/PORT/USER/PASS）
    try:
        from mail import SMTPClient
        ok, msg = SMTPClient().send(to, subject, body)
        res = SendResult(sent=ok, draft=False, to=to, subject=subject, message=msg)
        return ToolResult(content=f"[已发送] {msg}", data={"to": to})
    except Exception as e:
        res = SendResult(sent=False, draft=False, to=to, subject=subject,
                         message=f"发送失败：{e}")
        return ToolResult(content=f"[发送失败] {e}", data={"to": to})


# --------------------------------------------------------------------------- #
# 注册表（Agent 唯一依赖的工具清单）
# --------------------------------------------------------------------------- #
REGISTRY: Dict[str, Dict[str, Any]] = {
    "search_emails": {
        "description": "检索邮件。可按关键词/未读过滤，返回邮件列表（含 uid）。",
        "params": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词，留空表示全部"},
                "unread_only": {"type": "boolean", "description": "仅未读"},
                "limit": {"type": "integer", "description": "返回上限，默认20"},
            },
        },
        "func": search_emails,
    },
    "summarize_email": {
        "description": "对指定一封邮件生成结构化摘要（分类/优先级/要点）。",
        "params": {
            "type": "object",
            "properties": {"email_uid": {"type": "string", "description": "邮件 uid"}},
            "required": ["email_uid"],
        },
        "func": summarize_email,
    },
    "filter_spam": {
        "description": "用规则过滤垃圾邮件，返回垃圾 uid 列表与命中统计（可解释）。",
        "params": {
            "type": "object",
            "properties": {"strategy": {"type": "string", "description": "过滤策略，默认 rule"}},
        },
        "func": filter_spam,
    },
    "export_excel": {
        "description": "将邮件导出为 Excel 文件，返回路径与行数。",
        "params": {
            "type": "object",
            "properties": {
                "email_uids": {"type": "array", "items": {"type": "string"},
                               "description": "要导出的 uid 列表，空表示全部"},
                "path": {"type": "string", "description": "输出路径"},
            },
        },
        "func": export_excel,
    },
    "send_email": {
        "description": "发送邮件。默认 draft/dry_run，只生成草稿不真正发送。",
        "params": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人"},
                "subject": {"type": "string", "description": "主题"},
                "body": {"type": "string", "description": "正文"},
                "draft": {"type": "boolean", "description": "是否草稿态，默认 true"},
            },
            "required": ["to", "subject", "body"],
        },
        "func": send_email,
    },
}


def get_tool_schemas() -> List[dict]:
    """给 LLM 的 function-calling 用的 JSON Schema 列表。"""
    return [{
        "type": "function",
        "function": {
            "name": name,
            "description": meta["description"],
            "parameters": meta["params"],
        },
    } for name, meta in REGISTRY.items()]


def dispatch(name: str, args: Dict[str, Any]) -> ToolResult:
    if name not in REGISTRY:
        return ToolResult(content=f"未知工具：{name}", data={})
    return REGISTRY[name]["func"](**args)
