"""邮件源层（数据源可插拔）。

- ``FixtureStore``：**默认数据源**，直接加载 ``data/fixtures.json``（离线假邮件），
  零网络、零依赖，保证演示任何时候都能跑。
- ``IMAPStore``：真实邮箱占位实现，用标准库 ``imaplib`` + ``email`` 解析 MIME，
  不引入第三方解析库。**当前为可运行骨架**，接真实账号只需填 .env 并关闭 DEMO_MODE。
- ``get_mail_store()``：按 config 的 ``DEMO_MODE`` 切换；业务代码只依赖 ``MailStore`` 协议。
"""
from __future__ import annotations

import imaplib
import json
import os
from typing import List, Optional

from models import EmailMessage

from config import settings

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "data", "fixtures.json")


class MailStore:
    def all(self) -> List[EmailMessage]: ...
    def search(self, keyword: str = "", unread_only: bool = False,
               limit: int = 20) -> List[EmailMessage]: ...
    def get(self, uid: str) -> Optional[EmailMessage]: ...
    def mark_read(self, uid: str) -> None: ...


# --------------------------------------------------------------------------- #
# 离线假邮件数据源
# --------------------------------------------------------------------------- #
class FixtureStore(MailStore):
    def __init__(self, path: str = _FIXTURE_PATH):
        self._emails: List[EmailMessage] = self._load(path)

    @staticmethod
    def _load(path: str) -> List[EmailMessage]:
        raw = json.load(open(path, encoding="utf-8"))
        out: List[EmailMessage] = []
        for r in raw:
            out.append(EmailMessage(
                uid=str(r["uid"]),
                subject=r.get("subject", ""),
                from_addr=r.get("sender", ""),
                from_name=r.get("sender_name", ""),
                to_addrs=r.get("recipients", []) or [],
                date=r.get("date"),
                body_text=r.get("body_text", ""),
                has_attachment=bool(r.get("has_attachment", False)),
                attachment_names=r.get("attachment_names", []) or [],
                is_read=bool(r.get("is_read", False)),
            ))
        return out

    def all(self) -> List[EmailMessage]:
        return list(self._emails)

    def search(self, keyword: str = "", unread_only: bool = False,
               limit: int = 20) -> List[EmailMessage]:
        kw = keyword.strip().lower()
        res = self._emails
        if unread_only:
            res = [e for e in res if not e.is_read]
        if kw:
            res = [e for e in res
                   if kw in e.subject.lower() or kw in e.body_text.lower()
                   or kw in e.from_addr.lower() or kw in e.from_name.lower()]
        return res[:limit]

    def get(self, uid: str) -> Optional[EmailMessage]:
        return next((e for e in self._emails if e.uid == str(uid)), None)

    def mark_read(self, uid: str) -> None:
        e = self.get(uid)
        if e:
            e.is_read = True


# --------------------------------------------------------------------------- #
# 真实 IMAP 数据源（骨架，可更新）
# --------------------------------------------------------------------------- #
class IMAPStore(MailStore):
    """用标准库解析，不依赖第三方 MIME 库。

    使用方式：在 .env 填 IMAP_HOST/IMAP_USER/IMAP_PASS（国内邮箱用「应用专用密码」），
    并设 DEMO_MODE=false。当前实现读取 INBOX 最近 N 封并解析，扩展点已留好注释。
    """

    def __init__(self):
        self.host = settings.imap_host
        self.user = settings.imap_user
        self.password = settings.imap_pass

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.host, 993)
        conn.login(self.user, self.password)
        conn.select("INBOX")
        return conn

    @staticmethod
    def _parse(uid: bytes, raw: bytes) -> EmailMessage:
        import email
        msg = email.message_from_bytes(raw)
        subject = email.header.make_header(email.header.decode_header(msg.get("Subject", "")))
        from_ = msg.get("From", "")
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", "ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", "ignore")
        return EmailMessage(
            uid=uid.decode(),
            subject=str(subject),
            from_addr=from_,
            body_text=body[:8000],        # 截断，控制 LLM token
        )

    def search(self, keyword: str = "", unread_only: bool = False,
               limit: int = 20) -> List[EmailMessage]:
        conn = self._connect()
        # BODY.PEEK[] 读取但不改已读态
        typ, data = conn.search(None, "UNSEEN" if unread_only else "ALL")
        uids = data[0].split()[-limit:] if data and data[0] else []
        out = []
        for uid in uids:
            typ, d = conn.fetch(uid, "(BODY.PEEK[])")
            out.append(self._parse(uid, d[0][1]))
        conn.logout()
        return out

    def all(self) -> List[EmailMessage]:
        return self.search(limit=50)

    def get(self, uid: str) -> Optional[EmailMessage]:
        conn = self._connect()
        typ, d = conn.fetch(uid.encode(), "(BODY.PEEK[])")
        conn.logout()
        return self._parse(uid.encode(), d[0][1]) if d else None

    def mark_read(self, uid: str) -> None:
        conn = self._connect()
        conn.store(uid.encode(), "+FLAGS", "\\Seen")
        conn.logout()


def get_mail_store() -> MailStore:
    if settings.demo_mode:
        return FixtureStore()
    return IMAPStore()
