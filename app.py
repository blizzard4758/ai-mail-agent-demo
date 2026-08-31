"""命令行入口（最小可运行版）。

用法：
    python app.py --demo --instruction "检索未读邮件，过滤垃圾邮件，并导出为 Excel"
    python app.py --instruction "..."            # DEMO_MODE=true 时同样走离线 Mock

说明：
- --demo 等价于 DEMO_MODE=true（离线、MockLLM、不联网）。
- Gradio 可视化界面在 P-C 阶段加入（app_ui.py），本文件保持纯 CLI，便于面试时
  在终端直接演示 ReAct 循环与 Trace。
"""
from __future__ import annotations

import argparse
import sys

from config import summary
from agent import run_agent


def main() -> int:
    p = argparse.ArgumentParser(description="AI Mail Agent (CLI)")
    p.add_argument("--demo", action="store_true", help="离线演示模式（MockLLM + fixture）")
    p.add_argument("--instruction", "-i", default="检索未读邮件，过滤垃圾邮件，并导出为 Excel",
                   help="自然语言指令")
    args = p.parse_args()

    if args.demo:
        import os
        os.environ["DEMO_MODE"] = "true"

    print("=== 配置 ===")
    for k, v in summary().items():
        print(f"  {k:14}= {v}")
    print("\n=== 运行指令 ===")
    print(" ", args.instruction)

    r = run_agent(args.instruction)

    print("\n=== 执行轨迹 (Trace) ===")
    for s in r.steps:
        print(f"[{s.index}] {s.thought}")
        if s.tool:
            print(f"      Action : {s.tool}({s.args})")
            print(f"      Observe: {s.observation}")

    print("\n=== 最终回答 ===")
    print(r.answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
