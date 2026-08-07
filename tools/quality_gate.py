#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mx-stocks-monitor 代码质量守卫（CI / pre-commit 通用，纯标准库，无第三方依赖）。

退出码：
  0 = 全部通过
  1 = 发现阻断性问题

检查项：
  1. 任何 CSV 不得带 UTF-8 BOM（防观察池被清空）。
  2. 仓库内所有 .py 必须通过 py_compile（跳过 .venv / __pycache__ / .git / .workbuddy）。
  3. 主 CSV 必须能被 utf-8-sig 正常解析（首列=代码，无 BOM 残留）。
  4. 不得出现「无头/云端环境下 sys.exit(0) 伪造成功」的反模式。

设计依据见 docs/CODE_QUALITY.md（红线 1/3）。
"""
import csv
import os
import py_compile
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_CSV = os.path.join(ROOT, "mx_stocks_screener", "观察池_累计.csv")
SCREENER = os.path.join(ROOT, "screener_monitor.py")

SKIP_DIRS = (".venv", "__pycache__", ".git", ".workbuddy")

problems = []


def err(msg):
    problems.append(msg)
    print("❌ " + msg)


def ok(msg):
    print("✅ " + msg)


def _iter_py():
    for dp, _, fns in os.walk(ROOT):
        if any(seg in dp.split(os.sep) for seg in SKIP_DIRS):
            continue
        for fn in fns:
            if fn.endswith(".py"):
                yield os.path.join(dp, fn)


def _iter_csv():
    target = os.path.join(ROOT, "mx_stocks_screener")
    if not os.path.isdir(target):
        return
    for dp, _, fns in os.walk(target):
        for fn in fns:
            if fn.lower().endswith(".csv"):
                yield os.path.join(dp, fn)


def check_bom():
    bad = []
    total = 0
    for p in _iter_csv():
        total += 1
        with open(p, "rb") as f:
            if f.read(3) == b"\xef\xbb\xbf":
                bad.append(os.path.relpath(p, ROOT))
    if bad:
        err("以下 CSV 带 UTF-8 BOM（会破坏首列解析、可能清空观察池）：" + ", ".join(bad))
    else:
        ok(f"CSV BOM 检查通过（扫描 {total} 个文件）")


def check_syntax():
    failed = []
    total = 0
    for p in _iter_py():
        total += 1
        try:
            py_compile.compile(p, doraise=True)
        except py_compile.PyCompileError as e:
            last = str(e).strip().splitlines()[-1] if str(e).strip() else "unknown"
            failed.append(os.path.relpath(p, ROOT) + " -> " + last)
    if failed:
        err("Python 语法错误：\n  " + "\n  ".join(failed))
    else:
        ok(f"Python 语法检查通过（{total} 个文件）")


def check_master_parseable():
    if not os.path.exists(MASTER_CSV):
        ok("主 CSV 尚不存在，跳过解析检查")
        return
    try:
        with open(MASTER_CSV, encoding="utf-8-sig") as f:
            r = csv.DictReader(f)
            first = list(r.fieldnames or [])
            # 跳过空行与 # 开头标记行（观察池的日期分隔行），只统计真实数据行
            n = sum(1 for row in r
                    if (row.get("代码") or "").strip()
                    and not (row.get("代码") or "").lstrip().startswith("#"))
        if not first or first[0] != "代码":
            err(f"主 CSV 首列应为「代码」，实际为 {first[:1]}")
        else:
            ok(f"主 CSV 解析正常：首列=代码，数据行={n}")
    except Exception as e:  # noqa: BLE001
        err(f"主 CSV 解析失败：{e}")


def check_sys_exit_zero():
    if not os.path.exists(SCREENER):
        return
    with open(SCREENER, encoding="utf-8") as f:
        lines = f.readlines()
    pat = re.compile(r"sys\.exit\(\s*0\s*\)")
    # 允许的人工交互标记：本地扫码/输入/云端显式报错守卫
    allowed = re.compile(r"GITHUB_ACTIONS|# 人工交互|_print_auth|input\(")
    flagged = []
    for i, ln in enumerate(lines, 1):
        stripped = ln.lstrip()
        # 跳过纯注释行（避免注释里提到 sys.exit(0) 被误判）
        if stripped.startswith("#"):
            continue
        if pat.search(ln):
            window = "".join(lines[max(0, i - 4):i])
            if not allowed.search(window):
                flagged.append(i)
    if flagged:
        err("检测到可能在无头/云端环境伪造成功的 sys.exit(0)（附近无 GITHUB_ACTIONS 守卫/"
            "人工交互标记），请改为 raise 或仅在本地交互分支使用：行 " + ", ".join(map(str, flagged)))
    else:
        ok("无头环境 sys.exit(0) 反模式检查通过")


if __name__ == "__main__":
    print("=== mx-stocks-monitor 代码质量守卫 ===")
    check_bom()
    check_syntax()
    check_master_parseable()
    check_sys_exit_zero()
    print()
    if problems:
        print(f"❌ 守卫未通过，共 {len(problems)} 项阻断问题。请修复后再提交。")
        sys.exit(1)
    print("✅ 守卫全部通过。")
