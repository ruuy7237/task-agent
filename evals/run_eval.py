# -*- coding: utf-8 -*-
"""任务评测跑分器。

一次跑完两遍：
  baseline（verify=False）  —— 朴素版：参数错了直接炸、空结果照单全收
  hardened（verify=True）   —— 加固版：参数校验 + 结果校验 + 步数护栏

为什么要 A/B：只有把「加了校验」和「没加校验」的差距量化出来，
简历上那句「通过 X 优化把成功率从 62% 提到 84%」才站得住脚。

用法：
  python evals/run_eval.py                 # 全跑
  python evals/run_eval.py --limit 10      # 只跑前 10 条，快速试通流程
  python evals/run_eval.py --only hardened # 只跑加固版
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent import llm as llm_mod  # noqa: E402
from agent.loop import run  # noqa: E402
from agent.tools import WORKDIR  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(HERE, "tasks.json")
RESULTS_FILE = os.path.join(HERE, "results.json")
REPORT_FILE = os.path.join(HERE, "report.md")

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _numbers(text):
    out = []
    for raw in _NUM_RE.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return out


def check_number(answer, spec):
    target = float(spec["value"])
    tol = float(spec.get("tol", 0.51))
    nums = _numbers(answer)
    if not nums:
        return False
    return min(abs(n - target) for n in nums) <= tol


def check_file(answer, spec, workdir):
    path = os.path.join(workdir, spec["path"])
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    need = spec["value"]
    return need.lower() in content.lower()


def judge(result, checker, workdir):
    """判定一条任务是否成功。返回 (bool, 说明)。"""
    if not result["success"]:
        return False, result.get("failure_reason") or "未产出答案"

    answer = result["answer"]
    t = checker["type"]

    if t == "number":
        ok = check_number(answer, checker)
        return ok, "" if ok else f"数值不匹配（期望 {checker['value']}±{checker.get('tol', 0.51)}）"
    if t == "contains":
        ok = checker["value"].lower() in answer.lower()
        return ok, "" if ok else f"未包含关键词 {checker['value']}"
    if t == "today":
        d = datetime.date.today().strftime("%Y-%m-%d")
        also = datetime.date.today().strftime("%Y年%m月%d日")
        return (d in answer or also in answer or d[5:] in answer), f"未给出今天日期 {d}"
    if t == "weekday":
        wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.date.today().weekday()]
        return wd in answer, f"未给出星期 {wd}"
    if t == "month":
        m = datetime.date.today().month
        return f"{m}月" in answer or f"{m:02d}" in answer, f"未给出月份 {m}"
    if t == "file_contains":
        ok = check_file(answer, checker, workdir)
        return ok, "" if ok else f"文件 {checker['path']} 缺失或不含 {checker['value']}"
    if t == "multi":
        for sub in checker["values"]:
            ok, why = judge(result, sub, workdir)
            if not ok:
                return False, why
        return True, ""
    return False, f"未知 checker 类型 {t}"


def run_suite(tasks, verify, workdir, timeout_steps=8):
    os.makedirs(workdir, exist_ok=True)
    records = []
    for t in tasks:
        # 每条任务独立沙箱，避免文件类任务互相污染
        if os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        os.makedirs(workdir, exist_ok=True)
        res = run(t["task"], llm_mod, max_steps=timeout_steps, verify=verify)
        ok, why = judge(res, t["checker"], workdir)
        records.append({
            "id": t["id"], "level": t["level"], "task": t["task"],
            "success": ok, "reason": why or ("ok" if ok else "失败"),
            "answer": res["answer"][:300], "steps": res["steps"],
            "tool_calls": res["tool_calls"], "tool_errors": res["tool_errors"],
            "failure_reason": res["failure_reason"],
            "trace": res["trace"][:6],
        })
    return records


def summarize(records):
    total = len(records)
    ok = sum(r["success"] for r in records)
    by_level = {}
    for r in records:
        d = by_level.setdefault(r["level"], {"total": 0, "ok": 0})
        d["total"] += 1
        d["ok"] += 1 if r["success"] else 0
    calls = sum(r["tool_calls"] for r in records)
    errors = sum(r["tool_errors"] for r in records)
    return {
        "total": total, "success": ok,
        "success_rate": round(ok / total * 100, 1) if total else 0.0,
        "avg_steps": round(sum(r["steps"] for r in records) / total, 2) if total else 0,
        "tool_calls": calls, "tool_errors": errors,
        "tool_error_rate": round(errors / calls * 100, 1) if calls else 0.0,
        "by_level": {k: {"total": v["total"], "ok": v["ok"],
                         "rate": round(v["ok"] / v["total"] * 100, 1) if v["total"] else 0}
                     for k, v in by_level.items()},
    }


def failure_taxonomy(records):
    """把失败归因成几类 —— 这份归因就是 README 里最有价值的部分。"""
    buckets = {}
    for r in records:
        if r["success"]:
            continue
        trace_types = [t["type"] for t in r.get("trace", [])]
        if r["failure_reason"]:
            key = "步数耗尽/死循环"
        elif "invalid_args" in trace_types:
            key = "工具参数错误"
        elif "tool_error" in trace_types:
            key = "工具执行报错"
        elif "数值不匹配" in r["reason"] or "数值" in r["reason"]:
            key = "计算/数值错误"
        elif "文件" in r["reason"]:
            key = "文件未写出或未读回"
        elif "日期" in r["reason"] or "星期" in r["reason"] or "月份" in r["reason"]:
            key = "未调用日期工具（幻觉）"
        else:
            key = "答案不完整/跑偏"
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items(), key=lambda kv: -kv[1]))


def write_report(base_sum, hard_sum, base_rec, hard_rec, model_label):
    lines = [
        "# 任务评测报告（自动生成）",
        "",
        f"- 生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型/模式：**{model_label}**",
        f"- 任务数：{base_sum['total']}（简单 20 / 中等 20 / 困难 10）",
        "",
        "## 1. 总体对比",
        "",
        "| 版本 | 任务成功率 | 平均步数 | 工具调用错误率 |",
        "| --- | --- | --- | --- |",
        f"| baseline（无校验） | {base_sum['success_rate']}% | {base_sum['avg_steps']} | {base_sum['tool_error_rate']}% |",
        f"| hardened（参数+结果校验） | {hard_sum['success_rate']}% | {hard_sum['avg_steps']} | {hard_sum['tool_error_rate']}% |",
        "",
        "## 2. 分难度成功率",
        "",
        "| 难度 | baseline | hardened |",
        "| --- | --- | --- |",
    ]
    for lv in ("easy", "medium", "hard"):
        b = base_sum["by_level"].get(lv, {"total": 0, "rate": 0})
        h = hard_sum["by_level"].get(lv, {"total": 0, "rate": 0})
        lines.append(f"| {lv} ({b['total']}) | {b['rate']}% | {h['rate']}% |")

    lines += ["", "## 3. 失败归因（hardened 版本）", "", "| 失败类型 | 数量 |", "| --- | --- |"]
    tax = failure_taxonomy(hard_rec)
    if tax:
        for k, v in tax.items():
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("| （无失败） | 0 |")

    lines += ["", "## 4. 典型失败案例（取前 8 条）", "", "| 任务ID | 难度 | 任务 | 判定原因 |", "| --- | --- | --- | --- |"]
    for r in [x for x in hard_rec if not x["success"]][:8]:
        task_short = r["task"][:34] + ("…" if len(r["task"]) > 34 else "")
        lines.append(f"| {r['id']} | {r['level']} | {task_short} | {r['reason'][:40]} |")

    lines += ["", "> 用真实模型重跑会覆盖本文件：`LLM_API_KEY=sk-xxx python evals/run_eval.py`", ""]
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return REPORT_FILE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    ap.add_argument("--only", choices=["baseline", "hardened"], default="")
    args = ap.parse_args()

    if not os.path.exists(TASKS_FILE):
        print("找不到 tasks.json，先运行 python evals/gen_tasks.py")
        sys.exit(1)

    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)["tasks"]
    if args.limit:
        tasks = tasks[: args.limit]

    label = f"{llm_mod.MODEL} ({llm_mod.mode()} 模式)"
    print(f"评测开始 | {label} | {len(tasks)} 条任务")

    results = {"model": label, "generated_at": datetime.datetime.now().isoformat(timespec="seconds")}

    if args.only != "hardened":
        print("  运行 baseline ...")
        base_rec = run_suite(tasks, verify=False, workdir=WORKDIR)
    else:
        base_rec = None
    print("  运行 hardened ...")
    hard_rec = run_suite(tasks, verify=True, workdir=WORKDIR)

    if base_rec is None:
        base_sum = summarize([{"success": False, "steps": 0, "tool_calls": 0, "tool_errors": 0, "level": t["level"]} for t in tasks])
        base_sum["note"] = "本次仅跑 hardened"
    else:
        base_sum = summarize(base_rec)
    hard_sum = summarize(hard_rec)

    results["baseline"] = base_sum
    results["hardened"] = hard_sum
    results["records_hardened"] = hard_rec
    results["failure_taxonomy"] = failure_taxonomy(hard_rec)

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    rep = write_report(base_sum, hard_sum, base_rec or [], hard_rec, label)
    print("\n=== baseline ===")
    print(json.dumps(base_sum, ensure_ascii=False, indent=2))
    print("=== hardened ===")
    print(json.dumps(hard_sum, ensure_ascii=False, indent=2))
    print(f"\n报告已写入 {rep}")


if __name__ == "__main__":
    main()
