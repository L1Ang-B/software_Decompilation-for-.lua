#!/usr/bin/env python3
"""decompiler_api.py - 与 GUI 完全解耦的 LuaJIT 反编译调用接口。

设计目标：
  * 入口只接收「文件路径 + 输出路径 + 选项」，不感知任何 GUI/CLI。
  * 不使用 argparse，不使用 main.py 的 Main 类（其构造函数会 parse_args/sys.exit）。
  * 内部封装全局可变状态（ljd.CURRENT_VERSION 及模块级开关）与 stdout/stderr 捕获。

示例：
    from decompiler_api import decompile_file, DecompileOptions
    res = decompile_file("in.luac", "out.lua", DecompileOptions(catch_asserts=True))
    if not res.ok:
        print(res.error, res.log)
"""
from __future__ import annotations

import io
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import List, Optional

import ljd
import ljd.rawdump.code
import ljd.rawdump.parser
import ljd.ast.builder
import ljd.ast.validator
import ljd.ast.mutator
import ljd.ast.locals
import ljd.ast.slotworks
import ljd.ast.unwarper
import ljd.pseudoasm.instructions
import ljd.pseudoasm.writer
import ljd.lua.writer
from ljd.ast import nodes

_MAGIC = b"\x1bLJ"
# header.version 字节 -> LuaJIT 版本（对应 main.py:346-351）
_VERSION_MAP = {1: 2.0, 2: 2.1}

# 保护 ljd.CURRENT_VERSION 与模块级开关，避免并发反编译互相污染。
_GLOBAL_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class DecompileOptions:
    """反编译选项。默认值与 main.py 的 CLI 默认值一致。"""
    catch_asserts: bool = True          # main.py:111 -> 出错降级为警告
    no_unwarp: bool = False             # main.py:141 -> 跳过反结构化
    unsafe_extra_pass: bool = True      # main.py:128 -> 额外 slot 消除
    with_line_numbers: bool = False     # main.py:116 -> 函数定义附行号
    output_pseudoasm: bool = False      # main.py:145 -> 额外输出伪汇编


@dataclass
class DecompileResult:
    ok: bool
    output_path: Optional[str] = None   # 成功且落盘时的产物路径
    source: Optional[str] = None        # 反编译出的 Lua 源码文本
    pseudoasm: Optional[str] = None     # options.output_pseudoasm=True 时的伪汇编
    log: str = ""                       # 采集到的 stdout/stderr（含 errprint）
    error: Optional[str] = None         # 失败原因
    bytecode_version: Optional[float] = None   # 2.0 / 2.1
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 只读探测
# --------------------------------------------------------------------------- #
def detect_version(path: str) -> Optional[float]:
    """仅读文件头，返回 2.0 / 2.1；非法或未知返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return None
    if len(head) < 4 or head[:3] != _MAGIC:
        return None
    return _VERSION_MAP.get(head[3])


def is_supported_file(path: str) -> bool:
    """拖拽入 GUI 时的即时校验。"""
    return detect_version(path) is not None


# --------------------------------------------------------------------------- #
# 运行时状态
# --------------------------------------------------------------------------- #
def _set_luajit_version(bc_version: float) -> None:
    """复刻 main.py:56-72。调用方需持有 _GLOBAL_LOCK。"""
    if ljd.CURRENT_VERSION == bc_version:
        return
    ljd.CURRENT_VERSION = bc_version
    if bc_version == 2.0:
        from ljd.rawdump.luajit.v2_0.luajit_opcode import _OPCODES as opcodes
    elif bc_version == 2.1:
        from ljd.rawdump.luajit.v2_1.luajit_opcode import _OPCODES as opcodes
    else:
        raise ValueError("Unknown LuaJIT opcode module for version " + str(bc_version))
    ljd.rawdump.code.init(opcodes)
    ljd.ast.builder.init()
    ljd.pseudoasm.instructions.init()


def _apply_global_flags(options: DecompileOptions) -> None:
    """复刻 main.py:185-197。调用方需持有 _GLOBAL_LOCK。"""
    ljd.ast.builder.handle_invalid_functions = options.catch_asserts
    for mod in (ljd.ast.unwarper, ljd.ast.slotworks, ljd.ast.validator):
        mod.catch_asserts = options.catch_asserts
    ljd.lua.writer.show_line_info = options.with_line_numbers


# --------------------------------------------------------------------------- #
# 核心流水线（复刻 Main.decompile, main.py:342-450）
# --------------------------------------------------------------------------- #
def _run_pipeline(file_in: str, options: DecompileOptions, fd_out) -> DecompileResult:
    buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    try:
        _apply_global_flags(options)

        version_holder: List[float] = []

        def on_parse_header(preheader):
            if preheader.version not in _VERSION_MAP:
                raise ValueError("Unsupported bytecode version: " + str(preheader.version))
            bc_version = _VERSION_MAP[preheader.version]
            version_holder.append(bc_version)
            _set_luajit_version(bc_version)

        header, prototype = ljd.rawdump.parser.parse(file_in, on_parse_header)
        if not prototype:
            return DecompileResult(ok=False, log=buf.getvalue(),
                                   error="parse failed: not a valid LuaJIT dump")

        ast = ljd.ast.builder.build(header, prototype)
        assert ast is not None

        ljd.ast.validator.validate(ast, warped=True)
        ljd.ast.mutator.pre_pass(ast)
        ljd.ast.validator.validate(ast, warped=True)
        ljd.ast.locals.mark_locals(ast)

        try:
            ljd.ast.slotworks.eliminate_temporary(ast, identify_slots=True)
        except AssertionError:
            if not options.catch_asserts:
                raise
            print("-- Decompilation Error: slotworks.eliminate_temporary")

        if not options.no_unwarp:
            ljd.ast.unwarper.unwarp(ast, False)
            ljd.ast.locals.mark_local_definitions(ast)
            ljd.ast.mutator.primary_pass(ast)

            try:
                ljd.ast.validator.validate(ast, warped=False)
            except AssertionError:
                if not options.catch_asserts:
                    raise
                print("-- Decompilation Error: validator.validate(warped=False)")

            ljd.ast.locals.mark_locals(ast, alt_mode=True)
            ljd.ast.locals.mark_local_definitions(ast)

            # 额外(unsafe) slot 消除（main.py:410-448）
            for ass in (ast.statements.contents if options.unsafe_extra_pass else []):
                if not isinstance(ass, nodes.Assignment):
                    continue
                for node in ass.expressions.contents:
                    if not getattr(node, "_debuginfo", False) or not node._debuginfo.variable_info:
                        continue
                    if isinstance(node, nodes.FunctionDefinition):
                        contents = [node.statements.contents]
                    elif isinstance(node, nodes.TableConstructor):
                        contents = [node.array.contents, node.records.contents]
                    else:
                        continue
                    try:
                        for content_list in contents:
                            for subnode in content_list:
                                if isinstance(subnode, nodes.Assignment):
                                    for dst in subnode.destinations.contents:
                                        if isinstance(dst, nodes.Identifier) and dst.type == dst.T_SLOT:
                                            raise StopIteration
                    except StopIteration:
                        ljd.ast.slotworks.eliminate_temporary(node, unwarped=True, safe_mode=False)
                        for content_list in contents:
                            j = len(content_list) - 1
                            for i, subnode in enumerate(reversed(content_list)):
                                if getattr(subnode, "_invalidated", False):
                                    del content_list[j - i]

        pseudoasm = None
        if options.output_pseudoasm:
            pa_buf = io.StringIO()
            ljd.pseudoasm.writer.write(pa_buf, header, prototype)
            pseudoasm = pa_buf.getvalue()

        source = io.StringIO()
        ljd.lua.writer.write(source, ast)

        if fd_out is not None:
            fd_out.write(source.getvalue())

        return DecompileResult(ok=True, source=source.getvalue(), pseudoasm=pseudoasm,
                               log=buf.getvalue(),
                               bytecode_version=version_holder[0] if version_holder else None)
    except Exception as e:  # noqa: BLE001 - 边界处统一转成结果对象，保留已捕获日志
        return DecompileResult(ok=False, log=buf.getvalue(), error=repr(e))
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def decompile_file(path_in: str, path_out: str,
                   options: Optional[DecompileOptions] = None) -> DecompileResult:
    """反编译单文件并写入 path_out。线程安全。"""
    options = options or DecompileOptions()
    if detect_version(path_in) is None:
        return DecompileResult(ok=False, error="not a supported LuaJIT dump (magic/version)")

    with _GLOBAL_LOCK:
        try:
            parent = os.path.dirname(os.path.abspath(path_out))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path_out, "w", encoding="utf8") as fd:
                res = _run_pipeline(path_in, options, fd)
        except OSError as e:
            return DecompileResult(ok=False, error=repr(e))
    res.output_path = path_out if res.ok else None
    return res


def decompile_text(path_in: str,
                   options: Optional[DecompileOptions] = None) -> DecompileResult:
    """反编译但不落盘，源码放在 result.source 中（供 GUI 预览）。线程安全。"""
    options = options or DecompileOptions()
    if detect_version(path_in) is None:
        return DecompileResult(ok=False, error="not a supported LuaJIT dump (magic/version)")

    with _GLOBAL_LOCK:
        return _run_pipeline(path_in, options, None)