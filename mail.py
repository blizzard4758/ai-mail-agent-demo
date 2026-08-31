"""邮件源层（数据源可插拔）。

- ``FixtureStore``：**默认数据源**，直接加载 ``data/fixtures.json``（离线假邮件），
  零网络、零依赖，保证演示任何时候都能跑。
- ``IMAPStore``：真实邮箱实现，用标准库 ``imaplib`` + ``email`` 解析 MIME，
  不引入第三方解析库。接真实账号只需填 .env 并关闭 DEMO_MODE（演示开关）。
- ``SMTPClient``：真实发信，标准库 ``smtplib``，支持 465 SSL 与其余端口 STARTTLS。
- ``get_mail_store()``：按 config 的 ``DEMO_MODE`` 切换；业务代码只依赖 ``MailStore`` 协议。
"""
from __future__ import annotations

import imaplib
import json
import os
import smtplib
from email.message import EmailMessage as MimeMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
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
# 真实 IMAP 数据源
# --------------------------------------------------------------------------- #
class IMAPStore(MailStore):
    """用标准库解析，不依赖第三方 MIME 库。

    使用方式：在 .env 填 MAIL_HOST/MAIL_USER/MAIL_PASS（国内邮箱用「授权码/应用专用密码」，
    不是登录密码），并设 DEMO_MODE=false（或 UI 取消勾选「演示模式」）。
    """

    def __init__(self):
        self.host = settings.imap_host
        self.port = settings.imap_port
        self.user = settings.imap_user
        self.password = settings.imap_pass

    # ---- 底层连接 ----
    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        conn.login(self.user, self.password)
        conn.select("INBOX")
        return conn

    # ---- 单封抓取：FLAGS（已读态）+ BODY.PEEK[]（正文，不改已读态）----
    def _fetch_one(self, conn: imaplib.IMAP4_SSL, uid: bytes):
        seen = False
        try:
            typ, fd = conn.uid("fetch", uid, "(FLAGS)")
            if fd and fd[0]:
                blob = fd[0][0] if isinstance(fd[0], tuple) else fd[0]
                if b"\\Seen" in blob or b"\\SEEN" in blob:
                    seen = True
        except Exception:
            pass
        typ, bd = conn.uid("fetch", uid, "(BODY.PEEK[])")
        raw = None
        if bd and bd[0]:
            raw = bd[0][1] if isinstance(bd[0], tuple) else bd[0]
        return raw, seen

    # ---- MIME 解析：补齐发件人名/收件人/日期/附件/已读态 ----
    @staticmethod
    def _parse(uid, raw, seen: bool = False) -> EmailMessage:
        import email
        if raw is None:
            return EmailMessage(uid=_b(uid), is_read=seen)
        msg = email.message_from_bytes(raw)
        subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
        from_name, from_addr = parseaddr(msg.get("From", ""))
        to_addrs = [a for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])) if a]
        date = None
        try:
            dt = parsedate_to_datetime(msg.get("Date"))
            if dt is not None:
                date = dt.isoformat()
        except Exception:
            pass
        body, has_attachment, attachment_names = "", False, []
        if msg.is_multipart():
            for part in msg.walk():
                disp = part.get_content_disposition()
                if disp == "attachment":
                    has_attachment = True
                    fn = part.get_filename()
                    if fn:
                        attachment_names.append(fn)
                if part.get_content_type() == "text/plain" and not body:
                    try:
                        body = part.get_payload(decode=True).decode("utf-8", "ignore")
                    except Exception:
                        body = part.get_payload(decode=True) or b""
        else:
            try:
                body = msg.get_payload(decode=True).decode("utf-8", "ignore")
            except Exception:
                body = msg.get_payload(decode=True) or b""
        if isinstance(body, bytes):
            body = body.decode("utf-8", "ignore")
        return EmailMessage(
            uid=_b(uid),
            subject=subject,
            from_addr=from_addr,
            from_name=from_name,
            to_addrs=to_addrs,
            date=date,
            body_text=body[:8000],        # 截断，控制 LLM token
            has_attachment=has_attachment,
            attachment_names=attachment_names,
            is_read=seen,
        )

    # ---- 检索：支持关键词（主题/发件人/正文）与未读过滤 ----
    def search(self, keyword: str = "", unread_only: bool = False,
               limit: int = 20) -> List[EmailMessage]:
        conn = self._connect()
        try:
            criteria: list = []
            if unread_only:
                criteria.append("UNSEEN")
            kw = keyword.strip()
            if kw:
                # OR(SUBJECT kw, OR(FROM kw, TEXT kw))
                criteria += ["OR", "SUBJECT", kw, "OR", "FROM", kw, "TEXT", kw]
                charset = "UTF8"
            else:
                charset = None
            if not criteria:
                criteria = ["ALL"]
            try:
                typ, data = conn.search(charset, *criteria)
            except Exception:
                # 部分服务器不支持 UTF8 检索，退回纯 ASCII 关键词；再失败则取全部
                try:
                    typ, data = conn.search(None, *criteria)
                except Exception:
                    typ, data = conn.search(None, "ALL")
            uids = data[0].split() if data and data[0] else []
            uids = uids[-limit:]
            out = []
            for uid in uids:
                raw, seen = self._fetch_one(conn, uid)
                out.append(self._parse(uid, raw, seen))
            return out
        finally:
            conn.logout()

    def all(self) -> List[EmailMessage]:
        return self.search(limit=50)

    def get(self, uid: str) -> Optional[EmailMessage]:
        conn = self._connect()
        try:
            raw, seen = self._fetch_one(conn, str(uid).encode())
            return self._parse(str(uid), raw, seen)
        finally:
            conn.logout()

    def mark_read(self, uid: str) -> None:
        conn = self._connect()
        try:
            conn.uid("store", str(uid).encode(), "+FLAGS", "\\Seen")
        finally:
            conn.logout()


def _b(uid) -> str:
    return uid.decode() if isinstance(uid, bytes) else str(uid)


# --------------------------------------------------------------------------- #
# 真实 SMTP 发信（安全闸门在 tools.send_email 里：dry_run + 白名单）
# --------------------------------------------------------------------------- #
class SMTPClient:
    """标准库发信，支持 465(SSL) 与其余端口(STARTTLS)。复用 IMAP 的账号/授权码。"""

    def __init__(self):
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.user = settings.smtp_user or settings.imap_user
        self.password = settings.smtp_pass or settings.imap_pass
        self.from_addr = self.user

    def send(self, to: str, subject: str, body: str) -> tuple:
        """返回 (ok: bool, message: str)。"""
        m = MimeMessage()
        m["From"] = self.from_addr
        m["To"] = to
        m["Subject"] = subject
        m.set_content(body)
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as s:
                s.login(self.user, self.password)
                s.send_message(m)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(self.user, self.password)
                s.send_message(m)
        return True, f"已发送至 {to}"


def get_mail_store() -> MailStore:
    if settings.demo_mode:
        return FixtureStore()
    return IMAPStore()
