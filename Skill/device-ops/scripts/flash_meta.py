#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
flash_meta.py — Meta flash orchestrator (replaces xPCAT GUI 10-step flow)

No profile files needed. Parameters are auto-derived from the meta path and
a small built-in per-chip defaults table (see DEVICE_DEFAULTS below, kept in
sync with reference/flash-meta.md). Explicit CLI flags always override the
derived defaults.

Usage:
  # Build ID only — default server \\grilled\nsid-sha-spsp-02 is prepended
  python flash_meta.py --meta "SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1"

  # Full UNC path — used as-is
  python flash_meta.py --meta "\\server\share\SA8797P..." --dry-run
  python flash_meta.py --meta "\\..." --start-step 8
  python flash_meta.py --meta "\\..." --skip-provision

  # Unknown chip / override auto-detected defaults
  python flash_meta.py --meta "\\..." --pf 8775_la --pf2 sail_nor_lemans \
      --storage ufs --storage2 spinor --cdt-rel common/cdt/LEMANSSTAR/cdt.bin
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from tac import TacController

# ============================================================================
# 日志
# ============================================================================

log = logging.getLogger("flash")


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


# ============================================================================
# Default build server + per-chip defaults
#
# 陷阱: 这张表是"没给完整路径/没给 pf 时"的兜底猜测，不是权威数据源。
# 权威数据源永远是 meta_cli（真实解析 contents.xml）；这张表只在自动探测失败
# 或用户只给了 build ID 时提供合理默认值。保持与 reference/flash-meta.md 同步。
# ============================================================================

DEFAULT_SERVER = r"\\grilled\nsid-sha-spsp-02"

DEVICE_DEFAULTS = {
    # chip name (as it appears at the start of the meta build ID) -> defaults
    "SA8797P": {
        "board": "RIDESX",
        "pf": "nonsafe_ivi_pvm_lagvm", "storage": "ufs", "provision": True, "erase_luns": True,
        "pf2": "safe_rtos", "storage2": "spinor", "provision2": False, "erase_luns2": False,
        "cdt_rel": "common/cdt/RIDESX/cdt.bin",
        "sahara_mode": "nordy_multi",
        "provision_xml_rel": "common/config/ufs/provision/provision_ufs40_siod.xml",
        "verify_file": "/firmware/verinfo/ver_info.txt", "verify_key": "Meta_Build_ID",
    },
    "SA8775": {
        "board": "LEMANS",
        "pf": "8775_la", "storage": "ufs", "provision": True, "erase_luns": True,
        "pf2": "sail_nor_lemans", "storage2": "spinor", "provision2": False, "erase_luns2": False,
        "cdt_rel": "common/cdt/LEMANSSTAR/cdt.bin",
        "sahara_mode": "standard",
        "provision_xml_rel": "common/config/ufs/provision/provision_default.xml",
        "verify_file": "/firmware/verinfo/ver_info.txt", "verify_key": "Meta_Build_ID",
    },
}


def resolve_meta_path(meta_arg: str) -> str:
    """
    If meta_arg looks like a bare build ID (no leading \\\\ or /), prepend
    DEFAULT_SERVER. Full UNC/absolute paths pass through unchanged.
    """
    if meta_arg.startswith("\\\\") or meta_arg.startswith("/") or os.path.isabs(meta_arg):
        return meta_arg
    return os.path.join(DEFAULT_SERVER, meta_arg)


def detect_chip(meta_path: str) -> Optional[str]:
    """Extract chip name (e.g. 'SA8797P') from the meta build ID (leaf dir name)."""
    leaf = os.path.basename(meta_path.rstrip("/\\"))
    m = re.match(r"([A-Z0-9]+)", leaf)
    if not m:
        return None
    candidate = m.group(1)
    for known in DEVICE_DEFAULTS:
        if candidate.startswith(known) or known.startswith(candidate):
            return known
    return candidate


# ============================================================================
# 配置 — 来自 CLI + 自动探测，不再依赖 profile 文件
# ============================================================================

@dataclass
class FlashDomain:
    """One flash domain (e.g. main/UFS, sail/SPINOR)."""
    name: str
    pf: str            # product flavor
    storage: str       # ufs / spinor / emmc
    provision: bool    # 是否做 provisioning
    erase_luns: bool   # 是否全盘 LUN 擦除

@dataclass
class Config:
    # ---- 来自 CLI ----
    meta: str = ""
    dry_run: bool = False
    start_step: int = 1
    skip_provision: bool = False

    # ---- 自动探测 / CLI 覆盖 ----
    chip: str = ""
    board: str = ""
    sahara_mode: str = "standard"              # "nordy_multi" | "standard"
    domains: List[FlashDomain] = field(default_factory=list)
    cdt_rel: str = ""
    provision_xml_rel: str = ""
    meta_cli_rel: str = ""                     # 按 os.name 选 win/linux
    verify_file: str = ""
    verify_key: str = ""

    # ---- TAC ----
    tac_dialect: Optional[str] = None
    tac_port: Optional[str] = None

    # ---- 工具路径覆盖 ----
    fh_loader: Optional[str] = None
    qsahara: Optional[str] = None
    fastboot: Optional[str] = None
    adb: Optional[str] = None

    # ---- 超时 (秒) ----
    edl_port_timeout: int = 60
    # 陷阱: 大 build 刷完 UFS+SPINOR 后设备重新枚举到 fastboot 常常超过 60s
    # （真机实测过 60s 不够、需人工 power-cycle+resume 才能续上），120s 更贴近实际。
    fastboot_timeout: int = 120
    adb_timeout: int = 180


def derive_config(args: argparse.Namespace) -> Config:
    """
    Build Config from CLI args, auto-detecting chip defaults from the meta
    path where the user didn't explicitly override.

    陷阱: 这里的自动探测只是"合理默认值"，遇到没见过的芯片/新板型时，用
    --pf/--pf2/--storage/--storage2/--cdt-rel 等显式覆盖，不要指望自动猜对。
    """
    meta_path = resolve_meta_path(args.meta)
    chip = detect_chip(meta_path) or ""
    defaults = DEVICE_DEFAULTS.get(chip, {})

    if not defaults and not (args.pf and args.storage):
        log.warning(
            "Unknown chip '%s' (from meta path) and no --pf/--storage given. "
            "Known chips: %s. See reference/flash-meta.md to add this chip, "
            "or pass --pf/--storage/--cdt-rel explicitly.",
            chip or "?", list(DEVICE_DEFAULTS.keys()),
        )

    pf = args.pf or defaults.get("pf", "")
    storage = args.storage or defaults.get("storage", "ufs")
    # 注: --skip-provision 的实际生效点在 flash_all()（"if domain.provision and not
    # cfg.skip_provision"），这里只记录该 domain 本身是否需要 provision 这一能力。
    provision = defaults.get("provision", storage == "ufs")
    erase_luns = defaults.get("erase_luns", storage == "ufs")

    domains = []
    if pf:
        domains.append(FlashDomain(
            name="main", pf=pf, storage=storage,
            provision=provision, erase_luns=erase_luns,
        ))

    pf2 = args.pf2 or defaults.get("pf2")
    if pf2:
        storage2 = args.storage2 or defaults.get("storage2", "spinor")
        domains.append(FlashDomain(
            name="sail", pf=pf2, storage=storage2,
            provision=defaults.get("provision2", False),
            erase_luns=defaults.get("erase_luns2", storage2 == "ufs"),
        ))

    # 跨平台：选 meta_cli 路径
    meta_cli_rel = (
        "common/build/app/windows_x86_64/meta_cli.exe" if os.name == "nt"
        else "common/build/app/linux/meta_cli"
    ).replace("/", os.sep)

    cfg = Config(
        meta=meta_path,
        dry_run=args.dry_run,
        start_step=args.start_step,
        skip_provision=args.skip_provision,
        chip=chip,
        board=defaults.get("board", ""),
        sahara_mode=args.sahara_mode or defaults.get("sahara_mode", "standard"),
        domains=domains,
        cdt_rel=(args.cdt_rel or defaults.get("cdt_rel", "")).replace("/", os.sep),
        provision_xml_rel=(args.provision_xml_rel or defaults.get("provision_xml_rel", "")).replace("/", os.sep),
        meta_cli_rel=meta_cli_rel,
        verify_file=defaults.get("verify_file", "/firmware/verinfo/ver_info.txt"),
        verify_key=defaults.get("verify_key", "Meta_Build_ID"),
        tac_dialect=args.tac_dialect,
        tac_port=args.tac_port,
    )
    if args.fastboot_timeout:
        cfg.fastboot_timeout = args.fastboot_timeout
    return cfg


# ============================================================================
# 错误处理 & 命令执行
# ============================================================================

class FlashError(Exception):
    """刷机流程失败，携带清晰原因，顶层统一处理。"""


def _dump_trace(cwd: Optional[str], output: str = "", tail: int = 60):
    """
    Dump port_trace.txt tail on failure — fh_loader writes detailed root cause there.

    陷阱: fh_loader 退出码 0 不代表成功，必须看 stdout 判据 + port_trace 真因。
    """
    candidates = []
    # 从 fh_loader 输出里抓它报告的日志路径（最可靠）
    for m in re.finditer(r"(?:Writing log to|Log is)\s+'([^']+)'", output):
        candidates.append(m.group(1))
    if cwd:
        candidates.append(os.path.join(cwd, "port_trace.txt"))
    candidates.append(os.path.join(os.getcwd(), "port_trace.txt"))
    candidates.append(os.path.join(os.path.expanduser("~"), "Downloads", "port_trace.txt"))

    seen = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                log.error("---- port_trace.txt (%s) ----", p)
                for line in lines[-tail:]:
                    log.error("   trace| %s", line.rstrip())
                log.error("---- trace end ----")
                return
        except Exception as e:
            log.debug("读取 trace 失败 %s: %s", p, e)
    log.error("未找到 port_trace.txt（已查: %s）", candidates)


def run(cmd: List[str], cfg: Config, capture=True, timeout=None,
        check_success: Optional[str] = None, cwd: Optional[str] = None):
    """
    Execute a command, return (stdout, returncode).

    check_success: if set, both rc==0 AND this substring must appear in stdout.
    陷阱: fh_loader/QSahara 的 rc=0 不等于成功，必须靠字符串判据。
    """
    printable = " ".join(cmd)
    log.info(">>> %s", printable)
    if cfg.dry_run:
        return ("", 0)

    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout, check=False, cwd=cwd,
        )
    except FileNotFoundError as e:
        raise FlashError(f"找不到可执行文件: {e.filename} (cmd: {printable})")
    except subprocess.TimeoutExpired:
        raise FlashError(f"命令超时({timeout}s): {printable}")

    out = proc.stdout or ""
    if out.strip():
        for line in out.splitlines():
            log.info("    | %s", line.rstrip())

    if check_success is not None:
        ok = (proc.returncode == 0) and (check_success in out)
        if not ok:
            _dump_trace(cwd, output=out)
            raise FlashError(
                f"未见成功判据 '{check_success}' (rc={proc.returncode}): {printable}"
            )
    return (out, proc.returncode)


# ============================================================================
# 工具发现: fh_loader / QSaharaServer / fastboot / adb
# ============================================================================

# 常见 QC 工具安装目录 (Windows)
_QC_DIRS_WIN = [
    r"C:\Program Files (x86)\Qualcomm\QFIL",
    r"C:\Program Files (x86)\Qualcomm\QPST\bin",
    r"C:\Qualcomm\QFIL",
]


def _which(name: str) -> Optional[str]:
    from shutil import which
    return which(name)


def find_tool(cfg: Config, override: Optional[str], names: List[str],
              extra_dirs: List[str]) -> str:
    """Locate a tool binary: explicit override -> PATH -> known dirs -> meta boot dirs."""
    if override and os.path.exists(override):
        return override
    for n in names:
        p = _which(n)
        if p:
            return p
    search_dirs = (_QC_DIRS_WIN if os.name == "nt" else []) + extra_dirs
    for d in search_dirs:
        for n in names:
            cand = os.path.join(d, n)
            if os.path.exists(cand):
                return cand
    if cfg.dry_run:
        return names[0]
    raise FlashError(
        f"未找到 {names}。请用 QPM 安装: qpm-cli --license-activate qfil && "
        f"qpm-cli --install qfil --silent"
    )


def meta_cli_path(cfg: Config) -> str:
    """Resolve the meta_cli executable path within the meta build."""
    p = os.path.join(cfg.meta, cfg.meta_cli_rel)
    if not os.path.exists(p) and not cfg.dry_run:
        raise FlashError(f"meta 内找不到 meta_cli: {p}")
    return p


def discover_boot_dirs(cfg: Config) -> List[str]:
    """Ask meta_cli for boot_images paths as candidate tool directories."""
    dirs = []
    try:
        out, _ = run([meta_cli_path(cfg), "get_build_path", "tag='boot'"], cfg)
        base = out.strip().strip('[]"').replace("\\\\", "\\").strip().strip('"')
        # 常见的子目录结构
        suffixes = [
            os.path.join("boot_images", "boot", "QcomPkg", "Tools", "storage", "fh_loader"),
            os.path.join("boot_images", "QcomPkg", "Tools", "storage", "fh_loader"),
            os.path.join("boot_images", "core", "storage", "tools"),
            os.path.join("boot_images", "boot_tools", "QSaharaServer"),
        ]
        for sub in suffixes:
            if base:
                dirs.append(os.path.join(base, sub))
    except Exception as e:
        log.debug("get_build_path 失败(可忽略): %s", e)
    return dirs


# ============================================================================
# EDL 端口探测
# ============================================================================

# Windows: "QDLoader 9008"; Linux: idVendor=05c6 idProduct=9008
EDL_PORT_DESC = "QDLoader 9008"
EDL_PORT_PID = 0x9008


def _is_serial_available() -> bool:
    try:
        import serial.tools.list_ports  # noqa: F401
        return True
    except ImportError:
        return False


def find_edl_port(cfg: Config) -> Optional[str]:
    """Return the EDL QDLoader 9008 port if present, else None."""
    if cfg.dry_run:
        return "COM99" if os.name == "nt" else "/dev/ttyUSB99"
    if not _is_serial_available():
        raise FlashError("未安装 pyserial，无法探测 EDL 端口: pip install pyserial")
    import serial.tools.list_ports
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "") + " " + (p.hwid or "")
        if EDL_PORT_DESC in desc or "9008" in desc:
            return p.device
        # Linux fallback: 靠 PID
        if p.pid == EDL_PORT_PID:
            return p.device
    return None


def wait_for_edl(cfg: Config) -> str:
    """Block until EDL port appears; raise on timeout."""
    log.info("等待设备进入 EDL (%s) ...", EDL_PORT_DESC)
    deadline = time.time() + cfg.edl_port_timeout
    while time.time() < deadline:
        com = find_edl_port(cfg)
        if com:
            log.info("[ok] EDL port: %s", com)
            return com
        time.sleep(2)
    raise FlashError(f"{cfg.edl_port_timeout}s 内未出现 EDL 端口")


def wait_for_fastboot(cfg: Config, fastboot_bin: str) -> None:
    """Block until fastboot device appears."""
    log.info("等待 fastboot 设备 ...")
    deadline = time.time() + cfg.fastboot_timeout
    while time.time() < deadline:
        if cfg.dry_run:
            return
        out, _ = run([fastboot_bin, "devices"], cfg)
        if out.strip():
            log.info("[ok] fastboot device ready")
            return
        time.sleep(2)
    raise FlashError(f"{cfg.fastboot_timeout}s 内未发现 fastboot 设备")


# ============================================================================
# 跨平台端口格式
# ============================================================================

def device_port(com: str) -> str:
    r"""
    Format port for fh_loader/QSaharaServer consumption.

    陷阱: Windows 高编号 COM 口 (>=10) 必须用 \\.\COMxx 才能被 CreateFile 打开；
    Linux 直接用 /dev/ttyUSBx。
    """
    if os.name == "nt":
        return f"\\\\.\\{com}"
    return com


# ============================================================================
# meta_cli 封装
# ============================================================================

def get_programmer_dir(cfg: Config, pf: str) -> str:
    """Resolve device programmer directory via meta_cli get_device_programmer."""
    if cfg.dry_run:
        return os.path.join(cfg.meta, "boot_images", "programmer")
    out, _ = run([meta_cli_path(cfg), "get_device_programmer", f"flavor={pf}"], cfg)
    path = None
    try:
        arr = json.loads(out)
        if isinstance(arr, list) and arr:
            path = arr[0]
    except json.JSONDecodeError:
        m = re.search(r'"([^"]+\.elf)"', out)
        if m:
            path = m.group(1).replace("\\\\", "\\")
    if not path:
        raise FlashError(f"无法解析 device programmer (pf={pf})")
    path = os.path.normpath(path)
    d = os.path.dirname(path)
    if not os.path.exists(d):
        raise FlashError(f"programmer dir 不存在: {d}")
    log.info("[ok] programmer dir: %s", d)
    return d


def get_qsahara_files(cfg: Config) -> Dict[str, List[str]]:
    """
    meta_cli get_qsahara_files -> {image_id: [file, ...], ...}.
    Non-empty = Nordy platform (multi-image Sahara); empty = standard single-image.

    判据: cfg.sahara_mode=='nordy_multi' 时若这里返回空，说明 meta 可能太旧或该
    芯片实际不是 Nordy（chip defaults 表猜错了）——下游 load_sahara 会照单镜像处理。
    """
    if cfg.dry_run:
        return {}
    try:
        out, _ = run([meta_cli_path(cfg), "get_qsahara_files"], cfg, timeout=30)
        data = json.loads(out)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, FlashError) as e:
        log.debug("get_qsahara_files 无输出: %s", e)
    return {}


def detect_programmer_elf(cfg: Config, programmer_dir: str) -> str:
    """Pick the right programmer ELF based on sahara_mode."""
    is_nordy = cfg.sahara_mode == "nordy_multi"
    preferred = "device_programmer_ddr.elf" if is_nordy else "prog_firehose_ddr.elf"
    order = (preferred, "prog_firehose_ddr.elf", "device_programmer_ddr.elf")
    for name in order:
        if cfg.dry_run or os.path.exists(os.path.join(programmer_dir, name)):
            return name
    raise FlashError(f"programmer 目录里找不到 firehose elf: {programmer_dir}")


def make_partition_json(cfg: Config, pf: str, storage: str, temp_dir: str) -> str:
    """
    Generate partition_files.json via meta_cli (contains rawprogram + patch + bin lists).

    判据: RAW/BIN 列表非空才算成功（对应 GUI 步骤 6）。
    附加检查: 主动校验引用文件的可达性——把 fh_loader opaque 的 rc=1 提前变成精确定位。
    """
    mcli = meta_cli_path(cfg)
    cmd = [mcli, "get_partition_files", f"flavor='{pf}'", f"storage='{storage}'", "group=True"]
    out, rc = run(cmd, cfg)
    if cfg.dry_run:
        return os.path.join(temp_dir, "partition_files.json")

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise FlashError(
            f"meta_cli get_partition_files 输出非 JSON (pf={pf}, storage={storage})"
        )

    raws = data.get("partition") or []
    bins = data.get("partition_bin") or []
    if not raws or not bins:
        raise FlashError(
            f"partition_files 为空 (pf={pf}, storage={storage}): RAW/BIN 缺失"
        )
    log.info("[ok] partition_files: %d rawprogram, %d bin", len(raws), len(bins))

    # ---- 文件可达性检查 ----
    # 陷阱: UNC 路径因 VPN/掉线不可达时 fh_loader 只报一个模糊的 rc=1，这里提前暴露。
    missing = []
    all_files = list(raws) + list(data.get("partition_patch") or []) + list(bins)
    for f in all_files:
        try:
            if not os.path.exists(f):
                missing.append(f)
        except Exception as e:
            missing.append(f"{f} ({e})")
    if missing:
        log.warning("partition_files 引用了 %d 个不可达文件 (前 15 个):", len(missing))
        for f in missing[:15]:
            log.warning("     miss: %s", f)
        log.warning("   若为网络共享路径，可能是权限/掉线问题。fh_loader 会因此失败。")

    out_path = os.path.join(temp_dir, "partition_files.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return out_path


# ============================================================================
# Firehose (fh_loader / QSaharaServer) 操作
# ============================================================================

def load_sahara(cfg: Config, qsahara_bin: str, com: str, programmer_dir: str):
    """
    Download device programmer via Sahara protocol.

    Nordy 平台的设备索要多个 image-id (13/59/...)，必须逐一 -s <id>:<basename> 并追加
    -b <dir>。照 flasher.py getQSaharaServerCommandForNordy 复刻。
    """
    qsf = get_qsahara_files(cfg)
    is_nordy = cfg.sahara_mode == "nordy_multi" and bool(qsf)
    elf = detect_programmer_elf(cfg, programmer_dir)

    cmd = [qsahara_bin, "-s", f"13:{elf}"]
    base_dirs = [os.path.join(programmer_dir, "")]

    if is_nordy:
        log.info("Nordy 多镜像 Sahara: images=%s", [k for k in qsf if k != "13"])
        extra_dirs = set()
        for image_id, files in qsf.items():
            if image_id == "13":
                continue
            for f in files:
                cmd += ["-s", f"{image_id}:{os.path.basename(f)}"]
                extra_dirs.add(os.path.dirname(f))
        for d in extra_dirs:
            base_dirs.append(os.path.join(d, ""))

    for d in base_dirs:
        cmd += ["-b", d]
    cmd += ["-p", device_port(com)]

    label = "(Nordy multi-image)" if is_nordy else "(standard)"
    log.info("下发 programmer %s %s ...", elf, label)
    run(cmd, cfg, check_success="Sahara protocol completed", timeout=300)
    log.info("[ok] Sahara done")


def _fh_base(fh_bin: str) -> List[str]:
    return [fh_bin, "--setactivepartition=1", "--noprompt", "--showpercentagecomplete"]


def _fh_port(com: str) -> List[str]:
    return [f"--port={device_port(com)}", "--zlpawarehost=1"]


def fh_erase(cfg: Config, fh_bin: str, com: str, storage: str,
             cwd: Optional[str] = None):
    """
    Erase LUNs. Only UFS needs multi-LUN erase (0-7 skip 3); SPINOR/EMMC skip.

    陷阱: SPINOR 没有多 LUN 概念，rawprogram xml 内部自带分区级擦除，额外 erase 反而报错。
    """
    if storage != "ufs":
        log.info("跳过全盘擦除 (%s 无多 LUN，由 rawprogram xml 处理)", storage)
        return
    cmd = [fh_bin, f"--memoryname={storage}"]
    cmd += [f"--erase={i}" for i in (0, 1, 2, 4, 5, 6, 7)]
    cmd += ["--loglevel=1", f"--port={device_port(com)}"]
    log.info("擦除 %s LUNs ...", storage)
    run(cmd, cfg, check_success="{All Finished Successfully}", timeout=600, cwd=cwd)
    log.info("[ok] erase done (%s)", storage)


def fh_provision(cfg: Config, fh_bin: str, com: str, cwd: Optional[str] = None):
    """
    UFS Provisioning (corresponds to GUI step 5).

    判据: stdout 含 '{All Finished Successfully}'。
    """
    prov_xml = os.path.join(cfg.meta, cfg.provision_xml_rel)
    if not cfg.dry_run and not os.path.exists(prov_xml):
        raise FlashError(f"找不到 provision xml: {prov_xml}")
    search = os.path.dirname(prov_xml)
    xml_name = os.path.basename(prov_xml)
    cmd = _fh_base(fh_bin) + [
        "--memoryname=ufs",
        f"--sendxml={xml_name}",
        "--loglevel=1",
        f"--search_path={search}",
    ] + _fh_port(com)
    log.info("UFS Provisioning ...")
    run(cmd, cfg, check_success="{All Finished Successfully}", timeout=600, cwd=cwd)
    log.info("[ok] UFS Provisioning done")


def fh_load(cfg: Config, fh_bin: str, com: str, pf: str, storage: str,
            json_path: str, temp_dir: str, search_path: Optional[str] = None):
    """
    Write images via fh_loader (corresponds to GUI step 7/8).

    判据: '{All Finished Successfully}' — 其他一切 rc=0 均不可信。
    """
    sp = search_path or temp_dir
    cmd = _fh_base(fh_bin) + [
        f"--memoryname={storage}",
        f"--search_path={sp}",
        f"--json_in={json_path}",
        f"--flavor={pf}",
        "--loglevel=1",
    ] + _fh_port(com)
    log.info("写入镜像 pf=%s storage=%s ...", pf, storage)
    run(cmd, cfg, check_success="{All Finished Successfully}", timeout=1800, cwd=temp_dir)
    log.info("[ok] load done (%s/%s)", pf, storage)


# ============================================================================
# EDL 会话：进 EDL -> 探口 -> 下 sahara -> 回调操作 -> 自动重试
# ============================================================================

def edl_session(cfg: Config, tac: TacController, qsahara_bin: str,
                programmer_dir: str, do_writes: Callable[[str], None]):
    """
    Enter EDL, load programmer, execute do_writes(com_port).

    失败恢复: COM/端口类错误 -> power cycle -> 重试一次。
    陷阱: 有时 Windows 枚举到的 COM 口被 QUTS/xPCAT 独占，必须 kill 后重进。
    """
    last_err = None
    for attempt in (1, 2):
        try:
            tac.boot_ss_edl()
            com = wait_for_edl(cfg)
            load_sahara(cfg, qsahara_bin, com, programmer_dir)
            do_writes(com)
            return
        except FlashError as e:
            last_err = e
            msg = str(e)
            log.warning("EDL 会话第 %d 次失败: %s", attempt, msg)
            if attempt == 1 and any(k in msg.lower() for k in ("com", "9008", "sahara", "port")):
                log.warning("疑似 COM/端口问题 -> power cycle 后重试")
                try:
                    tac.power_cycle()
                except Exception as pe:
                    log.warning("power_cycle 异常(继续): %s", pe)
                time.sleep(3)
                continue
            break
    raise FlashError(f"EDL 会话最终失败: {last_err}")


# ============================================================================
# 预检 & 辅助
# ============================================================================

def kill_port_blockers(cfg: Config):
    """Release COM port: stop QUTS, kill tac.exe, PCATApp.exe (Windows only)."""
    if cfg.dry_run or os.name != "nt":
        log.info("[skip] kill port blockers (dry-run or non-Windows)")
        return
    for cmd in (
        ["net", "stop", "QUTS"],
        ["taskkill", "/F", "/IM", "QUTS.exe"],
        ["taskkill", "/F", "/IM", "tac.exe"],
        ["taskkill", "/F", "/IM", "PCATApp.exe"],
    ):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except Exception:
            pass
    log.info("已尝试释放占用 COM 口的进程")


def preflight(cfg: Config, tools: Dict[str, str]):
    """Pre-flight checks: meta reachable, CDT exists, tools found."""
    log.info("== preflight ==")
    if not cfg.dry_run and not os.path.exists(cfg.meta):
        raise FlashError(f"meta 路径不可达: {cfg.meta}")
    cdt = os.path.join(cfg.meta, cfg.cdt_rel)
    if not cfg.dry_run and cfg.cdt_rel and not os.path.exists(cdt):
        raise FlashError(f"找不到 CDT: {cdt}")
    log.info("meta       = %s", cfg.meta)
    log.info("chip/board = %s / %s", cfg.chip, cfg.board)
    for name, path in tools.items():
        log.info("%-10s = %s", name, path)
    log.info("[ok] preflight passed")


def verify_build(cfg: Config, adb_bin: str):
    """
    Final verification: adb read ver_info, check expected key substring.

    判据: verify_key 的值必须包含 meta build 路径中可推断的 build ID 子串。
    这里从 meta 路径最后一段（通常是 SA8797P.HGY.x.x.x.x.c1-xxxxx-STD...）抽取。
    """
    if not cfg.verify_file or not cfg.verify_key:
        log.info("[skip] 未配置校验 (verify.file/key)")
        return

    log.info("等待 adb 设备 ...")
    if not cfg.dry_run:
        run([adb_bin, "wait-for-device"], cfg, timeout=cfg.adb_timeout)

    out, _ = run([adb_bin, "shell", "cat", cfg.verify_file], cfg, timeout=30)
    if cfg.dry_run:
        log.info("[dry-run] 跳过校验")
        return

    # 从 meta 路径末段推断 expected build ID (取 - 之前的主要标识)
    meta_leaf = os.path.basename(cfg.meta.rstrip("/\\"))
    # 常见格式: SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1 -> 取到第二个 - 之前
    m = re.match(r"([A-Z0-9.]+\.[a-z0-9]+-\d+)", meta_leaf)
    expected = m.group(1) if m else meta_leaf

    # 在输出中搜索 verify_key=<value> 这行
    found = False
    for line in out.splitlines():
        if cfg.verify_key in line and expected in line:
            found = True
            break

    if found:
        log.info("[ok] 刷机校验通过: %s 含 %s", cfg.verify_key, expected)
    else:
        raise FlashError(
            f"校验失败: {cfg.verify_file} 中 {cfg.verify_key} 未含期望值 '{expected}'\n"
            f"实际内容:\n{out}"
        )


# ============================================================================
# 顶层编排
# ============================================================================

def flash_all(cfg: Config):
    """Orchestrate the full flash sequence driven by cfg.domains (derived in derive_config)."""
    # ---- 工具发现 ----
    boot_dirs = discover_boot_dirs(cfg) if not cfg.dry_run else []
    fh_names = (["fh_loader.exe", "fh_loader"] if os.name == "nt"
                else ["fh_loader"])
    qs_names = (["QSaharaServer.exe", "QSaharaServer"] if os.name == "nt"
                else ["QSaharaServer"])
    fb_names = (["fastboot.exe", "fastboot"] if os.name == "nt"
                else ["fastboot"])
    adb_names = (["adb.exe", "adb"] if os.name == "nt"
                 else ["adb"])

    fh = find_tool(cfg, cfg.fh_loader, fh_names, boot_dirs)
    qs = find_tool(cfg, cfg.qsahara, qs_names, boot_dirs)
    fb = find_tool(cfg, cfg.fastboot, fb_names, [])
    ab = find_tool(cfg, cfg.adb, adb_names, [])

    tools = {"fh_loader": fh, "QSahara": qs, "fastboot": fb, "adb": ab}
    preflight(cfg, tools)
    kill_port_blockers(cfg)

    # ---- TAC 控制器 ----
    tac = TacController(port=cfg.tac_port, dialect=cfg.tac_dialect, dry_run=cfg.dry_run)

    temp_dir = os.path.join(
        os.environ.get("TEMP", "/tmp") if os.name == "nt" else "/tmp",
        "flash_meta_tmp",
    )
    os.makedirs(temp_dir, exist_ok=True)

    # ---- 遍历 flash domains ----
    # 计算步骤偏移: domain 1 从 step 1 开始, domain 2 从 step 8 开始 (SPINOR), etc.
    # 简化: domain[0] = steps 1-7, domain[1] = step 8, CDT = step 9, verify = step 10
    domain_step_map = {0: 7, 1: 8}  # domain index -> step 阈值

    for idx, domain in enumerate(cfg.domains):
        step_threshold = domain_step_map.get(idx, 7 + idx)
        if cfg.start_step > step_threshold:
            log.info("跳过 domain '%s' (start-step=%d)", domain.name, cfg.start_step)
            continue

        log.info("========== domain: %s (pf=%s, storage=%s) ==========",
                 domain.name, domain.pf, domain.storage)

        prog_dir = get_programmer_dir(cfg, domain.pf)
        part_json = make_partition_json(cfg, domain.pf, domain.storage, temp_dir)

        # ---- Provision (独立 EDL 会话) ----
        # 陷阱: programmer 在 Sahara 加载时读取存储几何。若 provision 和 load 同会话，
        # programmer 拿到"未 provision=0 扇区"的旧几何，备份 GPT 写入会报
        # "Asked NUM_DISK_SECTOR-5 outside total sectors 0"。必须 provision 后重进 EDL。
        if domain.provision and not cfg.skip_provision:
            log.info("--- Provisioning (独立 EDL 会话) ---")

            def _provision(com, _fh=fh):
                fh_provision(cfg, _fh, com, cwd=temp_dir)

            edl_session(cfg, tac, qs, prog_dir, _provision)
            log.info("Provision done, power cycle 让 programmer 重读几何 ...")
            tac.power_cycle()
            time.sleep(3)

        # ---- Erase + Load (新 EDL 会话) ----
        def _domain_writes(com, _domain=domain, _fh=fh, _part_json=part_json):
            if _domain.erase_luns:
                fh_erase(cfg, _fh, com, _domain.storage, cwd=temp_dir)
            fh_load(cfg, _fh, com, _domain.pf, _domain.storage, _part_json, temp_dir)

        edl_session(cfg, tac, qs, prog_dir, _domain_writes)

    # ---- CDT + Fastboot (step 9) ----
    if cfg.start_step <= 9 and cfg.cdt_rel:
        log.info("========== CDT / fastboot (step 9) ==========")
        # 陷阱: SPINOR 刷完后设备仍被 programmer 占着 EDL，必须 power cycle 退出再进 fastboot
        log.info("Power cycle 退出 EDL -> fastboot ...")
        tac.power_cycle()
        time.sleep(10)
        tac.md_fastboot()
        wait_for_fastboot(cfg, fb)
        cdt_path = os.path.join(cfg.meta, cfg.cdt_rel)
        run([fb, "flash", "cdt", cdt_path], cfg, timeout=120)
        run([fb, "reboot"], cfg, timeout=60)
    else:
        log.info("跳过 CDT/fastboot (start-step=%d)", cfg.start_step)

    # ---- Verify (step 10) ----
    log.info("========== verify (step 10) ==========")
    verify_build(cfg, ab)

    tac.close()
    log.info("===== flash completed successfully =====")


# ============================================================================
# CLI
# ============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Meta flash orchestrator (replaces xPCAT GUI flow). "
                    "Parameters auto-derive from --meta; override with --pf/--storage/etc "
                    "for unknown chips. See reference/flash-meta.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--meta", required=True,
                        help="Meta build ID or full UNC path. Bare build ID "
                             f"gets '{DEFAULT_SERVER}' prepended automatically.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print commands without touching hardware")
    parser.add_argument("--start-step", type=int, default=1,
                        help="Resume from step N: 1=full, 8=SPINOR, 9=CDT, 10=verify only")
    parser.add_argument("--skip-provision", action="store_true",
                        help="Skip UFS provisioning (board already provisioned)")
    parser.add_argument("--tac-port",
                        help="TAC board COM port (default: auto-detect)")
    parser.add_argument("--tac-dialect", choices=["nordy", "lemans_sx"],
                        help="TAC command dialect (default: auto-detect by board VID/PID)")

    # ---- Overrides for chips not in DEVICE_DEFAULTS, or to correct a bad guess ----
    parser.add_argument("--pf", help="Main domain product flavor (overrides auto-detect)")
    parser.add_argument("--storage", help="Main domain storage type, e.g. ufs (default: ufs)")
    parser.add_argument("--pf2", help="Second domain (SAIL) product flavor")
    parser.add_argument("--storage2", help="Second domain storage type, e.g. spinor")
    parser.add_argument("--cdt-rel", help="CDT path relative to meta root")
    parser.add_argument("--provision-xml-rel", help="UFS provision XML path relative to meta root")
    parser.add_argument("--sahara-mode", choices=["nordy_multi", "standard"],
                        help="Sahara download mode (default: auto-detect via get_qsahara_files)")
    parser.add_argument("--fastboot-timeout", type=int,
                        help="Seconds to wait for the device to enumerate in fastboot after "
                             "power-cycle (default: 120 — large builds can take >60s to re-enumerate)")
    return parser.parse_args()


def main():
    setup_logging()
    args = parse_args()

    cfg = derive_config(args)

    log.info("===== flash_meta start (chip=%s board=%s dry_run=%s start_step=%d) =====",
             cfg.chip or "?", cfg.board or "?", cfg.dry_run, cfg.start_step)
    log.info("resolved meta path: %s", cfg.meta)

    if not cfg.domains:
        log.error(
            "No flash domain resolved (no --pf given and chip unknown). "
            "Pass --pf <flavor> --storage <ufs|spinor> explicitly."
        )
        sys.exit(1)

    try:
        flash_all(cfg)
    except FlashError as e:
        log.error("!!! FLASH FAILED: %s", e)
        log.error("COM/端口类错误可 power cycle 重试；仍失败请人工介入。")
        sys.exit(1)
    except KeyboardInterrupt:
        log.error("用户中断。")
        sys.exit(130)


if __name__ == "__main__":
    main()
