"""手写的 Tool-Calling Agent Loop —— 不依赖任何 agent 框架。

一次运行的数据流：

    用户任务
      └─> LLM ──tool_calls──> 参数校验 ──> 工具执行 ──> 结果校验
             ▲                                              │
             └──────────── 把观测结果写回上下文 ───────────────┘
                                       │
                                  无工具调用 → 输出最终答案

设计要点（面试时值得展开讲的四件事）：
1) **参数校验**：LLM 填错参数不是抛异常，而是把错误信息喂回去让它重试；
2) **结果校验**：工具返回空 / 报错，带着提示再试一次，而不是直接把垃圾塞进上下文；
3) **步数护栏**：超过 max_steps 强制结束并记录失败原因，避免死循环烧 token；
4) **可 Ablation**：关掉 verify 就是「朴素版」，打开就是「加固版」，可以量化每项防护的收益。
"""
import json

from .tools import SPECS, execute, validate_args

SYSTEM_PROMPT = """你是一个真正干活的任务型 Agent，不是聊天机器人。

可用工具：calculator（算数）、current_date（日期）、file_write / file_read（读写沙箱文件）、
sqlite_query（只读查本地库 office.db：departments / employees）、web_search（搜索）。

规则：
1. 需要精确数值时必须用 calculator，禁止心算；
2. 涉及日期一律用 current_date，不要凭记忆；
3. 要先取数据再回答，拿到工具结果后才写结论；
4. 参数必须严格符合 schema，path 只写文件名不带路径，sql 只能 SELECT；
5. 所有信息都拿到后，用一句话给出最终答案，附上关键数值。"""


def _normalize_call(call):
    """把各种方言的工具调用统一成 {id, name, args}。"""
    fn = call.get("function", {})
    args = fn.get("arguments", "{}")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None, "arguments 不是合法 JSON"
    return {"id": call.get("id") or "local", "name": fn.get("name"), "args": args}, None


def run(task, llm, max_steps=8, verify=True, verbose=False):
    """跑一个任务。返回结构化结果，方便评测脚本统计。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    trace, steps = [], 0
    tool_calls, tool_errors = 0, 0

    for step in range(1, max_steps + 1):
        steps = step
        msg = llm.chat(messages, SPECS)
        calls = msg.get("tool_calls") or []

        # ---- 分支 A：模型要调工具 ----------------------------------------
        if calls:
            assistant_msg = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": []}
            tool_messages = []

            for raw in calls:
                call, err = _normalize_call(raw)
                tool_calls += 1
                if err:
                    tool_errors += 1
                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": raw.get("id") or "local",
                        "name": raw.get("function", {}).get("name", ""),
                        "content": f"[参数错误] {err}。请修正参数后重新调用。",
                    })
                    trace.append({"step": step, "type": "invalid_args", "detail": err})
                    continue

                name, args, cid = call["name"], call["args"], call["id"]

                if verify:
                    ok, why = validate_args(name, args)
                    if not ok:
                        tool_errors += 1
                        tool_messages.append({
                            "role": "tool", "tool_call_id": cid, "name": name,
                            "content": f"[参数校验失败] {why}。请按 schema 重新调用。",
                        })
                        trace.append({"step": step, "type": "invalid_args", "tool": name, "detail": why})
                        if verbose:
                            print(f"  [{step}] ✗ 参数校验 {name}: {why}")
                        continue

                out = execute(name, args)
                content = out["result"]
                if not out["ok"]:
                    tool_errors += 1
                    trace.append({"step": step, "type": "tool_error", "tool": name, "detail": content[:200]})
                    if verbose:
                        print(f"  [{step}] ✗ 工具报错 {name}: {content[:120]}")
                else:
                    trace.append({"step": step, "type": "tool_ok", "tool": name, "detail": content[:120]})
                    if verbose:
                        print(f"  [{step}] ✓ {name} -> {content[:120]}")

                # 结果校验：空结果 / 报错，提示补一次
                if verify and (not content or content.strip() in ("[]", "{}", "null")):
                    content = f"[结果为空] {name} 返回空结果，请调整参数重试，或改用其它工具。"

                tool_messages.append({"role": "tool", "tool_call_id": cid, "name": name, "content": content})

            if not tool_messages:  # 极端情况：全部调用都被判定非法且未产出消息
                tool_messages.append({
                    "role": "tool", "tool_call_id": "fallback", "name": "system",
                    "content": "上一次调用全部非法，请重新规划并检查工具 schema。",
                })
            messages.append(assistant_msg)
            messages.extend(tool_messages)
            continue

        # ---- 分支 B：模型要收尾 ------------------------------------------
        answer = (msg.get("content") or "").strip()
        if not answer:
            tool_errors += 1
            messages.append({
                "role": "user",
                "content": "[空答案] 请基于已有工具结果给出最终答案，必须包含关键数值。",
            })
            continue
        if verify and len(answer) < 4:
            messages.append({"role": "user", "content": "[答案过短] 请补充结果数值后重新输出。"})
            continue

        return {
            "task": task,
            "success": True,
            "answer": answer,
            "steps": steps,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
            "trace": trace,
            "failure_reason": "",
        }

    return {
        "task": task,
        "success": False,
        "answer": "",
        "steps": steps,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "trace": trace,
        "failure_reason": f"超过最大步数 {max_steps} 仍未产出答案",
    }
