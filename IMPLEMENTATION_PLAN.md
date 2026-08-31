# AIMailAgent 实施方案（基于 AI.docx 框架升级版）

> 定位：可写进简历、可现场 Demo、可开源到 GitHub 的 LLM Agent 项目。
> 目标工期：约 6~7 个有效工作日（每天 4~6 小时）。
> 原则：**先跑通离线闭环，再接真实依赖**；每一阶段结束都有一个可演示、可 commit 的产物。

---

## 0. 对原框架的评审结论

原框架（AI.docx）功能清单和分层是对的，但作为面试项目有 10 处会在追问时露怯的地方：

| # | 问题 | 风险 | 本方案改法 |
|---|---|---|---|
| 1 | 工具间传裸 dict，无数据模型 | 加一个字段要改 N 处 | Pydantic 定义领域模型，带 `schema_version` |
| 2 | 主框架写 LangChain | 追问 ReAct 原理答不上、调试黑盒 | **手写 ReAct 内核为主**，LangChain 仅作可选适配 |
| 3 | 靠文本解析 Thought/Action | 格式一崩就挂 | Function Calling 为主 + 文本解析兜底 |
| 4 | LLM 输出 JSON 不校验 | 脏数据直进 Excel | Pydantic 校验 + 自动重试 + 失败降级 |
| 5 | 逐封串行调 LLM | 100 封跑几分钟，现场冷场 | 批量打包 + 异步并发 + 磁盘缓存 + 正文截断 |
| 6 | 依赖真实邮箱和联网 LLM | 现场没网 = 演示失败 | **`--demo` 离线模式**，本地 fixture 跑通全链路 |
| 7 | 自动发邮件无闸门 | 面试官必问"发错人怎么办" | dry_run 默认开 + Human-in-the-loop 确认 + 白名单 |
| 8 | Agent 是黑盒 | 讲不清执行链路 | 结构化 Trace 落盘 + UI 时间线回放 |
| 9 | 零测试 | 工程素养扣分 | pytest + mock IMAP + 离线 e2e + GitHub Actions |
| 10 | 只有"能做"没有"多好" | 缺量化结果 | 100 条标注集测垃圾邮件过滤准确率/召回率 |

**决定成败的五项**：数据建模（1）、手写内核（2）、并发与缓存（5）、离线 Demo（6）、发信闸门（7）。

---

## 1. 目标架构

```
交互层      Gradio UI / CLI / FastAPI
              ↓
Agent 内核   ReAct 循环（Thought → Action → Observation → Final）
              ↓
工具注册表   插件化自动发现：检索 / 摘要 / 过滤 / 导出 / 发送
              ↓
基础设施层   IMAP·SMTP适配 | LLM Provider 抽象 | Excel | SQLite

横切能力     可观测 Trace  |  安全闸门(HITL)  |  缓存·重试·限流
```

设计约束：
- **依赖倒置**：Agent 只依赖 `Tool` 协议和 `LLMProvider` 协议，不依赖任何具体实现。
- **新增工具零改动主逻辑**：`@register_tool` 装饰器 + 包扫描自动注册。
- **任何外部 IO 可替换**：真实 IMAP ↔ 本地 fixture，真实 LLM ↔ MockLLM，靠配置切换。

---

## 2. 核心数据模型（这是"建模简单但完善可更新"的落点）

```python
# core/models.py
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"

class MailCategory(str, Enum):
    WORK = "work"           # 工作协作
    NOTIFICATION = "notification"  # 系统通知
    PROMOTION = "promotion"        # 推广
    SPAM = "spam"                  # 垃圾/诈骗
    PERSONAL = "personal"
    UNKNOWN = "unknown"

class EmailMessage(BaseModel):
    """邮件原始记录 —— 与来源无关（IMAP / fixture / mbox 皆可产出）"""
    schema_version: str = SCHEMA_VERSION
    uid: str                      # 邮箱内唯一 ID
    message_id: str               # RFC822 Message-ID，去重主键
    sender: str
    sender_name: Optional[str] = None
    recipients: list[str] = Field(default_factory=list)
    subject: str
    date: datetime
    body_text: str                # 已解 MIME + HTML 转文本 + 去噪
    body_truncated: bool = False  # 是否因超长被截断
    has_attachment: bool = False
    attachment_names: list[str] = Field(default_factory=list)
    raw_size: int = 0
    is_read: bool = False

    @field_validator("body_text")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()[:20000]

class EmailSummary(BaseModel):
    """LLM 产出的结构化摘要"""
    schema_version: str = SCHEMA_VERSION
    message_id: str
    summary: str = Field(..., max_length=200)   # 一句话摘要
    key_points: list[str] = Field(default_factory=list, max_length=5)
    action_required: bool = False               # 是否需要我回复/处理
    intent: str = "other"        # meeting|approval|question|invoice|other
    urgency: int = Field(default=1, ge=1, le=5)
    deadline: Optional[str] = None              # ISO date 字符串，无则 null
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

class SpamVerdict(BaseModel):
    message_id: str
    is_spam: bool
    category: MailCategory = MailCategory.UNKNOWN
    reason: str = Field(default="", max_length=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

class ToolResult(BaseModel):
    """所有工具的统一返回，Agent 只看这一种结构"""
    ok: bool
    tool: str
    data: dict = Field(default_factory=dict)   # 供下游工具消费的结构化数据
    message: str = ""                          # 给 LLM 看的自然语言摘要
    error: Optional[str] = None
    elapsed_ms: int = 0

class TraceStep(BaseModel):
    """ReAct 每一步，落盘成 JSONL，UI 直接回放"""
    run_id: str
    step: int
    thought: Optional[str] = None
    action: Optional[str] = None
    action_input: Optional[dict] = None
    observation: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_ms: int = 0
    ts: datetime = Field(default_factory=datetime.now)
```

**为什么这样建模**：`EmailMessage` 是"事实"，`EmailSummary` / `SpamVerdict` 是"派生的 AI 判断"。三者用 `message_id` 关联，所有字段可空可缺省，`schema_version` 为将来迁移留口子。工具之间只传这些模型，不传 dict。

---

## 3. 目录结构（升级版）

```
AIMailAgent/
├── core/
│   ├── models.py            # 上节全部数据模型
│   ├── config.py            # pydantic-settings 读 .env，单例
│   └── logging_conf.py      # structlog / loguru 统一日志
├── llm/
│   ├── base.py              # LLMProvider 协议（chat / chat_json）
│   ├── openai_provider.py   # OpenAI 兼容（含 DeepSeek/通义/Qwen/vLLM）
│   ├── ollama_provider.py   # 走 Ollama 的 /v1 兼容端点
│   ├── mock_provider.py     # 离线规则式假 LLM（demo 模式）
│   └── guard.py             # JSON 解析 + Pydantic 校验 + 重试 + 降级
├── mail/
│   ├── imap_client.py       # 真 IMAP：搜索/抓取/解析
│   ├── smtp_client.py       # 真 SMTP：发送
│   ├── parser.py            # MIME 解码、HTML→文本、编码嗅探、签名去噪
│   ├── fixture_client.py    # 本地假邮箱（data/fixtures/*.json）
│   └── protocol.py          # MailSource / MailSink 协议
├── tools/
│   ├── registry.py          # @register_tool + 包扫描 + schema 生成
│   ├── base.py              # Tool 协议 + ToolResult
│   ├── retrieve.py
│   ├── summarize.py
│   ├── spam_filter.py
│   ├── export_excel.py
│   └── send_mail.py
├── agent/
│   ├── react.py             # 手写 ReAct 内核（核心，约 200 行）
│   ├── planner.py           # 可选：一次性规划模式（对照实验）
│   ├── trace.py             # Trace 落盘 JSONL + 读取
│   └── prompts.py           # 系统提示词 / 摘要 / 过滤 / 写信 prompt
├── storage/
│   ├── db.py                # SQLite：邮件、摘要、缓存
│   └── repo.py              # 仓储层（upsert / 查询 / 去重）
├── ui/
│   ├── gradio_app.py        # 主界面：指令输入 + Trace 时间线 + 下载
│   └── components.py        # Trace 时间线渲染
├── data/
│   ├── fixtures/            # 离线演示用假邮件（30 封，含 8 封垃圾）
│   └── eval/spam_eval.jsonl # 100 条人工标注评测集
├── tests/
│   ├── test_parser.py
│   ├── test_tools.py
│   ├── test_react_offline.py
│   └── conftest.py
├── docs/
│   ├── ARCHITECTURE.md
│   └── DEMO_SCRIPT.md       # 面试 5 分钟演示脚本
├── out/                     # Excel 输出（gitignore）
├── .env.example
├── .gitignore
├── requirements.txt
├── Makefile
├── main.py                  # CLI 入口
└── README.md
```

---

## 4. 九阶段实施路线

### P0 地基（0.5 天）
- [ ] `git init` + 建仓（先本地，P8 再推 GitHub）+ `.gitignore`（`.env`、`out/`、`*.db`、`__pycache__`）
- [ ] venv：`python -m venv .venv && pip install -r requirements.txt`
- [ ] `core/config.py`：pydantic-settings 读 `.env`，字段含 `MAIL_HOST/PORT/USER/PASS`、`LLM_PROVIDER/BASE_URL/API_KEY/MODEL`、`DEMO_MODE`、`DRY_RUN`、`MAX_STEPS`、`CONCURRENCY`
- [ ] `core/logging_conf.py`：统一日志（含 run_id）
- [ ] `.env.example` 写全注释
- **验收**：`python -c "from core.config import settings; print(settings.model_dump())"` 正常输出，且 `.env` 未被 git 跟踪
- **commit**: `chore: 初始化工程结构与配置层`

### P1 数据建模 + 存储（0.5 天）
- [ ] `core/models.py` 全部模型（第 2 节）
- [ ] `storage/db.py`：SQLite 建表（`emails`、`summaries`、`verdicts`、`llm_cache`、`runs`），`message_id` 唯一索引
- [ ] `storage/repo.py`：`upsert_email` / `get_by_ids` / `cache_get` / `cache_put`
- **验收**：写个临时脚本灌 10 条假数据，能 upsert 去重、能按 id 取回
- **commit**: `feat(core): 领域模型与 SQLite 仓储层`

### P2 邮件通信层 + 离线数据源（1 天）★关键
- [ ] `mail/protocol.py`：定义 `MailSource`（`search(...) -> list[EmailMessage]`）/ `MailSink`（`send(...)`）协议
- [ ] `mail/parser.py` —— **这里是 IMAP 实战的坑集中区**，必须处理：
  - `email.message_from_bytes` + `policy=default`
  - 多部分递归 `walk()`，取 `text/plain` 优先，回退 `text/html`
  - 编码：先 `get_content_charset()`，失败用 `charset_normalizer` 嗅探，再失败 `errors="replace"`
  - 传输编码：`base64` / `quoted-printable` 自动解（`get_payload(decode=True)`）
  - 主题解码：`decode_header` + `make_header`，处理 `=?UTF-8?B?...?=`
  - HTML → 文本：`<script>/<style>` 移除，`bleach` 或正则剥标签，`html.unescape`
  - 去噪：删除引用历史（`> ` 开头、Outlook 分隔线）、签名块、超长空行
  - 正文 > 8000 字符截断，置 `body_truncated=True`
- [ ] `mail/imap_client.py`：`SINCE` / `UNSEEN` / `FROM` 搜索，分批 fetch（`BODY.PEEK[]` 不改已读态），超时与重连
- [ ] `mail/fixture_client.py`：读 `data/fixtures/*.json` 产出同样的 `EmailMessage`
- [ ] `data/fixtures/`：手写 30 封（22 正常 + 8 垃圾），覆盖中文/英文/HTML/纯文本/带附件/带引用历史 6 种形态
- **验收**：`DEMO_MODE=true python main.py "取最近7天未读邮件"` 打印 30 封；`pytest tests/test_parser.py` 全绿
- **commit**: `feat(mail): IMAP/SMTP 适配、MIME 解析与离线数据源`

### P3 LLM 抽象层（0.5 天）
- [ ] `llm/base.py`：`class LLMProvider(Protocol)`，方法 `chat(messages) -> str` 与 `chat_json(messages, schema) -> dict`
- [ ] `openai_provider.py`：用官方 SDK，**所有兼容 OpenAI 的服务只是换 base_url**（DeepSeek / 通义 / vLLM 同理）
- [ ] `ollama_provider.py`：走 `http://localhost:11434/v1`（Ollama 自带 OpenAI 兼容端点），一套代码通吃
- [ ] `mock_provider.py`：规则式假 LLM（关键词命中 → 返回固定摘要/判定），**零网络、零延迟、可复现**
- [ ] `llm/guard.py`：`parse_json_loose()`（剥 ```json 围栏、截首个 `{` 到末个 `}`）+ Pydantic 校验 + 最多 2 次重试 + 失败返回降级对象（`confidence=0`）
- [ ] 磁盘缓存：key = `md5(prompt + model + schema_version)`，命中直接返回
- [ ] 并发：`asyncio.Semaphore(CONCURRENCY)` + `tenacity` 指数退避重试
- **验收**：`LLM_PROVIDER=mock` 时全链路 0 网络请求；切 `ollama` 用 qwen2.5:7b 能出合法 JSON
- **commit**: `feat(llm): Provider 抽象、结构化输出守卫与缓存`

### P4 工具层 + 注册表（1 天）
- [ ] `tools/base.py`：`Tool` 协议 = `name` / `description` / `args_schema`(Pydantic) / `run(**kw) -> ToolResult`；`to_openai_schema()` 自动生成 JSON Schema
- [ ] `tools/registry.py`：
  ```python
  REGISTRY: dict[str, Tool] = {}
  def register_tool(cls):
      t = cls(); REGISTRY[t.name] = t; return cls
  def discover(package="tools"):   # pkgutil 扫描，import 即注册
  ```
- [ ] 五个工具：
  | 工具 | 入参 | 产出 |
  |---|---|---|
  | `retrieve_mail` | `days`, `unread_only`, `sender`, `keyword`, `limit` | `data.mail_ids`, 摘要统计 |
  | `filter_spam` | `mail_ids`, `threshold` | `kept_ids`, `dropped_ids`, 每条理由 |
  | `summarize_batch` | `mail_ids`, `concurrency` | `summaries[]`，落库 |
  | `export_excel` | `mail_ids`, `with_summary`, `filename` | 文件路径 |
  | `send_mail` | `to`, `subject`, `body`, `reply_to_id` | 发送回执；**受 DRY_RUN 与 HITL 约束** |
- [ ] 所有工具入参出参都是 Pydantic，**不做字符串拼接传参**
- [ ] `filter_spam` 采用两级：规则层（黑名单域名/高频营销词/无退订链接）先快速过，LLM 只判规则层不确定的那批 —— 省 token 且可解释
- **验收**：`python -c "from tools.registry import discover; print(list(discover()))"` 打印 5 个工具
- **commit**: `feat(tools): 插件化工具注册表与五个业务工具`

### P5 Agent 内核 ReAct（1 天）★核心
- [ ] `agent/react.py` 手写循环，约 200 行：
  ```python
  for step in range(1, settings.max_steps + 1):
      resp = llm.chat_json(messages, tools=schemas)   # 主路径：Function Calling
      if resp.get("final_answer"):
          return Final(...)
      act = parse_action(resp) or parse_action_text_fallback(resp)  # 兜底：正则抽 Action/Input
      if act.name not in REGISTRY:
          observation = f"未知工具 {act.name}，可用：{list(REGISTRY)}"   # 错误即反馈，不抛异常
      else:
          result = REGISTRY[act.name].run(**act.args)                  # HITL 在此拦截
          observation = result.message
      messages.append(observation_msg)     # 观察结果回灌
      trace.append(TraceStep(...))         # 每一步落盘
  return Final(answer="超过最大步数，已中止", trace=trace)
  ```
- [ ] 必须有的防护：`max_steps`（默认 8）、观察文本截断（≤1500 字符）、token 预算上限、工具异常转成 observation 而非崩溃
- [ ] `agent/prompts.py`：系统提示词强调"优先用工具、不要臆造邮件内容、参数必须来自用户指令或上一步观察"
- [ ] `agent/trace.py`：写 `out/traces/{run_id}.jsonl`
- [ ] `agent/planner.py`（加分项）：一次性规划 vs 逐步 ReAct 的对照开关，面试可讲"我对比过两种范式的失败率"
- **验收**：离线模式下跑三条指令（检索导出 / 发信 / 模糊指令"帮我看看有什么要紧事"）全部收敛且 Trace 完整
- **commit**: `feat(agent): 手写 ReAct 内核、工具调度与 Trace`

### P6 导出与 UI（1 天）
- [ ] `export_excel.py`：用 `openpyxl` 而非裸 `to_excel`
  - 表头冻结 + 加粗 + 底色；列宽自适应；长文本自动换行；`摘要`列 60 宽
  - 增加"统计"页：总数 / 垃圾占比 / 紧急度分布 / 待办数
  - 条件格式：`urgency>=4` 标红，`action_required=true` 加粗
- [ ] `ui/gradio_app.py`：三栏 —— 左侧指令输入 + 示例按钮；中间 Trace 时间线（每步 thought/action/observation，可折叠）；右侧结果表格 + Excel 下载
- [ ] `main.py` CLI：`--demo`、`--dry-run`、`--trace`
- [ ] `docs/DEMO_SCRIPT.md`：面试 5 分钟脚本
- **验收**：`python main.py --demo` 浏览器打开，一条指令走完四步，Excel 能下载且排版过得去
- **commit**: `feat(ui): Gradio 界面、Trace 时间线与 Excel 格式化导出`

### P7 质量保障（0.5 天）
- [ ] `tests/test_parser.py`：编码 / HTML / 引用历史 / 附件 6 类样本断言
- [ ] `tests/test_llm_guard.py`：非法 JSON、被围栏包裹、字段缺失三种输入的降级行为
- [ ] `tests/test_react_offline.py`：端到端跑三条指令，断言工具调用序列
- [ ] `data/eval/spam_eval.jsonl`：100 条人工标注（建议 60 正常 / 40 垃圾），跑出 precision / recall / F1 写进 README
- [ ] `.github/workflows/ci.yml`：`ruff` + `mypy`(可选) + `pytest`，**CI 全程 mock 模式不需任何密钥**
- **commit**: `test: 单元测试、离线端到端与垃圾邮件评测集`

### P8 发布与面试包装（0.5 天）
- [ ] README（第 6 节有骨架）
- [ ] 截图 3 张 + 一段 20 秒 GIF（用 ScreenToGif，录 demo 模式全过程）
- [ ] 开源体检：确认无密钥、无真实邮箱地址（fixture 里用 `example.com`）
- [ ] GitHub：建仓 → 推 → 打 `v1.0.0` release → topics（`llm-agent` `react` `python` `email`）
- **commit**: `docs: README、架构文档与演示素材`

---

## 5. 关键实现要点（踩坑备忘）

**IMAP**
- 用 `BODY.PEEK[]` 而不是 `RFC822`，避免抓取动作把邮件标记为已读。
- 国内邮箱（163/QQ）需先在设置里开启 IMAP/SMTP 并使用**应用专用密码**。
- QQ 邮箱 IMAP 需 `imap.qq.com:993`，且部分账号要求 `ID` 命令，失败时降级用 `imaplib.IMAP4_SSL` + 手动 `ID`。
- 搜索中文主题要用 `CHARSET UTF-8`，否则搜不到。

**LLM 结构化输出**
- 不要只靠 prompt 约束 JSON，一定要代码侧 `parse_json_loose` + Pydantic + 重试。
- 本地 7B 模型 JSON 合规率明显低于云端，把摘要字段压到 5 个以内、长度限制写进 prompt。
- 缓存 key 必须含 `schema_version` 和 `model`，否则改 prompt 后拿到旧结果。

**成本控制**
- 正文截断到 8000 字符（约 2k token），100 封邮件摘要成本可控。
- 垃圾过滤先过规则层，通常能挡掉 60%+ 明显广告，LLM 只处理剩余部分。

**发信安全**
- `DRY_RUN=true` 为默认值，只在明确传 `--send` 且通过确认才真发。
- 实现 HITL：工具返回 `need_confirm=True`，Agent 暂停，UI 弹出待确认邮件预览，用户点确认后才执行。
- 收件人白名单：非白名单域名强制确认。

**Gradio**
- Trace 用 `gr.JSON` 或自定义 HTML；长任务用 `yield` 逐步刷新，体验比一次性返回好得多。

---

## 6. README 骨架

```markdown
# AIMailAgent — 自然语言驱动的智能邮件处理 Agent

> 一句话：用自然语言下达指令，Agent 自动完成邮件检索、垃圾过滤、AI 摘要、Excel 导出与辅助发信。

## 30 秒看懂
[架构图] [演示 GIF]

## 为什么值得看
- 手写 ReAct 内核（core/agent/react.py，约 200 行），不依赖重型框架，可直接读源码理解 Agent 原理
- 插件化工具注册表：新增一个工具只需一个文件 + 一个装饰器，主逻辑零改动
- 完整可观测性：每一步 Thought/Action/Observation 落盘 JSONL，UI 时间线回放
- 离线可复现：`--demo` 模式无需邮箱、无需联网即可跑通全链路
- 安全护栏：发信默认 dry-run + 人工确认 + 白名单
- 有量化结果：垃圾邮件过滤在 100 条标注集上 P/R/F1 = x/xx/xx

## 快速开始
git clone ... && cd AIMailAgent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py --demo          # 离线模式，零配置立即体验
# 真实模式：cp .env.example .env 并填写，然后 python main.py

## 架构
见 docs/ARCHITECTURE.md

## 效果
| 指标 | 值 |
|---|---|
| 垃圾邮件过滤 Precision / Recall / F1 | 0.xx / 0.xx / 0.xx |
| 100 封邮件摘要耗时（8 并发） | xx s |
| 端到端指令成功率（20 条测试指令） | xx / 20 |

## 路线图
- [ ] 附件解析（PDF/Word 抽取）
- [ ] 邮件向量库 + RAG 问答
- [ ] 定时扫描与增量同步
- [ ] 多邮箱账号管理
- [ ] 自动标签分类
```

---

## 7. 简历与面试

**简历一栏（详细版）**
> **AIMailAgent · 智能邮件处理 Agent** ｜ Python · LLM Agent · ReAct · IMAP/SMTP · Gradio
> 独立设计并实现 LLM Agent 邮件自动化系统。手写 ReAct 推理内核实现多步任务自动拆解与工具调度；将检索、摘要、垃圾识别、Excel 导出、发信封装为插件化工具，新增能力零改动主逻辑；通过 Function Calling + Pydantic 校验 + 重试降级保障结构化输出稳定性；使用批量并发与磁盘缓存将 100 封邮件摘要耗时从 x 分钟降至 x 秒；设计 Human-in-the-loop 发信确认与 dry-run 护栏；基于 SQLite 与 JSONL Trace 实现全链路可观测与结果复现。配套 100 条标注评测集与完整单元测试，离线 Demo 模式可零配置复现，项目开源至 GitHub。

**高频追问与回答要点**
| 问题 | 答法 |
|---|---|
| 为什么不用 LangChain？ | 用框架的 agent 是黑盒，追问原理答不上；自写内核能讲清每一步，也方便做 Trace 和护栏。保留了一层适配接口，随时可换。 |
| LLM 输出不合法怎么办？ | 四层：宽松解析 → Pydantic 校验 → 带错误信息的二次重试 → 降级默认值并标记 confidence=0，导出时该行标灰。 |
| Agent 死循环/乱调工具怎么办？ | max_steps 硬上限 + 观察结果截断 + 未知工具名回灌可用列表 + token 预算，超限时返回已完成的中间结果而非报错。 |
| 误发邮件怎么办？ | 默认 dry-run；真实发送需 UI 二次确认；收件人白名单外强制确认；所有发送写审计日志。 |
| 成本高吗？ | 规则层前置过滤 + 正文截断 + 并发 + 缓存，100 封约 x 分钱。 |
| 怎么扩展新能力？ | 新建一个 tools/xxx.py，继承 BaseTool 加 @register_tool，重启即生效，主逻辑不动。 |
| 有什么不足？ | 目前单账号、摘要质量依赖模型能力、垃圾过滤缺少真实大规模数据验证 —— 这三点是下一步方向。 |

---

## 8. 依赖清单（requirements.txt）

```
pydantic>=2.7
pydantic-settings>=2.3
openai>=1.30
pandas>=2.2
openpyxl>=3.1
gradio>=4.40
python-dotenv>=1.0
tenacity>=8.3
charset-normalizer>=3.3
structlog>=24.1
# 开发
pytest>=8.2
pytest-asyncio>=0.23
ruff>=0.5
```

---

## 9. 轻量落地版（工期最短、难度最低，推荐首选）

> 本节针对"项目要简单、功能完整、工期尽量短、降低实施难度"的需求，在 §0~§8 基础上做减法。**亮点叙事一个不丢**（手写 ReAct、插件化、离线 demo、安全闸门），只砍重基建。

### 9.1 结论
- **简单吗？** 简单。单仓库、单进程、无数据库、无微服务，本质是"一个 ReAct 循环 + 五个函数 + 一个 Gradio 界面"。
- **功能完整吗？** 在面试语境下完整：检索 / 摘要 / 过滤 / 导出 / 发信五件全有 + 自然语言入口 + 可演示。附件解析、RAG、定时任务明确列为"后续优化"，不计入本期。
- **难度尖峰在哪？** 只有两处：MIME 解析、真 IMAP/SMTP。用现成库 + 默认离线 fixture 即可抹平。

### 9.2 与原升级版差异（砍重基建，省 ~3.8 天）

| 项 | 原升级版 | 轻量版 | 省 |
|---|---|---|---|
| 持久化 | SQLite + 仓储层 | 内存 list + `lru_cache` | 0.5天 |
| 邮件解析 | 手写 MIME parser | 用 `mailparser` 库一行解 | 0.5天 |
| 真实 IMAP | 默认接入 | 默认 fixture 离线，IMAP 做成可选开关 | 1天 + 去风险 |
| 工具注册 | 装饰器 + 包扫描 | 单文件 `REGISTRY` 字典 | 0.3天 |
| LLM | Ollama + OpenAI + mock | Ollama + mock 两档 | 0.3天 |
| Excel | openpyxl 条件格式 | `pandas.to_excel` 基础版 | 0.3天 |
| UI | 三栏时间线组件 | 单栏 + 步骤文本框 | 0.3天 |
| 测试/CI | pytest + e2e + Actions | 2 个冒烟测试 | 0.3天 |

合计省 ~3.8 天 → **总工期降到约 2.5~3 天**。

### 9.3 扁平目录（约 11 个文件）

```
AIMailAgent/
├── models.py        # EmailMessage / EmailSummary（简易，dataclass 或 pydantic）
├── config.py        # os.getenv 读 .env
├── llm.py           # LLMProvider：Ollama + mock，含 JSON 守卫与重试
├── mail.py          # 离线 fixture 加载 + 可选 IMAP/SMTP（try/except 包裹）
├── tools.py         # 5 个工具 + REGISTRY 字典
├── agent.py         # ReAct 循环（约 150 行）
├── prompts.py
├── app.py           # Gradio 界面 + CLI 入口
├── data/fixtures.json   # 30 封离线假邮件
├── .env.example
├── requirements.txt
└── README.md
```

### 9.4 轻量版四阶段

- **P-A 地基（0.5 天）**：`git init`、`.gitignore`（`.env`/`out/`）、`requirements.txt`（pydantic, mailparser, pandas, openpyxl, gradio, tenacity, python-dotenv）、`config.py`、`data/fixtures.json`（手写 30 封，22 正常 + 8 垃圾，覆盖中文/英文/HTML/纯文本/带附件名/带引用历史）
- **P-B 内核（1 天）**：`models.py` → `llm.py`（Ollama `/v1` + `mock`，宽松 JSON 解析 + Pydantic 校验 + 重试降级）→ `mail.py`（fixture 优先，IMAP 写占位函数并 `try/except`）→ `tools.py`（5 个工具 + `REGISTRY`）→ `agent.py`（ReAct ~150 行，max_steps、观察截断、未知工具回灌、Trace 落盘 JSONL）
- **P-C 界面（0.5 天）**：`app.py` Gradio 单栏：指令输入 + 示例按钮 → 逐步打印 Thought/Action/Observation → 结果表格 + Excel 下载；`--demo` 默认开
- **P-D 收尾（0.5 天）**：`README.md`（架构图占位 + 演示 GIF + 效果数字）、GitHub 发布（打 `v1.0.0`）、2 个冒烟测试（`test_mock_llm.py`/`test_react_demo.py`）

### 9.5 降难度清单（照做即可）

1. 解析用 `mailparser.parse_from_bytes`，编码 / HTML / 附件一行搞定，**不要手写 MIME parser**。
2. 全程 `--demo`：先让 30 封假邮件跑通全链路，再碰真 IMAP；真 IMAP 只写一个函数并用 `try/except` 包裹，demo 阶段完全不依赖它。
3. 不引 SQLite，邮件与摘要放内存 list，导出直接 `pandas` 写出；缓存用 `functools.lru_cache`。
4. 工具用 `REGISTRY = {"retrieve": fn, ...}` 字典，新增工具 = 加一个函数 + 一个键值，照样能讲"可插拔、主逻辑零改动"。
5. LLM 只接 Ollama（本地离线最稳），mock 用于无网演示；OpenAI 留一个 `if` 分支即可，非必做。
6. Excel 用 `df.to_excel()`，演示够用；条件格式 / 统计页列为"后续优化"。
7. Gradio 用 `gr.Textbox` + `gr.DataFrame` + `gr.File`，**不要自定义时间线组件**。
8. 发信默认 `DRY_RUN=True`，真发送前用 `input("确认发送? y/N")` 或 Gradio 勾选框——闸门成本几乎为零，但面试必被问。

### 9.6 何时升级回完整版
出现任一诉求再补：接多个真实邮箱、留存历史做 RAG、要定时任务、被多人使用（需并发/DB）。在此之前，轻量版已足够支撑简历与面试。
