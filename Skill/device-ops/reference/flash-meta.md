# Flash Meta Build — Complete Reference

## What is "Flash Meta"

Write a COMPANY meta build (a directory containing all sub-system images) to the device's storage (UFS / SPINOR). Equivalent to the full xPCAT GUI download flow, but executed entirely via command line.

## Default Build Server

Meta builds are typically located at:
```
\\grilled\nsid-sha-spsp-02\<BUILD_ID>
```

If the user provides only a build ID (e.g. `SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1`), prepend `\\grilled\nsid-sha-spsp-02\` to form the full path. If a full UNC path is given, use it directly.

## Device-Specific Parameters

The script auto-derives most parameters from the meta via `meta_cli`. Known device defaults for when auto-detection is ambiguous:

| Chip | Board | Main Domain (UFS) pf | SAIL Domain (SPINOR) pf | CDT relative path | Sahara mode |
|------|-------|---------------------|------------------------|-------------------|-------------|
| SA8797P | RIDESX | `nonsafe_ivi_pvm_lagvm` | `safe_rtos` | `common/cdt/RIDESX/cdt.bin` | Nordy multi-image |
| SA8775 | LeMans Star | `8775_la` | `sail_nor_lemans` | `common/cdt/LEMANSSTAR/cdt.bin` | Standard |
| SA8775 | RideSX | `8775_la` | `sail_nor_lemans` | `common/cdt/RIDESXLEMANS/cdt.bin` | Standard |

Notes:
- `meta_cli get_partition_files` can list available pf/storage combos — use it to verify
- Nordy platforms (SA8797P) need multi-image Sahara (`meta_cli get_qsahara_files` returns non-empty)
- Non-Nordy platforms (SA8775 etc.) use single-image Sahara (`prog_firehose_ddr.elf` only)

## Physical Prerequisites

| Condition | How to verify |
|-----------|---------------|
| TAC board connected | `tac.py probe` shows ✓ |
| Device USB data connected | Device Manager shows COMPANY device |
| Meta path reachable | `dir \\server\share\META...` has content |
| fh_loader + QSaharaServer in PATH | `where fh_loader.exe` returns a path |
| fastboot + adb in PATH | `where fastboot` / `where adb` |

Missing tools? Install via QPM:
```
qpm-cli --license-activate qfil && qpm-cli --install qfil --silent
```

## One-Command Full Flash

```bash
python flash_meta.py --meta "SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1"
```

Full UNC path also works if the build lives on a different server:
```bash
python flash_meta.py --meta "\\other-server\share\SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1"
```

Skip provision for already-provisioned boards:
```bash
python flash_meta.py --meta "SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1" --skip-provision
```

Resume from a specific step:
```bash
python flash_meta.py --meta "SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1" --start-step 8   # Start from SPINOR
python flash_meta.py --meta "SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1" --start-step 9   # CDT + verify only
```

Unknown chip (not in the defaults table above) — pass parameters explicitly:
```bash
python flash_meta.py --meta "\\server\share\NEW_CHIP_BUILD" --pf my_flavor --storage ufs
```

## 10-Step Flow Detail

### Steps 1-3: Enter EDL + Connect

**What**: TAC board sends `BOOT_SS_EDL` → device enters EDL → PC sees `QDLoader 9008 (COMx)` port

**Success criterion**: `serial.tools.list_ports` enumerates a COM port with `9008` in its description

**Failure recovery**:
- 60s no 9008 → TAC power cycle → resend EDL command
- 9008 port occupied → kill QUTS/tac.exe/PCATApp.exe to release

### Step 4: Locate Device Programmer

**What**: `meta_cli get_device_programmer flavor=<pf>` → returns path to programmer .elf

**Nordy platform**: Uses `device_programmer_ddr.elf` (non-Nordy uses `prog_firehose_ddr.elf`)

**How to determine Nordy**: `meta_cli get_qsahara_files` returns non-empty JSON = Nordy

### Step 5: UFS Provisioning (New Boards Only)

**What**:
```
fh_loader --setactivepartition=1 --noprompt --memoryname=ufs
  --sendxml=provision_ufs40_siod.xml
  --search_path=<meta>/common/config/ufs/provision
  --port=\\.\COMx --zlpawarehost=1
```

**Success criterion**: stdout contains `{All Finished Successfully}`

**⚠️ CRITICAL PITFALL**: Provisioning MUST complete in a **separate EDL session**. Reason: the programmer reads UFS geometry at Sahara load time. If provision and load share the same session, the programmer's in-memory geometry is still "0 sectors" (pre-provision value), causing backup GPT write to fail with `Asked NUM_DISK_SECTOR-5 outside total sectors 0`.

**Correct sequence**: Provision → power cycle → fresh EDL entry → fresh Sahara → then erase + load.

### Step 6: Generate partition_files.json

**What**:
```
meta_cli get_partition_files flavor='<pf>' storage='<ufs|spinor>' group=True
```
Outputs JSON: `{partition_bin: [...], partition: [...rawprogram.xml...], partition_patch: [...]}`

**Success criterion**: Both rawprogram list and bin list are non-empty

### Step 7: Write UFS Images

**What**:
```
# Erase first (UFS only — multi-LUN erase)
fh_loader --memoryname=ufs --erase=0 --erase=1 --erase=2 --erase=4 --erase=5 --erase=6 --erase=7 --port=\\.\COMx

# Then write
fh_loader --setactivepartition=1 --noprompt --showpercentagecomplete
  --memoryname=ufs --search_path=<temp> --json_in=<temp>/partition_files.json
  --flavor=<pf> --port=\\.\COMx --zlpawarehost=1
```

**Success criterion**: `{All Finished Successfully}` + rc=0

**Failure analysis**: Check `port_trace.txt` (fh_loader's detailed log, auto-dumped on failure)

### Step 8: Flash SPINOR (SAIL Domain)

**What**: Re-enter SS EDL → fresh Sahara → `fh_loader --memoryname=spinor`

**⚠️ CRITICAL PITFALLS**:
- SPINOR has **no multi-LUN erase**. Sending `--erase=0..7` will hang indefinitely. The rawprogram xml handles partition-level erasure internally.
- Must re-enter EDL + Sahara (UFS programmer session state is not suitable for SPINOR)

### Step 9: CDT + Fastboot

**What**:
```
# Exit EDL first
tac.py power-cycle
sleep 10

# Enter fastboot
tac.py md-fastboot

# Flash CDT + reboot
fastboot flash cdt "<meta>/common/cdt/RIDESX/cdt.bin"
fastboot reboot
```

**⚠️ CRITICAL PITFALL**: After SPINOR flash, device is still in EDL (programmer loaded). You **must power cycle** to exit EDL before entering fastboot. Sending `MD_FASTBOOT` to a device in EDL has no effect — there's no OS running to receive the command.

### Step 10: Verify

**What**:
```
adb wait-for-device
adb shell cat /firmware/verinfo/ver_info.txt
```

**Success criterion**: Output contains `Meta_Build_ID=<your meta name>`

## Sahara Protocol (Nordy Multi-Image)

Nordy platforms (SA8797P is one) use multi-image Sahara. The device sequentially requests multiple image-ids (e.g., 13=programmer, 59=other firmware). Command:

```
QSaharaServer -s 13:device_programmer_ddr.elf -s 59:<another_file> ...
  -b <programmer_dir>/ -b <other_dir>/
  -p \\.\COMx
```

Image-id list comes from `meta_cli get_qsahara_files` (returns `{"13":[path], "59":[path], ...}`).

**Success criterion**: stdout contains `Sahara protocol completed`

## SS EDL vs MD EDL

| | SS EDL | MD EDL |
|---|---|---|
| Full name | Sub-System EDL | Multi-Death EDL |
| Behavior | Resets one sub-system only | Forces entire chip into EDL |
| Stability | More reliable for SPINOR | Occasionally unstable (depends on chip history) |
| Bantam command | `BOOT_SS_EDL` | `BOOT_MD_EDL` |
| Recommended for | SAIL/SPINOR flash | UFS main domain / full recovery |

## Deriving Parameters from Meta Path

Example meta: `\\grilled\nsid-sha-spsp-02\SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1`

- **Product Flavor (pf)**: Parsed from `contents.xml` by `meta_cli`. Common: `nonsafe_ivi_pvm_lagvm` (UFS main), `safe_rtos` (SPINOR SAIL)
- **Storage Type**: From `meta_cli get_partition_files` `storage` param. Derived from pf or explicit.
- **CDT path**: `<meta>/common/cdt/<board_type>/cdt.bin`
- **Provision XML**: `<meta>/common/config/ufs/provision/provision_ufs40_siod.xml` (Nordy) or `provision_default.xml`
- **meta_cli**: `<meta>/common/build/app/windows_x86_64/meta_cli.exe` (Win) or `.../linux/meta_cli` (Linux)

## Common Errors and Fixes

| Error | Root Cause | Fix |
|-------|-----------|-----|
| `Matching input image for ID 59 not found` | Nordy needs multi-image Sahara; only ID 13 was provided | Use `get_qsahara_files` to get full image-id list |
| `NUM_DISK_SECTOR-5 outside total sectors 0` | Provision and load in same EDL session | Power cycle after provision, then fresh EDL session |
| SPINOR erase timeout/hang | Sent UFS-style `--erase=0..7` to SPINOR | SPINOR has no LUN erase; skip it |
| Fastboot device not found | Sent fastboot command while still in EDL | Power cycle to exit EDL first |
| `60s 内未发现 fastboot 设备` (step 9 timeout) | Large builds can take >60s to re-enumerate in fastboot after power-cycle — confirmed on real hardware. `fastboot_timeout` default is now 120s; if still too short, pass `--fastboot-timeout 180` | Do NOT restart the whole flash — `tac.py power-cycle` + `tac.py md-fastboot` manually to get the device into fastboot, confirm with `fastboot devices`, then resume with `flash_meta.py ... --start-step 9` (UFS/SPINOR writes already completed are not re-run) |
| `port_trace.txt` reports file not found | `partition_files.json` paths unreachable | Check network share permissions / connectivity |
| Sahara no response | QUTS service occupying 9008 port | Kill QUTS/tac.exe then retry |
