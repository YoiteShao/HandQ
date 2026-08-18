# device-ops Script Capabilities

## tac.py

**Purpose**: Drive a TAC control board (Bantam/Alpaca) over serial to control device power and boot mode.

**CLI**:
```
tac.py <command> [--port COMx] [--dialect nordy|lemans_sx] [--dry-run] [--delay N]
```

**Commands**:
| Command | Effect |
|---------|--------|
| `boot-ss-edl` | Enter SS (Sub-System) EDL mode |
| `boot-md-edl` | Enter MD (Multi-Death) EDL mode |
| `md-fastboot` | Enter MD Fastboot mode |
| `power-on` | Power on the device |
| `power-off` | Power off the device |
| `power-cycle` | Power off → delay → power on (delay configurable, default 2s) |
| `probe` | Detect TAC board on this machine |

**Parameters**:
- `--port`: Manual COM port override (default: auto-detect by VID/PID)
- `--dialect`: Command dialect `nordy` (Bantam) or `lemans_sx` (Alpaca). Auto-selected by board type if omitted
- `--dry-run`: Print commands without opening serial or sending anything
- `--delay`: Seconds between power-off and power-on during power-cycle

**Output**: `✓ <command> done` on success; exit 1 on failure

**Library import**:
```python
from tac import TacController
with TacController() as tac:
    tac.boot_ss_edl()
```

**Cannot do**:
- Flash firmware (only controls power/mode)
- Communicate with the device itself (only talks to the TAC board)
- Operate a device without a TAC board connected
- `lemans_sx` dialect has no independent MD EDL command (falls back to SS EDL)

---

## flash_meta.py

**Purpose**: Full meta build flash orchestrator. Automates the 10-step flow: EDL → Sahara → UFS provision → UFS write → SPINOR write → CDT fastboot → verify. No config file needed — parameters auto-derive from the meta build ID.

**CLI**:
```
flash_meta.py --meta <build_id_or_path>
              [--pf FLAVOR] [--storage ufs] [--pf2 FLAVOR] [--storage2 spinor]
              [--cdt-rel PATH] [--provision-xml-rel PATH] [--sahara-mode nordy_multi|standard]
              [--skip-provision] [--start-step N] [--dry-run] [--fastboot-timeout N]
              [--tac-port COMx] [--tac-dialect nordy|lemans_sx]
```

**Parameters**:
- `--meta`: Meta build ID (bare, gets default server prepended) or full UNC/absolute path — **required**
- `--pf` / `--storage`: Main domain product flavor / storage type (overrides chip auto-detect)
- `--pf2` / `--storage2`: Second domain (SAIL/SPINOR) product flavor / storage type
- `--cdt-rel`: CDT path relative to meta root (overrides chip auto-detect)
- `--provision-xml-rel`: UFS provision XML path relative to meta root
- `--sahara-mode`: `nordy_multi` (multi-image Sahara) or `standard` (single-image)
- `--skip-provision`: Skip UFS provisioning (for already-provisioned boards)
- `--start-step N`: Resume from step N (for interrupted flashes)
- `--dry-run`: Print all commands without executing
- `--fastboot-timeout N`: Seconds to wait for fastboot re-enumeration after step-9 power-cycle (default: 120 — large builds have been observed taking >60s on real hardware)
- `--tac-port` / `--tac-dialect`: Override TAC board COM port / command dialect

**Auto-derivation**: Chip name is extracted from the meta build ID's leading token (e.g. `SA8797P` from `SA8797P.HGY.5.1.7.0...`) and looked up in a built-in defaults table (see `flash-meta.md`). Unknown chips require explicit `--pf`/`--storage` or the script exits with an error listing known chips.

**Output**: Step-by-step progress log; final adb verification of Meta_Build_ID

**Dependencies**: tac.py, fh_loader, QSaharaServer, meta_cli, fastboot, adb

**Cannot do**:
- Flash a single partition (full meta only; use raw fastboot for partials)
- Handle non-COMPANY platforms
- Auto-download meta builds (path must pre-exist)
- Execute remotely (must run on the PC where TAC board is attached)
- Correctly guess parameters for a chip not in the built-in defaults table (falls back to requiring explicit flags)

---

## device_info.py

**Purpose**: Detect device state, read firmware info, grab logs, collect crash dumps. Pure USB data-channel operations — no TAC board needed.

**CLI**:
```
device_info.py <action> [--adb PATH] [--fastboot PATH] [--type LOG_TYPE] [--output DIR]
```

**Actions**:
| Action | Purpose | Device must be in |
|--------|---------|-------------------|
| `status` | Detect current device mode | Any (enumerates all possible states) |
| `info` | Read firmware version, OS type, build fingerprint | adb mode |
| `log` | Grab logs (logcat/dmesg/slog2/journal) | adb mode |
| `dump` | Pull crash dump files | adb mode |

**Status return values**: `adb` / `fastboot` / `recovery` / `edl` / `offline` / `absent`

**`info` output fields**: `ver_info_path`, `Meta_Build_ID`, `Product_Flavor` (parsed from ver_info.txt JSON — tries both known mount points `/firmware/verinfo/ver_info.txt` and `/vendor/firmware_mnt/verinfo/ver_info.txt`, uses whichever responds); `build_display_id`, `build_fingerprint` (present only if this connection's userspace is AOSP-based); `os_release_version_id`, `os_release_pretty_name` (from `/etc/os-release`, present only if this connection's userspace is Yocto/OpenEmbedded-based); `os_type`.

**⚠️ `Meta_Build_ID` does not reflect apps-only flashes** — it's written by the firmware layer at full-meta-flash time. To verify an apps-only fastboot flash, diff whichever of `build_fingerprint`/`os_release_version_id` is non-empty on your connection, before/after. Which VM adb bridges to (PVM/GVM) and what OS that VM runs are independent, non-fixed dimensions — always probe, never assume "GVM implies Android" or any other fixed mapping. See `reference/post-flash-operations.md` §2-3.

**Supported log types**: logcat (Android), dmesg (Linux/Android), journal (systemd Linux), slog (QNX), kmsg

**Dump scan paths**: `/data/tombstones/`, `/data/vendor/ramdump/`, `/data/vendor/ssrdump/`, `/var/log/dumps/` (QNX), `/data/core/`

**Output**:
- status: prints mode name; exit 1 if absent
- info: prints key-value pairs
- log: saves `<type>_<timestamp>.log` to output dir
- dump: pulls directories to output dir

**Cannot do**:
- Control device power/mode (that's tac.py's job)
- Flash anything
- Communicate with a device in EDL (can only detect the 9008 port exists)
- Capture JTAG/trace-level logs

---

## probe.py

**Purpose**: Environment self-check. Validates all prerequisites for device-ops in one shot.

**CLI**:
```
probe.py [--meta "\\path\to\meta"]
```

**Parameters**:
- `--meta`: (optional) Meta build path — adds reachability check and meta_cli presence check

**Checks performed**:
1. TAC board connection (all known VID/PIDs)
2. Device state (EDL / adb / fastboot / absent)
3. Tool availability: fh_loader, QSaharaServer, fastboot, adb
4. QPM installation (qpm-cli --version)
5. Meta path reachability (only when `--meta` provided)
6. pyserial module installed

**Output**: One line per check with `✓`/`✗` + explanation; final summary of pass/fail counts

**Cannot do**:
- Install missing tools (only reports what's missing)
- Fix connection issues
- Validate meta build content integrity (only checks path accessibility)
