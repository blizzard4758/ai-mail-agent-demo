# 📧 AI Mail Agent · 邮件智能体

一个**功能完整、架构清晰、可离线演示**的邮件处理 Agent 小项目。基于**手写 ReAct 内核**
（不依赖 LangChain），把「检索邮件 / 提取摘要 / 过滤垃圾 / 导出 Excel / 发送邮件」五件
事，用自然语言指令串起来。

> 定位：能写进简历、能在面试现场断网演示的**完整可讲项目**，而不是玩具脚本。

---

## ✨ 为什么这个项目适合写进简历

| 面试官关注点 | 本项目怎么回答 |
|---|---|
| Agent 循环你会写吗？ | 手写 ReAct（`agent.py`，~120 行），思考→选工具→执行→观察→再思考，全透明 |
| 架构是否清晰 / 可扩展？ | 工具用 `REGISTRY` 字典**插件化注册**，新增能力只加一行，不改内核 |
| 数据是否规范？ | 跨模块只传 Pydantic 模型（带 `schema_version`），不传裸 dict |
| 现场演示万一断网/没邮箱？ | 默认**离线演示模式**：MockLLM + fixture 假邮件，断网也能全链路跑 |
| 发错邮件怎么办？ | 发信默认 `dry_run`/草稿态闸门 + 收件人白名单扩展点 |
| 怎么证明它"好"？ | 规则过滤可解释、有命中统计；可补 100 条标注集算 P/R/F1 |

---

## 🧱 架构

```
┌─────────────┐  自然语言指令
│  Gradio UI  │ ──────────────┐   (P-C 可视化：邮件列表 + Trace 时间线回放)
└─────────────┘               │
┌─────────────┐  指令          ▼
│  MailAgent  │ ───────> ┌──────────────────────────────┐
│ (ReAct 内核) │<──观察───┤ 工具层 REGISTRY（插件化）      │
└─────────────┘           │  search / summarize /         │
        │ 调 LLM           │  filter_spam / export / send  │
        ▼                  └──────────────────────────────┘
┌─────────────┐                │                │
│  LLM 层     │                ▼                ▼
│ Mock/Ollama │        ┌──────────────┐  ┌──────────────┐
└─────────────┘        │ 邮件源层      │  │ 数据模型层    │
                       │ Fixture/IMAP │  │ Pydantic ×5  │
                       └──────────────┘  └──────────────┘
```

四层：`Agent 内核` → `工具层(可插拔)` → `LLM 层(可换)` / `邮件源层(可换)` → `数据模型层`。
横切能力：配置中心 `config.py`、结构化 Trace 落盘（UI 回放）。

---

## 📂 目录结构

```
AI-Mail-Agent/
├── app.py              # CLI 入口：python app.py --demo -i "指令"
├── app_ui.py           # Gradio 界面：python app_ui.py → http://127.0.0.1:7860
├── agent.py            # ReAct 内核（手写，~120 行）
├── tools.py            # 工具层 + REGISTRY 插件化注册
├── llm.py              # LLM 层：MockLLM(离线) + OllamaLLM(真实) + JSON 守卫
├── mail.py             # 邮件源：FixtureStore(默认) + IMAPStore(骨架)
├── models.py           # Pydantic 数据模型 ×5（带 schema_version）
├── config.py           # 集中配置层（零依赖可加载）
├── requirements.txt
├── .env.example        # 接真实 LLM / 真实邮箱时填这里
├── data/
│   └── fixtures.json    # 30 封离线假邮件（22 正常 + 8 垃圾）
└── out/
    └── emails.xlsx      # 导出产物
```

---

## 🚀 快速开始

### 0. 环境
```bash
cd C:/Users/pc/Desktop/AI‑Mail‑Agent
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # 仅需一次
```
> 依赖已在项目里装好（`.venv`）。**必须用 `.venv/Scripts/python`，别用系统 python。**

### 1. CLI 模式（终端直接演示 ReAct + Trace）
```bash
.\.venv\Scripts\python.exe app.py --demo -i "检索未读邮件，过滤垃圾邮件，并导出为 Excel"
```

### 2. 可视化界面（面试首选）
```bash
.\.venv\Scripts\python.exe app_ui.py
# 浏览器自动打开 http://127.0.0.1:7860
```
界面三栏：**指令输入** + **邮件列表** + **Trace 时间线回放**；底部为最终回答与 Excel
预览/下载。打开即自动跑一条默认指令，面试官一来就有内容看。

### 3. 试试这些指令（都离线可跑）
```
检索未读邮件，过滤垃圾邮件，并导出为 Excel
检索邮件并生成摘要并导出
检索包含发票的邮件
过滤垃圾邮件
```

---

## ⚙️ 接真实大模型 / 真实邮箱（可选，演示版无需）

复制 `.env.example` 为 `.env`：
- 本地装 [Ollama](https://ollama.com)，拉模型 `ollama pull qwen2.5:7b`
- 设 `DEMO_MODE=false`、`LLM_PROVIDER=ollama`、`LLM_BASE_URL=http://localhost:11434/v1`
- 真实邮箱：填 `MAIL_HOST/MAIL_USER/MAIL_PASS`（国内邮箱用**应用专用密码**，非登录密码）

业务代码一行不用改——这就是「可插拔」的价值。

---

## 🎯 功能清单

- [x] **邮件检索**：按关键词 / 未读过滤，返回 uid 列表
- [x] **AI 摘要**：分类 / 优先级 / 是否需跟进 / 要点（结构化 JSON，带校验+降级）
- [x] **垃圾过滤**：规则层，确定性、可解释，输出命中统计
- [x] **导出 Excel**：pandas → openpyxl，一键导出
- [x] **发送邮件**：默认草稿闸门（dry_run），Human-in-the-loop 待扩展
- [x] **自然语言入口**：一条指令驱动多步工具编排
- [x] **Trace 可视化**：每步思考→工具→参数→结果逐帧回放
- [ ] 附件内容解析（后续）
- [ ] RAG 长邮件检索（后续）
- [ ] 定时任务 / 规则订阅（后续）

> 故意把"附件解析 / RAG / 定时"列为后续——面试时这反而显得**规划清晰、知道边界**。

---

## 💡 面试与简历话术

### 简历项目描述（可直接用）
> **AI 邮件智能体**（Python）｜独立开发
> 基于手写 ReAct 循环构建邮件处理 Agent，支持检索 / 摘要 / 垃圾过滤 / 导出 / 发送。
> 工具层采用 REGISTRY 字典插件化设计，新增能力零侵入内核；数据层统一 Pydantic 模型
> 并带 schema_version 以支持演进；LLM 与邮件源均抽象为可替换接口（Mock / Ollama /
> IMAP）。内置离线演示模式与发信安全闸门，可断网现场演示，全程结构化 Trace 可回放。

### 高频追问标准答法
1. **为什么不用 LangChain？**
   手写 ReAct（~120 行）让「思考→选工具→执行→观察」每步透明、可断点、可讲原理；
   框架黑盒不利于面试讲解，也多一层重依赖。Provider 仍用 OpenAI 兼容接口，未来可平滑接框架。

2. **LLM 输出 JSON 不合法怎么办？**
   `OllamaLLM.summarize` 用 Pydantic 校验 + 最多 2 次重试；仍失败降级到规则摘要
   （`heuristic_summary`），绝不把脏数据写进 Excel。

3. **误发邮件怎么办？**
   发信工具默认 `dry_run` / `draft=True`，只生成草稿不真正发送；扩展点已留收件人白名单
   与 Human-in-the-loop 二次确认，关闭闸门需显式配置。

4. **成本 / 性能？**
   演示走 Mock 零成本；真实模式用本地 Ollama 免费。批量可加并发与磁盘缓存（config 已留
   `CONCURRENCY`）、正文截断 8000 字符控制 token。

5. **怎么扩展新能力？**
   在 `tools.py` 的 `REGISTRY` 加一项（name/description/params JSON Schema/func），Agent
   内核与 UI 自动可用——这就是插件化。

6. **有什么不足 / 下一步？**
   当前垃圾过滤是规则层（可解释但召回有限），下一步用 100 条标注集训练轻量分类器并算
   F1；附件解析、RAG、定时任务已规划。诚实讲边界比吹完美更加分。

---

## 🧪 质量（建议补充，加分项）
- 用 `pytest` + `unittest.mock` 对 IMAP / LLM 做隔离测试
- 离线 e2e：固定 fixture 跑全链路断言步数与产物
- GitHub Actions：全 mock、无密钥，PR 自动跑测试
- 100 条标注集测垃圾过滤 P/R/F1，写进 README

---

## 📜 许可
MIT（演示项目，fixture 为虚构数据）。
