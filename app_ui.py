"""Gradio 可视化界面（P-C 阶段加入）。

三栏布局：
- 顶部：自然语言指令输入框 + 「演示模式」开关 + 运行按钮
- 左栏：邮件列表（DataFrame，随指令结果刷新）
- 右栏：执行轨迹 Trace 时间线（HTML 回放，逐帧展示 Agent 思考→调工具→观察）
- 底部：最终回答 + 导出 Excel 预览与下载

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


# --------------------------------------------------------------------------- #
# 渲染辅助
# --------------------------------------------------------------------------- #
def _build_trace_html(steps) -> str:
    """把 TraceStep 列表渲染成纵向时间线 HTML。"""
    if not steps:
        return "<p style='color:#888'>（暂无执行轨迹）</p>"
    items = []
    for s in steps:
        idx = s.index
        thought = (s.thought or "").replace("<", "&lt;").replace(">", "&gt;")
        if s.tool:
            args = str(s.args).replace("<", "&lt;").replace(">", "&gt;")
            obs = (s.observation or "").replace("<", "&lt;").replace(">", "&gt;")
            obs = obs[:600] + ("…" if len(obs) > 600 else "")
            body = (
                f'<div class="t-action">⚙️ <b>{s.tool}</b>({args})</div>'
                f'<div class="t-obs">📥 {obs}</div>'
            )
        else:
            body = '<div class="t-obs">✅ 任务完成，输出最终回答。</div>'
        items.append(
            f'<div class="t-step">'
            f'<div class="t-dot">#{idx}</div>'
            f'<div class="t-card">'
            f'<div class="t-thought">💭 {thought}</div>'
            f'{body}'
            f'</div></div>'
        )
    css = """
    <style>
    .trace {font-family:-apple-system,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
           font-size:13px; line-height:1.5;}
    .t-step{display:flex; gap:10px; margin-bottom:10px; align-items:flex-start;}
    .t-dot{flex:0 0 26px; height:26px; border-radius:50%; background:#2563eb; color:#fff;
           text-align:center; line-height:26px; font-weight:700; font-size:12px;}
    .t-card{flex:1; background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:8px 10px;}
    .t-thought{color:#0f172a; margin-bottom:4px;}
    .t-action{color:#7c3aed; font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
              background:#faf5ff; border-radius:6px; padding:4px 6px; margin:2px 0;}
    .t-obs{color:#334155; font-size:12px; white-space:pre-wrap;}
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
    """运行一条指令，返回 (邮件列表, Trace HTML, 最终回答, Excel预览, Excel路径)。"""
    # 演示开关：运行时切换数据源与 LLM（Mock vs 真实 Ollama）
    settings.demo_mode = bool(demo)
    os.environ["DEMO_MODE"] = "true" if demo else "false"

    result = run_agent(instruction or "检索未读邮件，过滤垃圾邮件，并导出为 Excel")

    email_df = _build_email_df(result.state)
    trace_html = _build_trace_html(result.steps)
    answer = result.answer or "(无回答)"
    excel_df = _load_excel_preview(result.state.get("export_path") or EXCEL_PATH)
    excel_path = result.state.get("export_path") or (EXCEL_PATH if os.path.exists(EXCEL_PATH) else None)
    return email_df, trace_html, answer, excel_df, excel_path


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
def build_ui():
    cfg = summary()
    mode_line = "演示模式(离线 Mock + fixture)" if cfg["DEMO_MODE"] else f"真实模式(LLM={cfg['LLM_MODEL']})"

    with gr.Blocks(title="AI Mail Agent") as ui:
        gr.Markdown(
            "# 📧 AI Mail Agent · 邮件智能体\n"
            f"<sub>当前模式：<b>{mode_line}</b> ｜ 架构：手写 ReAct 内核 + 插件化工具 + Pydantic 数据模型</sub>"
        )

        with gr.Row():
            instruction = gr.Textbox(
                label="自然语言指令",
                value="检索未读邮件，过滤垃圾邮件，并导出为 Excel",
                placeholder="例如：检索未读邮件，过滤垃圾邮件，并导出为 Excel",
                scale=5,
            )
            demo_mode = gr.Checkbox(label="演示模式(离线 Mock)", value=True, scale=1)
            run_btn = gr.Button("▶ 运行 Agent", variant="primary", scale=1)

        with gr.Row(equal_height=False):
            with gr.Column():
                gr.Markdown("### 📋 邮件列表")
                email_df = gr.DataFrame(label="", interactive=False, wrap=True)
            with gr.Column():
                gr.Markdown("### 🧠 执行轨迹 (Trace 时间线回放)")
                trace_html = gr.HTML()

        with gr.Row():
            gr.Markdown("### 💬 最终回答")
            answer = gr.Markdown()

        with gr.Row():
            with gr.Column(scale=4):
                gr.Markdown("### 📊 导出预览 (out/emails.xlsx)")
                excel_df = gr.DataFrame(label="", interactive=False, wrap=True)
            with gr.Column(scale=1):
                gr.Markdown("### ⬇️ 下载")
                excel_file = gr.File(label="点击下载 Excel")

        # 运行交互
        run_btn.click(
            fn=run,
            inputs=[instruction, demo_mode],
            outputs=[email_df, trace_html, answer, excel_df, excel_file],
        )
        # 首次加载即跑一条默认指令，面试时打开就有内容
        ui.load(
            fn=run,
            inputs=[instruction, demo_mode],
            outputs=[email_df, trace_html, answer, excel_df, excel_file],
        )

    return ui


if __name__ == "__main__":
    build_ui().launch(server_name="127.0.0.1", server_port=7860, share=False,
                     inbrowser=True, theme=gr.themes.Soft())
