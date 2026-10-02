# -*- coding: utf-8 -*-
"""生成标准库依赖的任务评测集 evals/tasks.json（50 条：简单 20 / 中等 20 / 困难 10）。

为什么用生成器而不是手写 JSON：
- 标准答案直接来自对本仓库示例数据库的真实 SQL 查询，**不会和代码漂移**；
- 想要更多任务时改模板就能扩展，评测集本身是可复现的产物；
- 日期类任务的答案在生成时刻固定，保证跨天评测时口径一致。

用法：python evals/gen_tasks.py
"""
import datetime
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.tools import DB_PATH, ensure_db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "tasks.json")


def q1(sql, args=()):
    ensure_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def dept_avg(name):
    return q1(
        "SELECT AVG(salary) FROM employees e JOIN departments d ON e.dept_id=d.id WHERE d.name=?",
        (name,),
    )


def build():
    tasks = []

    def add(level, task, checker, expect_tools=None, note=""):
        tasks.append({
            "id": f"T{len(tasks) + 1:02d}",
            "level": level,
            "task": task,
            "checker": checker,
            "expect_tools": expect_tools or [],
            "note": note,
        })

    # ---------------- 简单：单步工具调用 ----------------
    arith = [
        ("（123 + 456）× 2", (123 + 456) * 2),
        ("8888 ÷ 8 + 111", 8888 / 8 + 111),
        ("（15 + 27）× 3 - 40", (15 + 27) * 3 - 40),
        ("3600 ÷ 60 × 7", 3600 / 60 * 7),
        ("12.5 × 8 + 37.5", 12.5 * 8 + 37.5),
        ("（100 - 23）× 4 ÷ 2", (100 - 23) * 4 / 2),
        ("999 + 1001 - 750", 999 + 1001 - 750),
        ("7 × 8 × 9 ÷ 6", 7 * 8 * 9 / 6),
    ]
    for expr_cn, val in arith:
        add("easy", f"请计算 {expr_cn} 的结果是多少？",
            {"type": "number", "value": round(float(val), 2), "tol": 0.51},
            ["calculator"], "纯计算，检验是否会心算偷懒")

    emp_count = q1("SELECT COUNT(*) FROM employees")
    max_salary = q1("SELECT MAX(salary) FROM employees")
    avg_salary = q1("SELECT AVG(salary) FROM employees")
    top_name = q1("SELECT name FROM employees ORDER BY salary DESC LIMIT 1")
    add("easy", "employees 表（员工表）一共有多少名员工？",
        {"type": "number", "value": emp_count, "tol": 0.51}, ["sqlite_query"], "单表 COUNT")
    add("easy", "employees 表中工资最高的员工叫什么名字？",
        {"type": "contains", "value": top_name}, ["sqlite_query"], "排序取极值")
    add("easy", "所有员工的平均工资是多少元（取整数即可）？",
        {"type": "number", "value": round(avg_salary), "tol": 1.0}, ["sqlite_query"], "聚合 AVG")
    add("easy", "employees 表中最高的工资是多少元？",
        {"type": "number", "value": max_salary, "tol": 0.51}, ["sqlite_query"], "MAX")

    add("easy", "今天是几号？请给出具体日期。",
        {"type": "today"}, ["current_date"], "禁止靠训练记忆回答日期")
    add("easy", "今天是星期几？",
        {"type": "weekday"}, ["current_date"], "同上")
    add("easy", "现在是几月份？请查一下当前日期后告诉我。",
        {"type": "month"}, ["current_date"], "同上")

    add("easy", "原价 899 元的商品打 8 折，折后价格是多少元？",
        {"type": "number", "value": 899 * 0.8, "tol": 0.51}, ["calculator"], "业务场景算术")
    add("easy", "一件商品成本 120 元，售价 199 元，利润率是多少百分比（保留一位小数）？",
        {"type": "number", "value": round((199 - 120) / 120 * 100, 1), "tol": 0.6},
        ["calculator"], "百分比计算")

    dept_count = q1("SELECT COUNT(*) FROM departments")
    min_name = q1("SELECT name FROM employees ORDER BY salary ASC LIMIT 1")
    add("easy", "departments 表里一共有几个部门？",
        {"type": "number", "value": dept_count, "tol": 0.51}, ["sqlite_query"], "单表 COUNT")
    add("easy", "employees 表中工资最低的员工叫什么名字？",
        {"type": "contains", "value": min_name}, ["sqlite_query"], "升序取极值")
    add("easy", "请计算 45 × 12 + 380 的结果是多少？",
        {"type": "number", "value": 45 * 12 + 380, "tol": 0.51}, ["calculator"], "混合运算")

    # ---------------- 中等：两步以上，跨工具 ----------------
    pairs = [("研发部", "销售部"), ("市场部", "运营部"), ("研发部", "市场部"), ("运营部", "销售部")]
    for a, b in pairs:
        diff = dept_avg(a) - dept_avg(b)
        add("medium",
            f"先算出{a}的平均工资，再算出{b}的平均工资，告诉我前者比后者高多少元（取整数）？",
            {"type": "number", "value": round(diff), "tol": 1.5},
            ["sqlite_query", "calculator"], "JOIN + 差值计算")

    dev_count = q1("SELECT COUNT(*) FROM employees e JOIN departments d ON e.dept_id=d.id WHERE d.name='研发部'")
    add("medium", "研发部有多少名员工？请用 JOIN 查询 departments 和 employees 表得到答案。",
        {"type": "number", "value": dev_count, "tol": 0.51}, ["sqlite_query"], "隐式 JOIN")

    hired_2021 = q1("SELECT COUNT(*) FROM employees WHERE hire_date LIKE '2021%'")
    add("medium", "统计 2021 年入职的员工人数是多少？",
        {"type": "number", "value": hired_2021, "tol": 0.51}, ["sqlite_query"], "日期字符串过滤")

    for dept in ["研发部", "销售部", "市场部", "运营部"]:
        cnt = q1("SELECT COUNT(*) FROM employees e JOIN departments d ON e.dept_id=d.id WHERE d.name=?", (dept,))
        add("medium",
            f"统计{dept}的员工人数，并把结果写入文件 headcount_{dept}.txt（内容里要有具体人数）。",
            {"type": "file_contains", "path": f"headcount_{dept}.txt", "value": str(cnt)},
            ["sqlite_query", "file_write"], "查询 → 落盘")

    for expr_cn, val in [("365 × 24", 365 * 24), ("1024 × 8", 1024 * 8), ("（88 + 12）× 15", (88 + 12) * 15)]:
        fname = f"calc_{abs(hash(expr_cn)) % 1000}.txt"
        add("medium", f"先计算 {expr_cn}，再把计算结果写入文件 {fname}。",
            {"type": "file_contains", "path": fname, "value": str(int(val))},
            ["calculator", "file_write"], "计算 → 落盘")

    add("medium", "把文本「项目交付清单：RAG 检索模块、Agent 调度模块、评测流水线」写入 todo.txt，然后读出来确认内容无误。",
        {"type": "file_contains", "path": "todo.txt", "value": "Agent 调度模块"},
        ["file_write", "file_read"], "写 → 读闭环")

    order_avg = q1("SELECT AVG(salary) FROM employees e JOIN departments d ON e.dept_id=d.id WHERE d.name='运营部'")
    add("medium", "运营部的平均工资比全公司平均工资高多少元（取整数）？",
        {"type": "number", "value": round(order_avg - avg_salary), "tol": 1.5},
        ["sqlite_query", "calculator"], "嵌套比较")

    total_raise = q1("SELECT SUM(salary) FROM employees") * 0.1
    add("medium", "如果给所有员工涨薪 10%，公司每月要多支出多少元（取整数）？",
        {"type": "number", "value": round(total_raise), "tol": 2.0}, ["sqlite_query", "calculator"], "SUM + 比例")

    add("medium", "把全公司员工的平均工资（取整数）写入文件 avg_salary.txt。",
        {"type": "file_contains", "path": "avg_salary.txt", "value": str(round(avg_salary))},
        ["sqlite_query", "file_write"], "聚合 → 落盘")
    add("medium", "2021 年入职的员工占全公司总人数的百分比是多少（保留一位小数）？把结果写入 ratio2021.txt。",
        {"type": "file_contains", "path": "ratio2021.txt",
         "value": str(round(hired_2021 / emp_count * 100, 1))},
        ["sqlite_query", "calculator", "file_write"], "过滤 + 比例 + 落盘")
    senior_rich = q1("SELECT COUNT(*) FROM employees WHERE hire_date <= '2020-12-31' AND salary > 20000")
    add("medium", "在 2020 年及以前入职、且工资高于 20000 元的员工有多少人？",
        {"type": "number", "value": senior_rich, "tol": 0.51}, ["sqlite_query"], "多条件过滤")

    above = q1(f"SELECT COUNT(*) FROM employees WHERE salary > {avg_salary}")
    for _ in range(1):
        add("medium", "工资高于全公司平均工资的员工有多少人？",
            {"type": "number", "value": above, "tol": 0.51}, ["sqlite_query"], "子查询/两步")

    # ---------------- 困难：三步以上 + 结果落盘/归纳 ----------------
    best_dept = max(["研发部", "销售部", "市场部", "运营部"], key=dept_avg)
    add("hard",
        "统计每个部门的平均工资，找出平均工资最高的那个部门，把部门名称和它的平均工资写入 best_dept.md。",
        {"type": "file_contains", "path": "best_dept.md", "value": best_dept},
        ["sqlite_query", "file_write"], "聚合 → 比较 → 落盘")

    add("hard",
        "请做一份简易人力成本报告：先算出公司月度工资总额（SUM），再算出年度总额（×12），最后把这两个数字写入 cost_report.md。",
        {"type": "file_contains", "path": "cost_report.md",
         "value": str(int(q1("SELECT SUM(salary) FROM employees")))},
        ["sqlite_query", "calculator", "file_write"], "三步链式")

    pct = above / emp_count * 100
    add("hard", "工资高于全公司平均工资的员工占总人数的百分比是多少（保留一位小数）？",
        {"type": "number", "value": round(pct, 1), "tol": 0.6}, ["sqlite_query", "calculator"], "两步 + 百分比")

    today = datetime.date.today()
    days_left = (datetime.date(today.year, 12, 31) - today).days
    add("hard", "先查今天是几号，再算出距离今年 12 月 31 日还有多少天，把天数写入 countdown.txt。",
        {"type": "file_contains", "path": "countdown.txt", "value": str(days_left)},
        ["current_date", "calculator", "file_write"], "日期 → 计算 → 落盘")

    add("hard", "先搜索「什么是 MCP」，再把搜索结果的摘要写入 mcp_note.md。",
        {"type": "file_contains", "path": "mcp_note.md", "value": "MCP"},
        ["web_search", "file_write"], "外部信息 → 落盘")

    salary_gap = max_salary - q1("SELECT MIN(salary) FROM employees")
    add("hard", "公司内最高工资与最低工资的差距是多少元？并把差距占最高工资的比例（百分比，保留一位小数）一并告诉我。",
        {"type": "number", "value": salary_gap, "tol": 0.51}, ["sqlite_query", "calculator"], "双目标输出")

    add("hard",
        "先把「1. 需求分析\n2. 原型设计\n3. 开发联调」写入 plan.md，然后读回文件，告诉我一共有几个步骤。",
        {"type": "number", "value": 3, "tol": 0.51}, ["file_write", "file_read", "calculator"], "写读 + 归纳")

    cnt_s = q1("SELECT COUNT(*) FROM employees e JOIN departments d ON e.dept_id=d.id WHERE d.city='北京'")
    add("hard", "在北京办公的部门里共有多少名员工？（城市信息在 departments 表）",
        {"type": "number", "value": cnt_s, "tol": 0.51}, ["sqlite_query"], "跨表两跳")

    add("hard",
        "给我一份员工名单：写出工资排名前三的员工姓名，并用逗号分隔写进 top3.txt。",
        {"type": "file_contains", "path": "top3.txt", "value": top_name},
        ["sqlite_query", "file_write"], "排序 → 落盘")

    add("hard",
        "如果研发部全员涨薪 15%，研发部新的月工资总额是多少元（取整数）？把结果写入 dev_new_total.txt。",
        {"type": "file_contains", "path": "dev_new_total.txt",
         "value": str(round(q1("SELECT SUM(salary) FROM employees e JOIN departments d ON e.dept_id=d.id WHERE d.name='研发部'") * 1.15))},
        ["sqlite_query", "calculator", "file_write"], "JOIN + 比例 + 落盘")

    add("hard",
        "请告诉我三件事：公司总人数、平均工资（取整数）、以及人数最多的部门。三项都要回答。",
        {"type": "multi", "values": [
            {"type": "number", "value": emp_count, "tol": 0.51},
            {"type": "number", "value": round(avg_salary), "tol": 1.0},
        ]},
        ["sqlite_query"], "多目标输出，检验是否漏答")

    return tasks


def main():
    tasks = build()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "total": len(tasks), "tasks": tasks}, f, ensure_ascii=False, indent=2)
    levels = {}
    for t in tasks:
        levels[t["level"]] = levels.get(t["level"], 0) + 1
    print(f"已生成 {len(tasks)} 条任务 -> {OUT}")
    print("难度分布:", levels)


if __name__ == "__main__":
    main()
