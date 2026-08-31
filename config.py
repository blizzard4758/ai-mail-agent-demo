"""集中配置层：从 .env 读取，全部带默认值，离线模式零配置即可运行。"""
from __future__ import annotations

import os

# 防御式导入：未安装 python-dotenv 时也不影响（默认即离线 mock 行为）
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# 运行模式
DEMO_MODE: bool = _as_bool(os.getenv("DEMO_MODE"), default=True)
DRY_RUN: bool = _as_bool(os.getenv("DRY_RUN"), default=True)

# LLM
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "ollama")
LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen2.5:7b")

# 真实邮箱（仅 DEMO_MODE=false 使用，不要用登录密码，用「授权码/应用专用密码」）
MAIL_HOST: str = os.getenv("MAIL_HOST", "")
MAIL_PORT: int = _as_int(os.getenv("MAIL_PORT"), 993)
MAIL_USER: str = os.getenv("MAIL_USER", "")
MAIL_PASS: str = os.getenv("MAIL_PASS", "")

# SMTP 发信（默认复用 IMAP 的账号/授权码；QQ: smtp.qq.com:465 SSL）
MAIL_SMTP_HOST: str = os.getenv("MAIL_SMTP_HOST", "") or MAIL_HOST
MAIL_SMTP_PORT: int = _as_int(os.getenv("MAIL_SMTP_PORT"), 465)
MAIL_SMTP_USER: str = os.getenv("MAIL_SMTP_USER", "") or MAIL_USER
MAIL_SMTP_PASS: str = os.getenv("MAIL_SMTP_PASS", "") or MAIL_PASS
# 发信收件人白名单（逗号分隔，如 a@qq.com,b@163.com）；非空时不在名单内一律拦截，防误发
SEND_WHITELIST: str = os.getenv("SEND_WHITELIST", "")

# Agent
MAX_STEPS: int = _as_int(os.getenv("MAX_STEPS"), 8)
CONCURRENCY: int = _as_int(os.getenv("CONCURRENCY"), 8)
OUT_DIR: str = os.getenv("OUT_DIR", "out")

# 输出目录确保存在
os.makedirs(OUT_DIR, exist_ok=True)

# 统一配置对象：业务代码通过 `from config import settings` 访问，字段命名贴近语义。
from types import SimpleNamespace

def _split_csv(value: str) -> list:
    return [x.strip() for x in str(value).split(",") if x.strip()]

settings = SimpleNamespace(
    demo_mode=DEMO_MODE,
    dry_run=DRY_RUN,
    llm_provider=LLM_PROVIDER,
    ollama_base_url=LLM_BASE_URL,
    model=LLM_MODEL,
    imap_host=MAIL_HOST,
    imap_port=MAIL_PORT,
    imap_user=MAIL_USER,
    imap_pass=MAIL_PASS,
    smtp_host=MAIL_SMTP_HOST,
    smtp_port=MAIL_SMTP_PORT,
    smtp_user=MAIL_SMTP_USER,
    smtp_pass=MAIL_SMTP_PASS,
    send_whitelist=_split_csv(SEND_WHITELIST),
    out_dir=OUT_DIR,
)


def summary() -> dict:
    """脱敏后的配置快照，便于启动时打印 / 调试。"""
    return {
        "DEMO_MODE": DEMO_MODE,
        "DRY_RUN": DRY_RUN,
        "LLM_PROVIDER": LLM_PROVIDER,
        "LLM_MODEL": LLM_MODEL,
        "MAIL_HOST": MAIL_HOST or "(未配置)",
        "MAX_STEPS": MAX_STEPS,
        "CONCURRENCY": CONCURRENCY,
        "OUT_DIR": OUT_DIR,
    }


if __name__ == "__main__":
    for k, v in summary().items():
        print(f"{k:14}= {v}")
