"""工具注册表：OpenAI function calling 规范的 JSON Schema + 执行器。

每个工具三件套：
  1. spec      —— 给 LLM 看的 JSON Schema（决定它能不能填对参数）
  2. execute   —— 真正干活的函数，统一返回 {"ok": bool, "result": str}
  3. 护栏      —— 文件读写限制在沙箱目录、SQL 只允许 SELECT
"""
import ast
import datetime
import json
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKDIR = os.path.join(ROOT, "evals", "_work")
DB_PATH = os.path.join(ROOT, "data", "office.db")

# --------------------------------------------------------------------------
# 示例数据库（不存在时自动创建，保证仓库 clone 下来即可运行）
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    dept_id INTEGER NOT NULL,
    salary INTEGER NOT NULL,
    hire_date TEXT NOT NULL,
    FOREIGN KEY(dept_id) REFERENCES departments(id)
);
"""

_DEPARTMENTS = [
    (1, "研发部", "上海"),
    (2, "销售部", "北京"),
    (3, "市场部", "广州"),
    (4, "运营部", "深圳"),
]

_EMPLOYEES = [
    (1, "张伟", 1, 28000, "2021-03-15"),
    (2, "李娜", 1, 32000, "2020-07-01"),
    (3, "王强", 1, 25000, "2022-11-20"),
    (4, "赵敏", 2, 18000, "2021-09-09"),
    (5, "孙悦", 2, 22000, "2019-05-30"),
    (6, "周涛", 2, 19500, "2023-02-14"),
    (7, "吴芳", 3, 21000, "2020-12-12"),
    (8, "徐磊", 3, 24000, "2022-06-06"),
    (9, "马云龙", 4, 26000, "2018-08-08"),
    (10, "朱琳", 4, 23000, "2021-01-25"),
]


def ensure_db(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT OR REPLACE INTO departments VALUES (?,?,?)", _DEPARTMENTS
    )
    conn.executemany(
        "INSERT OR REPLACE INTO employees VALUES (?,?,?,?,?)", _EMPLOYEES
    )
    conn.commit()
    conn.close()
    return path


def schema_ddl(path=DB_PATH):
    ensure_db(path)
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    conn.close()
    return "\n".join(r[0] for r in rows)


# --------------------------------------------------------------------------
# 沙箱文件路径
# --------------------------------------------------------------------------


def _safe_path(path: str) -> str:
    """把用户传入的路径关进沙箱，防止路径穿越。"""
    os.makedirs(WORKDIR, exist_ok=True)
    name = os.path.basename(str(path))
    if not name or name in (".", ".."):
        raise ValueError("非法文件名")
    return os.path.join(WORKDIR, name)


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------


def calculator(expression: str):
    """安全求值：只允许数字和 +-*/()，禁止函数调用与变量。"""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as e:
        return {"ok": False, "result": f"表达式语法错误: {e}"}

    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            return {"ok": False, "result": f"表达式含不允许的语法: {type(node).__name__}"}
    try:
        value = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {})
    except ZeroDivisionError:
        return {"ok": False, "result": "除零错误"}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "result": f"求值失败: {e}"}

    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {"ok": True, "result": str(value)}


def current_date():
    now = datetime.datetime.now()
    return {
        "ok": True,
        "result": json.dumps(
            {
                "date": now.strftime("%Y-%m-%d"),
                "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
                "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
        ),
    }


def file_write(path: str, content: str):
    try:
        target = _safe_path(path)
    except ValueError as e:
        return {"ok": False, "result": str(e)}
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "result": f"已写入 {os.path.basename(target)}，共 {len(content)} 字符"}
    except OSError as e:
        return {"ok": False, "result": f"写入失败: {e}"}


def file_read(path: str):
    try:
        target = _safe_path(path)
    except ValueError as e:
        return {"ok": False, "result": str(e)}
    if not os.path.exists(target):
        return {"ok": False, "result": f"文件不存在: {os.path.basename(target)}"}
    with open(target, "r", encoding="utf-8") as f:
        return {"ok": True, "result": f.read()}


def sqlite_query(sql: str):
    """只允许只读 SQL —— 这是结果校验的一环，也是 SQL Agent 的安全护栏雏形。"""
    ensure_db()
    cleaned = sql.strip().rstrip(";").strip()
    first_word = cleaned.split()[0].lower() if cleaned.split() else ""
    if first_word not in ("select", "with", "pragma"):
        return {"ok": False, "result": f"只允许 SELECT 查询，收到的是 {first_word.upper()}"}
    if any(k in cleaned.lower() for k in ("delete ", "update ", "insert ", "drop ", "alter ")):
        return {"ok": False, "result": "检测到写操作，已拦截"}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(cleaned)
        rows = [dict(r) for r in cur.fetchall()]
        if len(rows) > 50:
            rows = rows[:50]
            rows.append({"__note__": "结果超过 50 行，已截断"})
        return {"ok": True, "result": json.dumps(rows, ensure_ascii=False)}
    except sqlite3.Error as e:
        return {"ok": False, "result": f"SQL 执行错误: {e}"}
    finally:
        conn.close()


def web_search(query: str):
    """本地桩实现：真实场景替换为 Tavily / SerpAPI。

    保留这个工具是为了证明「工具可插拔」：换一个函数体，agent 逻辑一行不用改。
    """
    fake = {
        "LangChain 是什么": "LangChain 是一个用于构建 LLM 应用的编排框架，提供 Chain / Agent / Memory 抽象。",
        "什么是 MCP": "MCP（Model Context Protocol）是把外部数据与工具接入大模型客户端的开放协议。",
    }
    for k, v in fake.items():
        if k in query:
            return {"ok": True, "result": v}
    return {"ok": True, "result": f"[本地桩] 未接入真实搜索：{query}"}


# --------------------------------------------------------------------------
# Schema 与注册表
# --------------------------------------------------------------------------

SPECS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算数学表达式，支持 + - * / 和括号。需要精确结果时必须使用，不要心算。",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "如 (12+8)*3/4"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_date",
            "description": "获取今天的日期、星期和当前时间。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "把文本内容写入沙箱目录下的文件。path 只写文件名，不要带路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件名，如 report.txt"},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取沙箱目录下的文件内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件名，如 report.txt"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sqlite_query",
            "description": "查询本地 SQLite 数据库 office.db，含 departments、employees 两张表。只允许 SELECT。",
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "SELECT 语句"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索外部信息，返回摘要。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
]

EXECUTORS = {
    "calculator": lambda a: calculator(a["expression"]),
    "current_date": lambda a: current_date(),
    "file_write": lambda a: file_write(a["path"], a["content"]),
    "file_read": lambda a: file_read(a["path"]),
    "sqlite_query": lambda a: sqlite_query(a["sql"]),
    "web_search": lambda a: web_search(a["query"]),
}

SCHEMAS = {s["function"]["name"]: s["function"]["parameters"] for s in SPECS}


def validate_args(name: str, args: dict):
    """按 JSON Schema 做参数校验 —— 这是「结果校验」的第一道关口。

    返回 (ok, message)。故意做得宽松：缺必要参数就报错让模型重试，
    而不是让工具抛异常后整个 agent 崩掉。
    """
    schema = SCHEMAS.get(name)
    if schema is None:
        return False, f"未知工具 {name}，可选工具: {', '.join(SCHEMAS)}"
    if not isinstance(args, dict):
        return False, f"参数必须是对象，收到 {type(args).__name__}"

    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return False, f"缺少必填参数: {', '.join(missing)}"

    props = schema.get("properties", {})
    for k, v in args.items():
        if k not in props:
            return False, f"多余参数 {k}，该工具只接受: {', '.join(props) or '无'}"
        expect = props[k].get("type")
        actual = "integer" if isinstance(v, bool) is False and isinstance(v, int) else type(v).__name__
        actual = {
            "str": "string", "int": "integer", "float": "number",
            "bool": "boolean", "dict": "object", "list": "array",
        }.get(type(v).__name__, type(v).__name__)
        if expect and expect != actual:
            return False, f"参数 {k} 类型应为 {expect}，实际是 {actual}"
    return True, ""


def execute(name: str, args: dict):
    fn = EXECUTORS.get(name)
    if fn is None:
        return {"ok": False, "result": f"未知工具 {name}"}
    try:
        return fn(args)
    except KeyError as e:
        return {"ok": False, "result": f"参数缺失: {e}"}
    except Exception as e:  # 工具异常不应该炸掉整个 loop
        return {"ok": False, "result": f"{name} 执行异常: {type(e).__name__}: {e}"}
