#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
device_info.py — 设备状态检测、信息读取、日志抓取、dump 收集

依赖: adb, fastboot (需在 PATH 或通过 --adb/--fastboot 指定)
无需 TAC 板 — 此工具只通过 USB 数据通道与已开机的设备交互。

adb 当前桥接到哪个 VM（PVM/GVM）、那个 VM 装什么 OS（QNX/Yocto Linux/AOSP
Android），是两个独立、且都不固定的维度——取决于当次连接。本工具对 ver_info
两条已知路径 + AOSP getprop + Yocto /etc/os-release 均做探测式尝试，不预设
"因为是 GVM 所以是 X"。见 reference/post-flash-operations.md §2-3。

注意: Meta_Build_ID 验证的是"整机 meta"（固件层写入），验证 apps 单独刷写要
看 os_release_version_id/build_fingerprint 里哪个非空，不要看 Meta_Build_ID。

CLI 用法:
  device_info.py status                   # 检测设备当前模式
  device_info.py info                     # 读取固件版本等信息
  device_info.py log                      # 抓取日志 (自动检测 OS 类型)
  device_info.py log --type logcat        # 指定日志类型
  device_info.py dump                     # 收集 crash dump
  device_info.py dump --output ./dumps/   # 指定输出目录
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from shutil import which
from typing import Optional

log = logging.getLogger(__name__)


def _run(cmd, timeout=30):
    """执行命令并返回 (stdout, returncode)。"""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout, check=False)
        return proc.stdout.strip(), proc.returncode
    except FileNotFoundError:
        return f"找不到: {cmd[0]}", -1
    except subprocess.TimeoutExpired:
        return f"超时({timeout}s)", -2


def find_adb(override: Optional[str] = None) -> str:
    if override and os.path.exists(override):
        return override
    p = which("adb") or which("adb.exe")
    if p:
        return p
    raise RuntimeError("adb 未找到。请安装 Android Platform Tools 或用 --adb 指定路径。")


def find_fastboot(override: Optional[str] = None) -> str:
    if override and os.path.exists(override):
        return override
    p = which("fastboot") or which("fastboot.exe")
    if p:
        return p
    raise RuntimeError("fastboot 未找到。请安装 Android Platform Tools 或用 --fastboot 指定路径。")


# ============================================================================
# status: 检测设备当前模式
# ============================================================================

def detect_status(adb: str, fastboot: str) -> str:
    """
    返回设备当前模式:
      'adb'      — 正常开机,adb 可连
      'fastboot' — 在 fastboot/bootloader
      'recovery' — 在 recovery
      'edl'      — 在 EDL (QDLoader 9008 端口存在)
      'offline'  — USB 已连但 adb 显示 offline
      'absent'   — 完全未检测到
    """
    # adb devices
    out, rc = _run([adb, "devices"])
    if rc == 0 and out:
        for line in out.splitlines()[1:]:
            if "\tdevice" in line:
                return "adb"
            if "\trecovery" in line:
                return "recovery"
            if "\toffline" in line:
                return "offline"

    # fastboot devices
    out, rc = _run([fastboot, "devices"])
    if rc == 0 and out.strip():
        return "fastboot"

    # EDL: 检查 9008 端口
    try:
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "") + " " + (p.hwid or "")
            if "9008" in desc or "QDLoader" in desc:
                return "edl"
    except ImportError:
        pass

    return "absent"


# ============================================================================
# info: 读取设备信息
# ============================================================================

def get_device_info(adb: str) -> dict:
    """
    Read basic device info. Device must be in adb mode.

    陷阱0（最重要）: "PVM/GVM" 和 "OS 类型" 是两个独立维度，不能互相推断。
    PVM/GVM 只说明 adb 当前桥接到哪个虚拟机（取决于那台机器此刻的 adbd 桥接配置，
    同一物理设备下次连可能桥接到另一个 VM）；那个 VM 装的是 QNX / Yocto Linux /
    AOSP Android 完全独立，且不同项目、不同 meta 都可能不同。绝不要写"因为是 GVM
    所以用 os-release"或"因为是 GVM 所以是 Android"这类固定规则——本函数对两条
    ver_info 路径、AOSP getprop、Yocto os-release 三者都做探测式尝试（try-and-see），
    用哪个看当前这次连接实际非空，不预设。

    陷阱1: Meta_Build_ID 反映的是"上次整机刷了什么 meta"（固件层写入），刷 apps 单
    分区不会更新它。验证 apps-only 刷写要看下面的 os_release_version_id/
    build_fingerprint（哪个非空用哪个），不要看 ver_info。

    ver_info.txt 路径: PVM(QNX/LV/AGL) 通常在 /firmware/verinfo/ver_info.txt；
    某些 Linux VM 挂载在 /vendor/firmware_mnt/verinfo/ver_info.txt。两条都试。
    格式是 JSON: {"Metabuild_Info": {"Meta_Build_ID": ..., "Product_Flavor": ...}}
    """
    info = {}

    # ver_info.txt — try both known mount points (JSON format)
    ver_info_paths = ["/firmware/verinfo/ver_info.txt", "/vendor/firmware_mnt/verinfo/ver_info.txt"]
    for vpath in ver_info_paths:
        out, rc = _run([adb, "shell", "cat", vpath])
        if rc == 0 and out:
            info["ver_info_raw"] = out
            info["ver_info_path"] = vpath
            try:
                meta_info = json.loads(out).get("Metabuild_Info", {})
                info["Meta_Build_ID"] = meta_info.get("Meta_Build_ID", "unknown")
                info["Product_Flavor"] = meta_info.get("Product_Flavor", "unknown")
            except json.JSONDecodeError:
                log.debug("ver_info.txt at %s is not JSON, leaving raw only", vpath)
            break

    # AOSP/Android properties — only present if the currently-bridged VM's
    # userspace is Android. Empty result means it isn't (e.g. a plain Yocto
    # Linux VM) — not a failure, just try os_release below instead.
    out, rc = _run([adb, "shell", "getprop", "ro.build.display.id"])
    if rc == 0 and out:
        info["build_display_id"] = out.strip()

    out, rc = _run([adb, "shell", "getprop", "ro.build.fingerprint"])
    if rc == 0 and out:
        info["build_fingerprint"] = out.strip()

    # Yocto/OpenEmbedded version stamp — present if the currently-bridged VM's
    # userspace is Yocto-based Linux (no AOSP property system). This is what
    # changes on an apps-only flash for that kind of build.
    out, rc = _run([adb, "shell", "cat", "/etc/os-release"])
    if rc == 0 and out:
        for line in out.splitlines():
            if line.startswith("VERSION_ID="):
                info["os_release_version_id"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("PRETTY_NAME="):
                info["os_release_pretty_name"] = line.split("=", 1)[1].strip().strip('"')

    # OS type detection — probes uname + getprop,照 ADBProfiler.py:getOSType 的
    # 'GNU/Linux' in uname 字符串判据加固（区分 glibc Linux vs busybox-style）。
    out, rc = _run([adb, "shell", "uname", "-a"])
    if rc == 0:
        uname_full = out.strip()
        if "qnx" in uname_full.lower():
            info["os_type"] = "qnx"
        elif "GNU/Linux" in uname_full:
            info["os_type"] = "linux_gnu"
        elif "Linux" in uname_full:
            # 区分 Android userspace vs 其它 busybox/musl Linux（如某些 Yocto 配置）
            out2, _ = _run([adb, "shell", "getprop", "ro.build.version.sdk"])
            if out2 and out2.strip().isdigit():
                info["os_type"] = "android"
            else:
                info["os_type"] = "linux"
        else:
            info["os_type"] = uname_full.split()[0].lower() if uname_full else "unknown"
    else:
        info["os_type"] = "unknown"

    return info


# ============================================================================
# log: 抓取日志
# ============================================================================

LOG_TYPES = {
    "logcat": {"cmd": ["logcat", "-d"], "os": ["android"], "desc": "Android logcat"},
    "dmesg": {"cmd": ["dmesg"], "os": ["android", "linux"], "desc": "Kernel ring buffer"},
    "journal": {"cmd": ["journalctl", "--no-pager", "-n", "2000"], "os": ["linux"], "desc": "systemd journal"},
    "slog": {"cmd": ["slog2info"], "os": ["qnx"], "desc": "QNX slog2"},
    "kmsg": {"cmd": ["cat", "/proc/kmsg_dump"], "os": ["android", "linux"], "desc": "Last kmsg dump"},
}


def grab_log(adb: str, log_type: Optional[str] = None, output_dir: str = ".") -> list:
    """
    抓取设备日志，保存到 output_dir。
    log_type=None 则根据 OS 类型自动选择合适的日志。
    返回保存的文件路径列表。
    """
    os.makedirs(output_dir, exist_ok=True)
    saved = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 探测 OS
    info = get_device_info(adb)
    os_type = info.get("os_type", "unknown")
    log.info("设备 OS: %s", os_type)

    if log_type:
        types_to_grab = [log_type]
    else:
        # 自动选择
        types_to_grab = [k for k, v in LOG_TYPES.items() if os_type in v["os"]]
        if not types_to_grab:
            types_to_grab = ["dmesg"]  # fallback

    for lt in types_to_grab:
        spec = LOG_TYPES.get(lt)
        if not spec:
            log.warning("未知日志类型: %s", lt)
            continue
        cmd = [adb, "shell"] + spec["cmd"]
        log.info("抓取 %s: %s", lt, " ".join(cmd))
        out, rc = _run(cmd, timeout=60)
        if rc == 0 and out:
            fname = f"{lt}_{timestamp}.log"
            fpath = os.path.join(output_dir, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(out)
            saved.append(fpath)
            log.info("✓ 保存: %s (%d bytes)", fpath, len(out))
        else:
            log.warning("  %s 抓取失败 (rc=%s)", lt, rc)

    return saved


# ============================================================================
# dump: 收集 crash dump
# ============================================================================

DUMP_PATHS = [
    "/data/tombstones/",
    "/data/vendor/ramdump/",
    "/data/vendor/ssrdump/",
    "/var/log/dumps/",        # QNX
    "/data/core/",
]


def grab_dump(adb: str, output_dir: str = "./dumps") -> list:
    """
    从设备 pull crash dump 文件。
    遍历已知 dump 路径，有内容就 pull 下来。
    """
    os.makedirs(output_dir, exist_ok=True)
    pulled = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for dpath in DUMP_PATHS:
        # 检查路径是否存在且非空
        out, rc = _run([adb, "shell", "ls", dpath], timeout=10)
        if rc != 0 or not out.strip():
            continue
        # pull 整个目录
        local_dest = os.path.join(output_dir, f"{dpath.strip('/').replace('/', '_')}_{timestamp}")
        os.makedirs(local_dest, exist_ok=True)
        log.info("拉取 %s -> %s", dpath, local_dest)
        proc = subprocess.run([adb, "pull", dpath, local_dest],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=300, check=False)
        if proc.returncode == 0:
            pulled.append(local_dest)
            log.info("✓ %s 拉取完成", dpath)
        else:
            log.warning("  %s pull 失败: %s", dpath, proc.stdout[:200] if proc.stdout else "")

    if not pulled:
        log.info("未发现 dump 文件（设备可能没有 crash 记录）")
    return pulled


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="设备信息、日志抓取、dump 收集")
    parser.add_argument("action", choices=["status", "info", "log", "dump"],
                        help="status=检测模式, info=读取信息, log=抓日志, dump=收dump")
    parser.add_argument("--adb", help="adb 路径")
    parser.add_argument("--fastboot", help="fastboot 路径")
    parser.add_argument("--type", dest="log_type", choices=list(LOG_TYPES.keys()),
                        help="日志类型（log 命令用）")
    parser.add_argument("--output", "-o", default=".", help="输出目录")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    adb = find_adb(args.adb)
    fastboot_path = None
    if args.action == "status":
        fastboot_path = find_fastboot(args.fastboot)

    if args.action == "status":
        mode = detect_status(adb, fastboot_path)
        print(f"设备状态: {mode}")
        sys.exit(0 if mode != "absent" else 1)

    elif args.action == "info":
        info = get_device_info(adb)
        if not info:
            print("无法读取设备信息（设备可能不在 adb 模式）")
            sys.exit(1)
        for k, v in info.items():
            if k != "ver_info_raw":
                print(f"  {k}: {v}")

    elif args.action == "log":
        saved = grab_log(adb, log_type=args.log_type, output_dir=args.output)
        if saved:
            print(f"✓ 保存了 {len(saved)} 个日志文件到 {args.output}")
        else:
            print("未能抓取任何日志")
            sys.exit(1)

    elif args.action == "dump":
        pulled = grab_dump(adb, output_dir=args.output)
        if pulled:
            print(f"✓ 拉取了 {len(pulled)} 个 dump 目录到 {args.output}")
        else:
            print("未发现 dump（设备可能没有 crash 记录）")


if __name__ == "__main__":
    main()
