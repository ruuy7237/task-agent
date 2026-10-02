# -*- coding: utf-8 -*-
"""命令行入口：单条任务调试 / 交互模式。

  python cli.py --task "先算出研发部平均工资，再算出销售部平均工资，告诉我差多少"
  python cli.py                       # 交互模式
  python cli.py --task "..." --no-verify --verbose
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import llm as llm_mod  # noqa: E402
from agent.loop import run  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", help="要执行的任务")
    ap.add_argument("--no-verify", action="store_true", help="关闭参数/结果校验（朴素版）")
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    verify = not args.no_verify
    print(f"[模式] LLM={llm_mod.MODEL} ({llm_mod.mode()}) 校验={'开' if verify else '关'}")

    if args.task:
        res = run(args.task, llm_mod, max_steps=args.max_steps, verify=verify, verbose=args.verbose)
        print("\n--- 最终答案 ---")
        print(res["answer"] or f"（失败：{res['failure_reason']}）")
        print(f"\n步数={res['steps']} 工具调用={res['tool_calls']} 工具错误={res['tool_errors']}")
        return

    print("交互模式，输入 exit 退出")
    while True:
        try:
            task = input("\n任务> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if task.lower() in ("exit", "quit", "exit()"):
            break
        if not task:
            continue
        res = run(task, llm_mod, max_steps=args.max_steps, verify=verify, verbose=args.verbose)
        print("回答>", res["answer"] or f"（失败：{res['failure_reason']}）")


if __name__ == "__main__":
    main()
