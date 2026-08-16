#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tac.py — TAC (Test Access Controller) 设备控制工具

通过串口直接驱动 Bantam / Alpaca 控制板，无需任何第三方 GUI 软件。
既可作为独立 CLI 使用，也可被其他脚本导入。

CLI 用法:
  tac.py boot-ss-edl          # 让 device 进入 SS EDL
  tac.py md-fastboot           # 让 device 进入 MD fastboot
  tac.py power-cycle           # 重启 device
  tac.py power-off             # 关闭 device
  tac.py power-on              # 启动 device
  tac.py probe                 # 探测本机 TAC 板
  tac.py --port COM11 power-cycle   # 指定端口
  tac.py --dialect lemans_sx boot-ss-edl  # 指定方言

库导入:
  from tac import TacController
  tac = TacController()        # 自动探测
  tac.boot_ss_edl()
  tac.power_cycle()
  tac.close()
"""

import argparse
import logging
import os
import sys
import time
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
    _HAVE_SERIAL = True
except ImportError:
    serial = None
    _HAVE_SERIAL = False

log = logging.getLogger(__name__)

# ============================================================================
# TAC 板 USB 指纹 → 命令方言
# ============================================================================

VIDPID_DIALECT = {
    (0x04D8, 0x000A): "nordy",       # Bantam (序列号常含 BANTAM)
    (0x05C6, 0x9302): "lemans_sx",   # Lemans Alpaca
}


class TacError(Exception):
    """TAC 操作失败。"""


class TacController:
    """
    TAC 控制器：纯 pyserial 驱动，支持两套命令方言。

    - nordy    : Bantam 板高级命名命令 (BOOT_SS_EDL / MD_FASTBOOT / PWR_OFF)
    - lemans_sx: 老 Lemans Alpaca 低级引脚序列 (devicePower / pin / gpio / ttl)
    """

    def __init__(self, port: Optional[str] = None, dialect: Optional[str] = None,
                 baud: int = 115200, dry_run: bool = False):
        """
        Args:
            port: COM 口 (None = 自动探测)
            dialect: 'nordy' | 'lemans_sx' (None = 按 VID/PID 自动选)
            baud: 波特率 (默认 115200)
            dry_run: True 则不打开串口、不发送命令
        """
        self.dry_run = dry_run
        self.baud = baud
        self._dialect = dialect
        self._auto_dialect = dialect is None
        self.port = port or self._detect_port()
        self.dialect = self._dialect or "nordy"  # fallback
        self.ser = None
        if not dry_run:
            if not _HAVE_SERIAL:
                raise TacError("未安装 pyserial: pip install pyserial")
            self.ser = serial.Serial(self.port, self.baud, timeout=1)
        log.info("TacController: port=%s dialect=%s", self.port, self.dialect)

    def _detect_port(self) -> str:
        if self.dry_run:
            return "COM_TAC(dry-run)"
        if not _HAVE_SERIAL:
            raise TacError("未安装 pyserial，无法探测 TAC 板: pip install pyserial")
        for p in serial.tools.list_ports.comports():
            if p.vid is None:
                continue
            key = (p.vid, p.pid)
            if key in VIDPID_DIALECT:
                if self._auto_dialect:
                    self._dialect = VIDPID_DIALECT[key]
                    self.dialect = self._dialect
                log.info("检测到 TAC 板: %s [%04X:%04X] dialect=%s sn=%s",
                         p.device, p.vid, p.pid, VIDPID_DIALECT[key], p.serial_number or "")
                return p.device
        raise TacError(
            "未探测到已知 TAC 控制板。已知指纹: "
            + ", ".join(f"{v:04X}:{p:04X}" for v, p in VIDPID_DIALECT)
            + "\n检查 USB 连接，或用 --port 手动指定。"
        )

    def _w(self, s: str):
        log.debug("TAC <- %r", s)
        if not self.dry_run and self.ser:
            self.ser.write(s.encode("ascii"))

    # ==== Nordy (Bantam) 命令 ====
    def _nordy_ss_edl(self):
        self._w("BOOT_SS_EDL\n"); time.sleep(5)

    def _nordy_md_edl(self):
        self._w("BOOT_MD_EDL\n"); time.sleep(5)

    def _nordy_fastboot(self):
        self._w("MD_FASTBOOT\n"); time.sleep(10)

    def _nordy_off(self):
        self._w("PWR_OFF 1\n"); time.sleep(2)

    def _nordy_on(self):
        self._w("PWR_OFF 0\n"); time.sleep(2)

    # ==== Lemans SX 命令 ====
    def _lsx_ss_edl(self):
        self._w("devicePower 0\r"); time.sleep(0.5)
        self._w("pin 1 31\r"); time.sleep(0.05)
        self._w("devicePower 1\r"); time.sleep(0.1)
        self._w("ttl outputBit 1 1\r"); time.sleep(0.5)
        self._w("ttl outputBit 1 0\r"); time.sleep(0.5)
        self._w("pin 0 31\r"); time.sleep(0.5)

    def _lsx_fastboot(self):
        self._w("devicePower 0\r"); time.sleep(0.5)
        self._w("ttl outputBit 1 0\r"); time.sleep(0.1)
        self._w("gpio volup 0\r"); time.sleep(0.01)
        self._w("ttl outputBit 2 1\r"); time.sleep(0.1)
        self._w("ttl outputBit 4 0\r"); time.sleep(0.5)
        self._w("devicePower 1\r"); time.sleep(0.9)
        self._w("ttl outputBit 1 1\r"); time.sleep(0.8)
        self._w("ttl outputBit 1 0\r"); time.sleep(8.1)
        self._w("ttl outputBit 2 0\r")

    def _lsx_off(self):
        self._w("devicePower 0\r"); time.sleep(1.5)

    def _lsx_on(self):
        self._w("devicePower 1\r"); time.sleep(1.5)
        self._w("ttl outputBit 1 1\r"); time.sleep(1.5)
        self._w("ttl outputBit 1 0\r")

    # ==== 对外统一接口 ====
    def boot_ss_edl(self):
        """让 device 进入 SS (Sub-System) EDL 模式。"""
        log.info("TAC: Boot SS EDL")
        if self.dialect == "lemans_sx":
            self._lsx_ss_edl()
        else:
            self._nordy_ss_edl()

    def boot_md_edl(self):
        """让 device 进入 MD (Multi-Death/整机) EDL 模式。仅 nordy 方言支持独立命令。"""
        log.info("TAC: Boot MD EDL")
        if self.dialect == "lemans_sx":
            # Lemans SX 没有独立的 MD EDL 命令，用 SS EDL 代替
            log.warning("lemans_sx 无独立 MD EDL 命令，退回 SS EDL")
            self._lsx_ss_edl()
        else:
            self._nordy_md_edl()

    def md_fastboot(self):
        """让 device 进入 MD fastboot 模式。"""
        log.info("TAC: MD Fastboot")
        if self.dialect == "lemans_sx":
            self._lsx_fastboot()
        else:
            self._nordy_fastboot()

    def power_off(self):
        """关闭 device 电源。"""
        log.info("TAC: Power Off")
        if self.dialect == "lemans_sx":
            self._lsx_off()
        else:
            self._nordy_off()

    def power_on(self):
        """启动 device 电源。"""
        log.info("TAC: Power On")
        if self.dialect == "lemans_sx":
            self._lsx_on()
        else:
            self._nordy_on()

    def power_cycle(self, delay: float = 2.0):
        """重启 device (power off → delay → power on)。"""
        log.info("TAC: Power Cycle (delay=%.1fs)", delay)
        self.power_off()
        time.sleep(delay)
        self.power_on()

    def close(self):
        """关闭串口。"""
        if self.ser:
            self.ser.close()
            self.ser = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ============================================================================
# probe: 探测本机 TAC 板
# ============================================================================

def probe() -> dict:
    """
    探测本机 TAC 板信息。返回 dict:
      {"found": bool, "port": str|None, "vid": int, "pid": int, "dialect": str, "serial": str}
    """
    result = {"found": False, "port": None, "vid": 0, "pid": 0, "dialect": "", "serial": ""}
    if not _HAVE_SERIAL:
        log.warning("pyserial 未安装，无法探测")
        return result
    for p in serial.tools.list_ports.comports():
        if p.vid is None:
            continue
        key = (p.vid, p.pid)
        if key in VIDPID_DIALECT:
            result.update({
                "found": True,
                "port": p.device,
                "vid": p.vid,
                "pid": p.pid,
                "dialect": VIDPID_DIALECT[key],
                "serial": p.serial_number or "",
            })
            return result
    return result


# ============================================================================
# CLI
# ============================================================================

COMMANDS = {
    "boot-ss-edl": "让 device 进入 SS EDL 模式",
    "boot-md-edl": "让 device 进入 MD EDL 模式",
    "md-fastboot": "让 device 进入 MD Fastboot 模式",
    "power-on": "启动 device",
    "power-off": "关闭 device",
    "power-cycle": "重启 device",
    "probe": "探测本机 TAC 控制板",
}


def main():
    parser = argparse.ArgumentParser(
        description="TAC 设备控制工具 — 纯串口驱动 Bantam/Alpaca 板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="命令:\n" + "\n".join(f"  {k:<14} {v}" for k, v in COMMANDS.items()),
    )
    parser.add_argument("command", choices=COMMANDS.keys(), help="操作命令")
    parser.add_argument("--port", help="TAC 板 COM 口（默认自动探测）")
    parser.add_argument("--dialect", choices=["nordy", "lemans_sx"], help="命令方言（默认按板型自动选）")
    parser.add_argument("--dry-run", action="store_true", help="不发送命令，只打印")
    parser.add_argument("--delay", type=float, default=2.0, help="power-cycle 中间等待秒数（默认 2）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if args.command == "probe":
        info = probe()
        if info["found"]:
            print(f"✓ TAC 板: {info['port']} [{info['vid']:04X}:{info['pid']:04X}] "
                  f"dialect={info['dialect']} sn={info['serial']}")
        else:
            print("✗ 未发现已知 TAC 控制板")
            sys.exit(1)
        return

    with TacController(port=args.port, dialect=args.dialect, dry_run=args.dry_run) as tac:
        cmd_map = {
            "boot-ss-edl": tac.boot_ss_edl,
            "boot-md-edl": tac.boot_md_edl,
            "md-fastboot": tac.md_fastboot,
            "power-on": tac.power_on,
            "power-off": tac.power_off,
            "power-cycle": lambda: tac.power_cycle(delay=args.delay),
        }
        cmd_map[args.command]()
        print(f"✓ {args.command} 完成")


if __name__ == "__main__":
    main()
