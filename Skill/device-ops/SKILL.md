---
name: device-ops
description: COMPANY automotive device operations — TAC serial control (power/EDL/fastboot), full meta build flashing (Firehose/Sahara), device info/log/dump collection. Pure scripts, no GUI automation. Read this before operating any connected COMPANY automotive device.
enabled: true
standing: false
origin: bundled
allowed-tools: [shell]
---
# Device Operations

COMPANY automotive platform device control and firmware operations via TAC serial boards and USB data channel. Zero dependency on Axiom, xPCAT, or any GUI — pure Python scripts using pyserial, subprocess calls to fh_loader/QSaharaServer/fastboot/adb.

## Available Scripts

| Script | Purpose |
|--------|---------|
| `${SKILL_DIR}/scripts/tac.py` | TAC board serial control: power on/off/cycle, boot to SS EDL / MD EDL / MD Fastboot |
| `${SKILL_DIR}/scripts/flash_meta.py` | Full meta build flash orchestration (EDL→Sahara→Provision→UFS→SPINOR→CDT→verify) |
| `${SKILL_DIR}/scripts/device_info.py` | Device status detection, firmware info, log grab (logcat/dmesg/journal/slog), dump collection |
| `${SKILL_DIR}/scripts/probe.py` | Environment self-check: TAC board / device state / tool chain / QPM / meta reachability |

**Always invoke scripts with the full `${SKILL_DIR}/scripts/...` path — never a bare relative path like `scripts/probe.py`.** The agent's shell cwd is the task workspace, not this skill's directory, so a relative path will not resolve and forces a blind filesystem search instead. `${SKILL_DIR}` is substituted by `read_skill` into this skill's actual on-disk directory at read time, so it resolves correctly regardless of machine or install layout (`%USERPROFILE%\HandQ\Skill\device-ops` for a user-owned copy, `<install_dir>\Skill\device-ops` for the bundled one).

## Prerequisites

- **TAC board** (Bantam `04D8:000A` or Alpaca `05C6:9302`) connected to PC via USB
- **USB data cable** from PC to DUT (device under test)
- **Tool chain**: fh_loader, QSaharaServer, fastboot, adb (install via QPM: `qpm-cli --install qfil --silent`)
- **Python**: pyserial (`pip install pyserial`)
- **For flashing**: meta build path reachable (UNC share or local)

Run `${SKILL_DIR}/scripts/probe.py` to verify all prerequisites in one shot.

## Cannot Do

- Operate a device without a TAC board (cannot control power/mode without hardware fixture)
- Flash non-COMPANY platforms
- Download/fetch meta builds (path must already exist)
- Push execution to remote machines (scripts run locally where TAC board is attached)
- JTAG-level debug or trace capture
- Partial partition flash via Firehose (use fastboot for single partitions)

## Reference Documents

- `${SKILL_DIR}/reference/capabilities.md` — Each script's parameters, output format, limitations
- `${SKILL_DIR}/reference/hardware-topology.md` — Physical topology, VID/PID table, SS vs MD EDL, QPM bootstrap
- `${SKILL_DIR}/reference/flash-meta.md` — Complete 10-step flash flow, default build server, per-chip parameter table, success criteria, failure recovery, and known pitfalls
- `${SKILL_DIR}/reference/post-flash-operations.md` — Perf apps swap (manual adb/fastboot, no `flash_meta.py`), VM-bridge/OS-type probing, apps-only verification signal

## Terminology — Mapping User Phrasing to Procedures

| User says | Procedure |
|---|---|
| "刷meta" / "flash meta" | `flash-meta.md` → `flash_meta.py`. Only case touching EDL/Sahara/fh_loader. |
| "刷perf apps" / "flash perf apps" | `post-flash-operations.md` §1 only — manual `fastboot`/`adb` on an already-flashed device. No `flash_meta.py`. |
| "刷perf meta" / "flash perf meta" | Both, in order: `flash-meta.md` first, then `post-flash-operations.md` §1. |

Ambiguous "perf" request → ask which one; a full `flash_meta.py` run costs tens of minutes the apps-only swap doesn't need.

## Parameter Auto-Derivation

`flash_meta.py` needs no config file. Give it a meta build ID or path and it derives the rest:

- Bare build ID (e.g. `SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1`) → prepends the default build server `\\grilled\nsid-sha-spsp-02\`
- Full UNC/absolute path → used as-is
- Chip name (leading token of the build ID, e.g. `SA8797P`) → looked up in a small built-in defaults table (product flavors, storage types, CDT path, Sahara mode) — kept in sync with `reference/flash-meta.md`
- Unknown chip → script warns and requires explicit `--pf`/`--storage` (and `--pf2`/`--storage2`/`--cdt-rel` as needed)

## Quick Start

```bash
# Check environment
python ${SKILL_DIR}/scripts/probe.py

# Device control (standalone, no flashing)
python ${SKILL_DIR}/scripts/tac.py power-cycle
python ${SKILL_DIR}/scripts/tac.py boot-ss-edl
python ${SKILL_DIR}/scripts/tac.py md-fastboot

# Full meta flash — build ID only, server + chip params auto-derived
python ${SKILL_DIR}/scripts/flash_meta.py --meta "SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1"

# Full UNC path also works
python ${SKILL_DIR}/scripts/flash_meta.py --meta "\\server\share\META_BUILD"

# Unknown chip: override explicitly
python ${SKILL_DIR}/scripts/flash_meta.py --meta "\\server\share\NEW_CHIP_BUILD" \
    --pf my_flavor --storage ufs

# Device info / logs / dumps
python ${SKILL_DIR}/scripts/device_info.py status
python ${SKILL_DIR}/scripts/device_info.py log --output ./logs/
python ${SKILL_DIR}/scripts/device_info.py dump --output ./dumps/
```
