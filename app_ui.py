"""Gradio 可视化界面（P-C 阶段加入，产品级美化版）。

布局（自上而下）：
- 顶部：品牌渐变 Header（标题 + 当前模式徽章）
- 指令区：自然语言输入 + 示例指令（点击即填）+ 演示开关 + 运行按钮
- 统计卡片：邮件总数 / 未读 / 垃圾 / 已导出（随指令结果刷新）
- 主区：左「邮件列表」+ 右「执行轨迹 Trace 时间线」
- 底部：最终回答 + Excel 预览与下载

启动：
    .venv/Scripts/python app_ui.py
浏览器自动打开 http://127.0.0.1:7860

面试最值钱的一块就在这：Trace 时间线能把 Agent 每一步「思考 → 调哪个工具 →
参数 → 返回了什么」逐帧展示出来，而不是只丢一个黑盒结果。
"""
from __future__ import annotations

import os

import gradio as gr
import pandas as pd

from agent import run_agent
from config import settings, summary
from mail import get_mail_store

EXCEL_PATH = os.path.join(settings.out_dir if hasattr(settings, "out_dir") else "out", "emails.xlsx")
# 兼容：settings.out_dir 可能不存在（config.settings 命名空间未暴露 OUT_DIR）
if not os.path.exists(EXCEL_PATH):
    EXCEL_PATH = "out/emails.xlsx"

# 主题色板（与 Header / Trace 时间线保持一致）
_C_BLUE = "#2563eb"
_C_INDIGO = "#4f46e5"
_C_PURPLE = "#7c3aed"
_C_AMBER = "#f59e0b"
_C_RED = "#ef4444"
_C_GREEN = "#10b981"
_C_SLATE = "#64748b"
_C_BORDER = "#e2e8f0"

# 示例指令（面试时一键切换演示场景）
_EXAMPLES = [
    "检索未读邮件，过滤垃圾邮件，并导出为 Excel",
    "检索邮件并生成摘要并导出",
    "检索包含发票的邮件",
    "过滤垃圾邮件",
]


# --------------------------------------------------------------------------- #
# 渲染辅助
# --------------------------------------------------------------------------- #
def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _build_header_html(mode_text: str) -> str:
    """品牌渐变头部：标题 + 当前模式徽章。"""
    return (
        '<div style="background:linear-gradient(135deg,#1e3a8a 0%,#4f46e5 55%,#7c3aed 100%);'
        'border-radius:16px;padding:26px 30px;color:#fff;margin-bottom:6px;'
        'box-shadow:0 6px 20px rgba(79,70,229,.25);">'
        '<div style="display:flex;align-items:center;justify-content:space-between;'
        'flex-wrap:wrap;gap:14px;">'
        '<div>'
        '<div style="font-size:24px;font-weight:800;letter-spacing:.4px;">'
        '📧 AI Mail Agent · 邮件智能体</div>'
        '<div style="font-size:13px;opacity:.88;margin-top:6px;">'
        '手写 ReAct 内核 · 插件化工具 · Pydantic 数据模型 · 离线可演示</div>'
        '</div>'
        '<div style="display:inline-flex;align-items:center;gap:8px;'
        'background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.28);'
        'border-radius:999px;padding:6px 14px;font-size:12px;font-weight:600;">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#4ade80;'
        'display:inline-block;box-shadow:0 0 0 3px rgba(74,222,128,.25);"></span>'
        f'{_esc(mode_text)}'
        '</div>'
        '</div>'
        '</div>'
    )


def _build_stats_html(state: dict) -> str:
    """顶部四张统计卡片：总数 / 未读 / 垃圾 / 已导出。"""
    try:
        emails = get_mail_store().all()
        total = len(emails)
        unread = sum(1 for e in emails if not e.is_read)
    except Exception:
        total = unread = 0
    spam = len(state.get("spam_uids") or [])
    exported = "是" if state.get("export_path") else "—"

    cards = [
        ("📬", "邮件总数", total, _C_BLUE),
        ("✉️", "未读", unread, _C_AMBER),
        ("🚫", "垃圾邮件", spam, _C_RED),
        ("📊", "已导出", exported, _C_GREEN),
    ]
    items = []
    for icon, label, value, color in cards:
        items.append(
            '<div style="flex:1;min-width:130px;background:#fff;'
            'border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;'
            'box-shadow:0 1px 3px rgba(15,23,42,.06);">'
            f'<div style="font-size:20px;line-height:1;">{icon}</div>'
            f'<div style="font-size:26px;font-weight:800;color:{color};'
            f'margin:8px 0 2px;">{value}</div>'
            f'<div style="font-size:12px;color:#64748b;">{label}</div>'
            '</div>'
        )
    return '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:10px 0 4px;">' \
           + "".join(items) + "</div>"


def _build_trace_html(steps) -> str:
    """把 TraceStep 列表渲染成纵向时间线 HTML（产品级卡片）。"""
    if not steps:
        return "<p style='color:#94a3b8;padding:24px;text-align:center;'>（暂无执行轨迹）</p>"
    items = []
    for s in steps:
        thought = _esc(s.thought)
        if s.tool:
            args = _esc(s.args)
            obs = _esc(s.observation)[:600] + ("…" if len(s.observation or "") > 600 else "")
            body = (
                f'<div class="t-action">⚙️ <b>{_esc(s.tool)}</b>'
                f'<span class="t-args">({args})</span></div>'
                f'<div class="t-obs"><span class="t-obs-tag">📥 观察</span>{obs}</div>'
            )
        else:
            body = '<div class="t-finish">✅ 任务完成，输出最终回答。</div>'
        items.append(
            f'<div class="t-node">'
            f'<div class="t-dot">#{s.index + 1}</div>'
            f'<div class="t-card">'
            f'<div class="t-thought">💭 {thought}</div>'
            f'{body}'
            f'</div></div>'
        )
    css = """
    <style>
    .trace {font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
           font-size:13px; line-height:1.55;}
    .t-node{display:flex; gap:12px; position:relative; padding-bottom:14px;}
    .t-node:not(:last-child)::before{content:''; position:absolute; left:14px; top:32px;
           bottom:0; width:2px; background:#e2e8f0;}
    .t-dot{flex:0 0 30px; height:30px; border-radius:50%;
           background:linear-gradient(135deg,#4f46e5,#7c3aed); color:#fff;
           display:flex; align-items:center; justify-content:center;
           font-weight:700; font-size:12px; z-index:1;
           box-shadow:0 2px 6px rgba(79,70,229,.35);}
    .t-card{flex:1; background:#fff; border:1px solid #e2e8f0; border-radius:12px;
           padding:10px 14px; box-shadow:0 1px 2px rgba(15,23,42,.04);
           transition:box-shadow .15s;}
    .t-card:hover{box-shadow:0 4px 14px rgba(15,23,42,.08);}
    .t-thought{color:#334155; margin-bottom:6px;}
    .t-action{color:#7c3aed; font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
              background:#f5f3ff; border:1px solid #ede9fe; border-radius:8px;
              padding:5px 8px; margin:4px 0;}
    .t-args{color:#a78bfa; font-weight:400;}
    .t-obs{color:#334155; font-size:12px; white-space:pre-wrap; background:#f8fafc;
           border-radius:8px; padding:6px 8px; margin-top:4px;}
    .t-obs-tag{display:inline-block; color:#0f766e; font-weight:700; margin-right:6px;}
    .t-finish{color:#047857; font-weight:600; background:#ecfdf5; border:1px solid #a7f3d0;
              border-radius:8px; padding:6px 8px; margin-top:4px;}
    </style>
    """
    return css + '<div class="trace">' + "".join(items) + "</div>"


def _build_email_df(state: dict) -> pd.DataFrame:
    """根据 Agent 运行后的 state，构造邮件列表 DataFrame。

    - 优先展示本次指令检索到的邮件（state['search_uids']）
    - 若有垃圾判定（state['spam_uids']），加一列「垃圾」标记
    """
    store = get_mail_store()
    spam = set(str(u) for u in (state.get("spam_uids") or []))
    uids = state.get("search_uids") or None
    emails = store.all()
    if uids:
        want = set(str(u) for u in uids)
        emails = [e for e in emails if e.uid in want]
    rows = [{
        "uid": e.uid,
        "日期": e.date or "",
        "发件人": e.sender,
        "主题": e.subject,
        "正文预览": e.snippet,
        "附件": "有" if e.has_attachment else "—",
        "已读": "是" if e.is_read else "否",
        "垃圾": "是" if e.uid in spam else "—",
    } for e in emails]
    return pd.DataFrame(rows)


def _load_excel_preview(path: str):
    """读取导出的 Excel 作为预览 DataFrame。"""
    if path and os.path.exists(path):
        try:
            return pd.read_excel(path)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


# --------------------------------------------------------------------------- #
# 核心交互
# --------------------------------------------------------------------------- #
def run(instruction: str, demo: bool):
    """运行一条指令，返回 (邮件列表, Trace HTML, 最终回答, Excel预览, Excel路径, 统计卡片)。"""
    # 演示开关：运行时切换数据源与 LLM（Mock vs 真实 Ollama）
    settings.demo_mode = bool(demo)
    os.environ["DEMO_MODE"] = "true" if demo else "false"

    result = run_agent(instruction or "检索未读邮件，过滤垃圾邮件，并导出为 Excel")

    email_df = _build_email_df(result.state)
    trace_html = _build_trace_html(result.steps)
    answer = result.answer or "(无回答)"
    excel_df = _load_excel_preview(result.state.get("export_path") or EXCEL_PATH)
    excel_path = result.state.get("export_path") or (EXCEL_PATH if os.path.exists(EXCEL_PATH) else None)
    stats_html = _build_stats_html(result.state)
    return email_df, trace_html, answer, excel_df, excel_path, stats_html


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
_CSS = """
.gradio-container { font-family: 'PingFang SC','Microsoft YaHei','Segoe UI',Roboto,sans-serif; }
.gradio-container { max-width: 1240px !important; margin: 0 auto; }
"""

# Gradio 6 起 theme / css 需在 launch() 传入（而非 Blocks 构造函数）
# 注：Gradio 6 的 theme.font 不接受字符串列表，字体统一由 _CSS 的 .gradio-container 控制
_THEME = gr.themes.Soft(
    primary_hue="indigo",
    neutral_hue="slate",
)


def build_ui():
    cfg = summary()
    mode_text = ("演示模式 · 离线 Mock" if cfg["DEMO_MODE"]
                 else f"真实模式 · {cfg['LLM_MODEL']}")

    with gr.Blocks(title="AI Mail Agent") as ui:
        # 顶部品牌 Header
        gr.HTML(_build_header_html(mode_text))

        # 指令输入区
        with gr.Group():
            instruction = gr.Textbox(
                label="💬 自然语言指令",
                value="检索未读邮件，过滤垃圾邮件，并导出为 Excel",
                placeholder="例如：检索未读邮件，过滤垃圾邮件，并导出为 Excel",
                lines=2,
                scale=6,
            )
            with gr.Row():
                demo_mode = gr.Checkbox(label="演示模式（离线 Mock）", value=True)
                run_btn = gr.Button("▶ 运行 Agent", variant="primary")
            gr.Examples(examples=_EXAMPLES, inputs=instruction, label="✨ 示例指令（点击填入）")

        # 统计卡片
        stats_html = gr.HTML()

        # 主区：邮件列表 + Trace 时间线
        with gr.Row(equal_height=False):
            with gr.Column():
                gr.Markdown("### 📋 邮件列表")
                email_df = gr.DataFrame(label="", interactive=False, wrap=True)
            with gr.Column():
                gr.Markdown("### 🧠 执行轨迹（Trace 时间线回放）")
                trace_html = gr.HTML()

        # 最终回答
        gr.Markdown("### 💬 最终回答")
        answer = gr.Markdown()

        # Excel 预览 + 下载
        with gr.Row():
            with gr.Column(scale=4):
                gr.Markdown("### 📊 导出预览（out/emails.xlsx）")
                excel_df = gr.DataFrame(label="", interactive=False, wrap=True)
            with gr.Column(scale=1):
                gr.Markdown("### ⬇️ 下载")
                excel_file = gr.File(label="点击下载 Excel")

        # 运行交互
        outputs = [email_df, trace_html, answer, excel_df, excel_file, stats_html]
        run_btn.click(fn=run, inputs=[instruction, demo_mode], outputs=outputs)
        # 首次加载即跑一条默认指令，面试时打开就有内容
        ui.load(fn=run, inputs=[instruction, demo_mode], outputs=outputs)

    return ui


if __name__ == "__main__":
    build_ui().launch(server_name="127.0.0.1", server_port=7860, share=False, inbrowser=True,
                      theme=_THEME, css=_CSS)
