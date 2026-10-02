"""统一 LLM 客户端。

设计取舍：
- 只用标准库 urllib，不强依赖 openai 包，clone 下来就能跑；
- 任何 OpenAI 兼容接口都能用（DeepSeek / 通义 / Kimi / OpenAI），改环境变量即可；
- 没有 API Key 时自动降级为 mock 模式，用来验证 agent loop 与评测流水线本身是否正确。

环境变量：
  LLM_BASE_URL  默认 https://api.deepseek.com/v1
  LLM_API_KEY   有值 → 真实模型；为空 → mock 模式
  LLM_MODEL     默认 deepseek-chat
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid


def _load_env():
    """可选：从项目根目录的 .env 读取 API Key，免得配系统环境变量。"""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for cand in (os.path.join(root, ".env"), os.path.join(os.getcwd(), ".env")):
            if os.path.exists(cand):
                for line in open(cand, encoding="utf-8"):
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
                break
    except Exception:
        pass


_load_env()

BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")


def mode() -> str:
    return "mock" if not API_KEY else "api"


def chat(messages, tools=None, tool_choice="auto", temperature=0.0, retries=2):
    """返回标准化 assistant message dict。

    {"role": "assistant", "content": str|None, "tool_calls": [{"id","name","arguments"}]}
    """
    if mode() == "mock":
        return _mock_chat(messages, tools)

    url = BASE_URL.rstrip("/") + "/chat/completions"
    payload = {"model": MODEL, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败: {last_err}")


# --------------------------------------------------------------------------
# mock 模式：一个基于规则的“笨”规划器
# 目的不是聪明，而是让整条 pipeline（loop + 评测 + 报告）在没有 Key 时也能跑通并产出可解释的失败样本。
# --------------------------------------------------------------------------

_ARITH = re.compile(r"([0-9\.\s\+\-\*/\(\)]{3,})")


def _last_user(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


def _tool_call(name, args):
    return {
        "id": "call_" + uuid.uuid4().hex[:8],
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _mock_chat(messages, tools=None):
    # 已经拿到工具结果 → 收尾总结。保证 loop 能终止，也顺便演示「观测 → 答案」这一步。
    if messages and messages[-1].get("role") == "tool":
        obs = [m.get("content", "") for m in messages if m.get("role") == "tool"]
        tail = " | ".join(o[:300] for o in obs[-3:])
        return {"role": "assistant", "content": f"根据工具结果：{tail}", "tool_calls": []}

    text = _last_user(messages)
    calls = []

    numbers = re.findall(r"\d+(?:\.\d+)?", text)

    if any(k in text for k in ("几号", "今天", "日期", "当前时间")):
        calls.append(_tool_call("current_date", {}))
    elif any(k in text for k in ("写入", "保存", "写到", "存到", "生成文件", "存成")):
        m = re.search(r"([A-Za-z0-9_\-]+\.(?:txt|md|csv))", text)
        path = m.group(1) if m else "output.txt"
        expr = _ARITH.search(text)
        if expr and any(op in expr.group(1) for op in "+-*/"):
            calls.append(_tool_call("calculator", {"expression": expr.group(1).strip()}))
        calls.append(_tool_call("file_write", {"path": path, "content": text}))
        calls.append(_tool_call("file_read", {"path": path}))
    elif any(k in text for k in ("查询", "统计", "多少", "平均", "排序", "表", "数据库", "SQL")):
        if "比" in text and numbers:
            calls.append(_tool_call("sqlite_query", {"sql": "SELECT COUNT(*) FROM employees"}))
        else:
            calls.append(_tool_call("sqlite_query", {"sql": "SELECT * FROM employees LIMIT 5"}))
    elif numbers and any(op in text for op in ("+", "-", "*", "/", "加", "减", "乘", "除")):
        expr = _ARITH.search(text)
        calls.append(_tool_call("calculator", {"expression": (expr.group(1) if expr else "1+1").strip()}))
    else:
        calls.append(_tool_call("web_search", {"query": text}))

    if calls:
        return {"role": "assistant", "content": None, "tool_calls": calls}

    return {"role": "assistant", "content": f"[mock] 无法规划工具调用：{text}", "tool_calls": []}
