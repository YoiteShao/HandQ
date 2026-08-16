#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe.py — device-ops 环境自检

检查运行 device-ops 所需的全部前提条件:
  - TAC 板连接
  - 设备状态
  - 必需工具可用性
  - QPM 安装
  - meta 路径可达 (可选)

CLI:
  probe.py                             # 基础检查
  probe.py --meta "\\server\share\..."  # 额外检查 meta 路径
"""

import argparse
import logging
import os
import subprocess
import sys
from shutil import which
from typing import Optional

log = logging.getLogger(__name__)

# ============================================================================
# TAC 板 VID/PID 指纹
# ============================================================================

KNOWN_TAC_BOARDS = {
    (0x04D8, 0x000A): "Bantam (nordy)",
    (0x05C6, 0x9302): "Alpaca (lemans_sx)",
}


# ============================================================================
# 检查函数
# ============================================================================

def _run_quiet(cmd, timeout=10):
    """执行命令，返回 (stdout, returncode)。"""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout, check=False)
        return proc.stdout.strip(), proc.returncode
    except FileNotFoundError:
        return "", -1
    except subprocess.TimeoutExpired:
        return "", -2


def check_pyserial() -> tuple:
    """检查 pyserial 是否已安装。"""
    try:
        import serial
        import serial.tools.list_ports
        return True, f"pyserial {serial.VERSION}"
    except ImportError:
        return False, "未安装 (pip install pyserial)"


def check_tac_board() -> tuple:
    """检测 TAC 板连接。"""
    try:
        import serial.tools.list_ports
    except ImportError:
        return False, "无法检测 (pyserial 未安装)"

    for p in serial.tools.list_ports.comports():
        if p.vid is None:
            continue
        key = (p.vid, p.pid)
        if key in KNOWN_TAC_BOARDS:
            name = KNOWN_TAC_BOARDS[key]
            sn = p.serial_number or "N/A"
            return True, f"{p.device} — {name} [VID:PID={p.vid:04X}:{p.pid:04X}] SN={sn}"

    return False, "未发现已知 TAC 板 (已知: " + ", ".join(
        f"{v:04X}:{p:04X}" for v, p in KNOWN_TAC_BOARDS) + ")"


def check_device_state() -> tuple:
    """检测设备当前状态。"""
    # 尝试 adb
    adb = which("adb") or which("adb.exe")
    if adb:
        out, rc = _run_quiet([adb, "devices"])
        if rc == 0 and out:
            for line in out.splitlines()[1:]:
                if "\tdevice" in line:
                    serial_id = line.split("\t")[0]
                    return True, f"adb 在线 (serial={serial_id})"
                if "\trecovery" in line:
                    return True, "recovery 模式"
                if "\toffline" in line:
                    return True, "adb offline (USB 连接但未响应)"

    # 尝试 fastboot
    fb = which("fastboot") or which("fastboot.exe")
    if fb:
        out, rc = _run_quiet([fb, "devices"])
        if rc == 0 and out.strip():
            return True, "fastboot 模式"

    # 尝试 EDL (9008)
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "") + " " + (p.hwid or "")
            if "9008" in desc or "QDLoader" in desc:
                return True, f"EDL 模式 ({p.device})"
    except ImportError:
        pass

    return False, "未检测到设备 (absent)"


def check_tool(name: str, version_arg: str = "--version") -> tuple:
    """检查某个工具是否在 PATH 中可用。"""
    path = which(name) or which(name + ".exe")
    if not path:
        return False, f"未找到 ({name})"
    out, rc = _run_quiet([path, version_arg])
    # 有些工具 --version 返回非零但仍有输出
    version_hint = out.splitlines()[0][:80] if out else "(无版本信息)"
    return True, f"{path} — {version_hint}"


def check_qpm() -> tuple:
    """检查 QPM CLI 是否已安装。"""
    path = which("qpm-cli") or which("qpm-cli.exe")
    if not path:
        return False, "qpm-cli 未找到 (需安装 QPM)"
    out, rc = _run_quiet([path, "--version"])
    ver = out.strip().splitlines()[0] if out else "unknown"
    return True, f"{path} — {ver}"


def check_meta_path(meta: str) -> tuple:
    """检查 meta 路径是否可达。"""
    if not os.path.isdir(meta):
        return False, f"路径不可达: {meta}"
    # 检查 meta_cli
    meta_cli_win = os.path.join(meta, "common", "build", "app", "windows_x86_64", "meta_cli.exe")
    meta_cli_linux = os.path.join(meta, "common", "build", "app", "linux", "meta_cli")
    if os.path.isfile(meta_cli_win):
        return True, f"可达, meta_cli 存在: {meta_cli_win}"
    elif os.path.isfile(meta_cli_linux):
        return True, f"可达, meta_cli 存在: {meta_cli_linux}"
    else:
        return True, f"可达, 但未找到 meta_cli (可能结构不标准)"


# ============================================================================
# 主流程
# ============================================================================

def run_probe(meta: Optional[str] = None) -> int:
    """
    执行所有检查，打印结果。
    返回失败项数量。
    """
    checks = []

    # 1. pyserial
    checks.append(("pyserial 模块", check_pyserial()))

    # 2. TAC 板
    checks.append(("TAC 控制板", check_tac_board()))

    # 3. 设备状态
    checks.append(("设备连接", check_device_state()))

    # 4. 必需工具
    tools = [
        ("fh_loader", "--version"),
        ("QSaharaServer", "--version"),
        ("fastboot", "--version"),
        ("adb", "--version"),
    ]
    for tool_name, ver_arg in tools:
        checks.append((tool_name, check_tool(tool_name, ver_arg)))

    # 5. QPM
    checks.append(("QPM (qpm-cli)", check_qpm()))

    # 6. meta 路径 (可选)
    if meta:
        checks.append(("Meta 路径", check_meta_path(meta)))

    # 输出
    print()
    print("=" * 60)
    print("  device-ops 环境自检")
    print("=" * 60)
    print()

    passed = 0
    failed = 0
    for name, (ok, detail) in checks:
        mark = "✓" if ok else "✗"
        status = "PASS" if ok else "FAIL"
        print(f"  {mark} [{status}] {name}")
        print(f"         {detail}")
        print()
        if ok:
            passed += 1
        else:
            failed += 1

    print("-" * 60)
    print(f"  结果: {passed} 通过, {failed} 失败")
    if failed == 0:
        print("  环境就绪，可以运行 device-ops 脚本。")
    else:
        print("  存在缺失项，部分脚本可能无法运行。")
    print()

    return failed


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="device-ops 环境自检 — 检查 TAC 板、设备、工具链",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--meta", help="Meta build 路径 (可选, 加上则检查路径可达性)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    failed = run_probe(meta=args.meta)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
