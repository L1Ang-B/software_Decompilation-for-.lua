#!/usr/bin/env python3
"""baseline_semantic.py - LJD 语义回归基线。

对 test/tests/ 下每个 .lua：
  1) luajit 不带 -g 编译 -> ref.bc（参照）
  2) luajit 带   -g 编译 -> in.bc （给反编译器提供调试信息）
  3) python main.py 反编译 in.bc -> out.lua
  4) luajit 不带 -g 编译 out.lua -> rc.bc
  5) 比较 rc.bc 与 ref.bc 是否逐字节一致

设计说明：仓库自带 harness 对「带 -g 输入 / 剥离比对」是不对称的——它直接剥离输入，
导致局部变量无法还原。本脚本用 -g 输入保证反编译器拿到调试信息，再用剥离字节码比对，
从而真正衡量「反编译是否语义等价」。

用法:
    python baseline_semantic.py [--luajit PATH] [--tests-dir DIR]
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def find_luajit(explicit):
    if explicit:
        return explicit
    found = shutil.which("luajit")
    if not found:
        sys.exit("error: 找不到 luajit，请用 --luajit 指定路径，或将其加入 PATH")
    return found


def run(args):
    return subprocess.run(args, capture_output=True, text=True)


def compile_bc(luajit, src, dst, debug):
    args = [luajit, "-b", "-t", "raw", str(src)]
    if debug:
        args.append("-g")
    args.append(str(dst))
    return run(args)


def decompile(src_bc, out_lua):
    return run([sys.executable, str(HERE / "main.py"), "-f", str(src_bc), "-o", str(out_lua)])


def first_diff(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n if len(a) != len(b) else -1


def main():
    ap = argparse.ArgumentParser(description="LJD semantic round-trip baseline")
    ap.add_argument("--luajit", default=None, help="luajit 可执行文件路径（默认从 PATH 查找）")
    ap.add_argument("--tests-dir", default=str(HERE / "test" / "tests"), help="测试目录")
    args = ap.parse_args()

    luajit = find_luajit(args.luajit)
    tests_dir = Path(args.tests_dir)
    sources = sorted(tests_dir.rglob("*.lua"))
    if not sources:
        sys.exit("error: 在 %s 下没有找到 .lua" % tests_dir)

    workdir = Path(tempfile.mkdtemp(prefix="ljd-sem-"))
    print("luajit    : %s" % luajit)
    print("tests dir : %s" % tests_dir)
    print("workdir   : %s\n" % workdir)
    print("%-28s %-6s %8s %8s %s" % ("TEST", "RESULT", "rc", "ref", "NOTE"))
    print("-" * 78)

    passed = 0
    for src in sources:
        name = str(src.relative_to(tests_dir)).replace(os.sep, "/")
        tag = name[:-4].replace("/", "_")
        ref = workdir / (tag + "_ref.bc")
        gin = workdir / (tag + "_g.bc")
        out = workdir / (tag + ".lua")
        rc = workdir / (tag + "_rc.bc")

        note = ""
        result = "FAIL"

        r1 = compile_bc(luajit, src, ref, debug=False)
        if not ref.is_file() or ref.stat().st_size == 0:
            result, note = "ERROR", "ref compile failed: %s" % (r1.stderr.strip()[:60])

        if result != "ERROR":
            compile_bc(luajit, src, gin, debug=True)
            rd = decompile(gin, out)
            if not out.is_file() or out.stat().st_size == 0:
                result, note = "ERROR", "decompile failed/empty"

        if result != "ERROR":
            compile_bc(luajit, out, rc, debug=False)
            if not rc.is_file() or rc.stat().st_size == 0:
                result, note = "ERROR", "rc compile failed (decompiled output invalid?)"

        if result != "ERROR":
            a = rc.read_bytes()
            b = ref.read_bytes()
            if a == b:
                result, passed = "PASS", passed + 1
            else:
                d = first_diff(a, b)
                note = "diff@%d" % d

        size_rc = rc.stat().st_size if rc.is_file() else 0
        size_ref = ref.stat().st_size if ref.is_file() else 0
        print("%-28s %-6s %8d %8d %s" % (name, result, size_rc, size_ref, note))

    print("-" * 78)
    print("semantic round-trip passed: %d / %d" % (passed, len(sources)))
    return 0 if passed == len(sources) else 1


if __name__ == "__main__":
    sys.exit(main())