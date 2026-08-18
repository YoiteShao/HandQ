# Hardware Topology

## Physical Connection Diagram

```
┌──────────────────────────────────────────────────────────┐
│                         PC (Host)                         │
│                                                          │
│  ┌─────────────┐                    ┌─────────────────┐  │
│  │ TAC control  │                    │ Flash/debug     │  │
│  │ (tac.py)    │                    │ fh_loader       │  │
│  │             │                    │ QSaharaServer   │  │
│  │             │                    │ adb / fastboot  │  │
│  └──────┬──────┘                    └────────┬────────┘  │
│         │                                    │           │
│     USB cable ①                         USB cable ②      │
│     (control channel)                   (data channel)   │
└─────────┼────────────────────────────────────┼───────────┘
          │                                    │
          ▼                                    │
┌───────────────────┐                          │
│   TAC Board        │                          │
│  (Bantam/Alpaca)  │                          │
│                   │                          │
│  serial cmds ───► │ power/GPIO pins ─►┐     │
└───────────────────┘                   │     │
                                        ▼     ▼
                         ┌──────────────────────────┐
                         │    Device Under Test      │
                         │         (DUT)             │
                         │                          │
                         │  ← power/reset via TAC   │
                         │  ← data USB direct to PC │
                         │                          │
                         │  Modes:                   │
                         │    EDL (9008) ← flashing  │
                         │    fastboot  ← CDT/parts  │
                         │    adb       ← normal OS  │
                         └──────────────────────────┘
```

## Key Concepts

### TAC Board is an External Fixture — Not Part of the Device

TAC (Test Access Controller) is an **independent board** attached to the test bench. It controls the DUT's power rail and boot-mode GPIO pins through physical wires. The device itself does NOT contain TAC hardware. "Device can be controlled via Alpaca" means someone physically connected a TAC board to it — not that the device has one built in.

### Two USB Paths

| Path | Connection | Purpose | Content |
|------|------------|---------|---------|
| ① Control | PC → TAC board | Power/mode control | Serial ASCII commands at 115200 baud |
| ② Data | PC → DUT directly | Flash/debug/adb | Firehose protocol (EDL), fastboot, adb |

These are independent — you can control power (①) without data connected (②), and you can adb (②) without a TAC board (①) if the device is already booted.

## TAC Board VID/PID Table

| Board | VID | PID | Dialect | Serial Commands |
|-------|-----|-----|---------|-----------------|
| **Bantam** | `04D8` | `000A` | `nordy` | `BOOT_SS_EDL\n`, `MD_FASTBOOT\n`, `PWR_OFF 1/0\n` |
| **Lemans Alpaca** | `05C6` | `9302` | `lemans_sx` | `devicePower 0/1\r`, `pin 1 31\r`, `gpio ...\r`, `ttl outputBit ...\r` |
| **FTDI (RideMX)** | `0403` | `6011` | (bit-bang) | FTDI D2XX bit-bang sequences (dual-SoC) |

Detection: `tac.py probe` or `probe.py` — both enumerate COM ports by VID/PID.

In Windows Device Manager, Bantam appears as **"USB Serial Device (COMx)"** (generic name, not labeled "Alpaca" or "TAC"). The FTDI ports (COM13/14/16) are typically the device's UART debug consoles, NOT the TAC control — only the Bantam/Alpaca COM port drives power/mode.

## SS EDL vs MD EDL

| | SS EDL | MD EDL |
|---|---|---|
| Full name | Sub-System EDL | Multi-Death EDL |
| Behavior | Resets only one sub-system into EDL | Forces the entire chip (all cores) into EDL |
| Stability | More reliable for SPINOR/SAIL flash | Occasionally unstable (depends on prior chip state) |
| Bantam command | `BOOT_SS_EDL` | `BOOT_MD_EDL` |
| When to use | Flashing SAIL/SPINOR domain | Flashing main UFS domain or full recovery |

**Why SS is preferred for SPINOR**: SS EDL precisely controls which sub-system yields the SPINOR bus. MD EDL does a brute-force whole-chip reset — if the prior state was dirty (mid-provision, another sub-system holding the bus), the programmer may see inconsistent peripheral state.

**Rule of thumb**: Use SS EDL for SPINOR; either works for UFS but always power-cycle between provision and write.

## QPM Bootstrap (From Zero to Ready)

For a fresh PC with nothing installed:

```bash
# 1. Install QPM3 itself (from network share / USB)
"\\server\share\Installers\QPM3.3.x.x.Windows-AnyCPU.exe" /SILENT

# 2. Login (service account)
qpm-cli --login <user> '<password>'

# 3. Install flash tools
qpm-cli --license-activate qfil && qpm-cli --install qfil --silent
# This provides: fh_loader.exe, QSaharaServer.exe, lsusb.exe

# 4. Install platform tools (adb/fastboot)
qpm-cli --license-activate QPST && qpm-cli --install QPST --silent
# Or use standalone Android Platform Tools

# 5. Install Python dependency
pip install pyserial

# 6. Verify everything
python probe.py
```

After this, all device-ops scripts will find their tools automatically (they search PATH + common COMPANY install dirs).
