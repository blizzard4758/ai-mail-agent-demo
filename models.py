"""数据模型层。

所有跨模块传递的结构化数据都用 Pydantic 定义，并带 ``schema_version`` 字段：
后续要新增字段或改结构时，模型本身记录了版本号，便于「可更新性」（兼容旧数据、
做迁移、写测试断言）。

设计原则：
- 工具之间只传这些模型，绝不传裸 dict（避免「加一个字段要改 N 处」）。
- 模型即文档：字段含义、默认值一目了然，面试时可直接打开讲解。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

# 全局数据模型版本。任何破坏性变更 +1，配套写迁移逻辑即可。
SCHEMA_VERSION = 1


class EmailMessage(BaseModel):
    """一封邮件（已经是解析好的扁平结构，方便直接进 DataFrame / Excel）。"""

    schema_version: int = SCHEMA_VERSION
    uid: str                                   # 全局唯一 id（IMAP 里对应 UID）
    subject: str = ""
    from_addr: str = ""                        # 发件人邮箱
    from_name: str = ""                        # 发件人显示名（可能为空）
    to_addrs: List[str] = Field(default_factory=list)
    date: Optional[str] = None                 # ISO 8601 字符串，如 2026-08-30T09:12:00
    body_text: str = ""                        # 纯文本正文（HTML 已转文本、已去噪）
    body_html: str = ""
    has_attachment: bool = False
    attachment_names: List[str] = Field(default_factory=list)
    is_read: bool = False
    folder: str = "INBOX"
    labels: List[str] = Field(default_factory=list)

    @property
    def snippet(self) -> str:
        """列表/UI 展示用的正文预览。"""
        t = (self.body_text or "").strip().replace("\n", " ")
        return t[:120] + ("…" if len(t) > 120 else "")

    @property
    def sender(self) -> str:
        return self.from_name or self.from_addr


class EmailSummary(BaseModel):
    """单封邮件的 AI 摘要结果。"""

    schema_version: int = SCHEMA_VERSION
    email_uid: str
    subject: str = ""
    summary: str = ""                          # 一两句话的核心内容
    category: str = "other"                   # invoice/meeting/notification/personal/spam/other
    priority: str = "normal"                   # high/normal/low
    action_needed: bool = False                # 是否需要人跟进（回复/付款/参会…）
    key_points: List[str] = Field(default_factory=list)


class SpamResult(BaseModel):
    """垃圾邮件过滤结果（规则层，确定性、可解释）。"""

    schema_version: int = SCHEMA_VERSION
    spam_uids: List[str] = Field(default_factory=list)
    total_checked: int = 0
    rule_hits: dict = Field(default_factory=dict)   # {"关键词命中": 3, "可疑发件域": 1, ...}


class ExportResult(BaseModel):
    schema_version: int = SCHEMA_VERSION
    path: str = ""
    rows: int = 0
    ok: bool = False


class SendResult(BaseModel):
    """发信结果。dry_run=True 时 sent=False 但 draft=True，表示「已生成待确认」。"""

    schema_version: int = SCHEMA_VERSION
    sent: bool = False
    draft: bool = False
    to: str = ""
    subject: str = ""
    message: str = ""
