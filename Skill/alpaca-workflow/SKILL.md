---
name: alpaca-workflow
description: Alpaca Test Controller (TAC.exe) automation — launching, connecting, power control, and boot-mode entry (EDL/Fastboot/UEFI) for Qualcomm automotive SoC boards. Read this before driving any Alpaca button.
enabled: true
standing: false
origin: bundled
allowed-tools: [desktop_find_and_click, desktop_screenshot, desktop_click_at, desktop_list_windows, shell]
process-hints:
  TAC.exe: "none_detected on TAC buttons is EXPECTED, not a failure — custom render engine, UIA blind to the hardware effect (battery cut, GPIO assert). Verify via real device state, not the click's effect field. Do not retry the click or drop to raw Win32 SendInput/PostMessage/SendMessage — use desktop_find_and_click(use_uia_pattern=False), which already works."
---
# Alpaca Test Controller Workflow

Alpaca Test Controller (TAC) is Qualcomm's GPIO-sequencing utility for automotive/mobile
evaluation boards. It drives a **debug controller** (NordAu RIDE, PIC32CXAuto on COM4) via
USB-serial; that board in turn asserts hardware GPIO lines on the **target SoC** (SA8797P /
KARUSSELL) to perform power cycles and boot-mode entry.

## CRITICAL: Custom Renderer — Use `desktop_find_and_click` with `use_uia_pattern=False`

TAC uses a **custom rendering engine** — standard UIA (Invoke/Value patterns) is silently
ignored by every button. The one-tool pattern that works:

```
desktop_find_and_click("<button label>", use_uia_pattern=False)
```

`desktop_find_and_click` runs **OCR internally** to locate the button by text, then clicks
it via **raw mouse** when `use_uia_pattern=False`. No manual coordinate lookup needed.

```
# CORRECT — OCR find + raw mouse click in one call
desktop_find_and_click("Boot MD EDL",         use_uia_pattern=False)
desktop_find_and_click("Power On",            use_uia_pattern=False)
desktop_find_and_click("Boot to MD Fastboot", use_uia_pattern=False)

# WRONG — UIA click is swallowed, effect: none_detected
desktop_click_at(x=433, y=655)              # use_uia_pattern defaults to True
desktop_find_and_click("Boot MD EDL")       # UIA InvokePattern fails silently
```

Clicks produce `content_changed: false` / `effect: none_detected` even when they **do**
work — this is expected. The hardware effect (battery cut, GPIO assert) is invisible to
the UIA layer. Verify by checking real device state (see § Verification).

## Launching & Connecting

```
1. shell: Start-Process "C:\Users\Public\Desktop\Alpaca Test Controller.lnk"
2. wait_interval(seconds=3)
3. A "How are we doing?" survey dialog may appear — dismiss it:
       desktop_find_and_click("No thanks",  use_uia_pattern=False)   # or "Cancel" / "Close"
4. TAC main window opens — Status: Disconnected.
       desktop_find_and_click("Connect", use_uia_pattern=False)
5. "TAC Device Selection" dialog opens.
       desktop_find_and_click("COM4",  use_uia_pattern=False)         # select the device row
       desktop_find_and_click("OK",    use_uia_pattern=False)         # confirm
6. Window title changes to "Test Automation Controller  COM4".
   Status bar reads: Status: Connected.
```

> After each dialog, call `desktop_list_windows` to confirm the active hwnd — a stale
> hwnd returns frozen screenshot content forever.

## UI Layout — General Tab

Three tabs (always use **General** for device control):

| Tab | Purpose |
|-----|---------|
| **General** | All controls — Connections, Buttons, Switches, **Quick Settings**, Variables |
| Device Info | Read-only board metadata (HW, firmware, serial, config version) |
| Terminal | Command history + free-form command entry |

Navigate to General tab if not already there:
```
desktop_find_and_click("General", use_uia_pattern=False)
```

### Layout overview

```
▼ Connections
    Battery [toggle]

▼ Buttons  — raw GPIO momentary-press (advanced use only)
    KK Power I…  MD PS HOLD  KK Reset      SAIL PS HOLD
    Power Off    Force MD PS  PMS Pow…     SAIL Power On
    MD EDL       Force SS PS  Fastboot     SAIL Subsystem Fa…
    SS EDL       UEFI         EUD

▼ Switches  — persistent GPIO toggles
    PCIE Attention  Mode 1  Mode 2

▼ Quick Settings  ← USE THESE
    Power On           Power Off          Boot MD EDL       Boot to SS MD Fastboot
    Boot SS EDL        Boot to UEFI       Boot to MD Fastboot

▼ Variables  (editable timing parameters)
    EDL timing (ms): 2500
    Fastboot timing (ms): 12000
    UEFI timing (ms): 13000
```

**Use Quick Settings, not raw Buttons.** Quick Settings execute a complete, safe,
sequenced GPIO script (power-cycle + pin assert/release). Raw Buttons assert individual
GPIO pins — only use them if you know the exact hardware signal needed.

## Quick Settings Reference

### Power Controls

| Button label | `use_uia_pattern=False` call | Behaviour |
|---|---|---|
| **Power On** | `desktop_find_and_click("Power On", use_uia_pattern=False)` | Cuts battery ~900 ms, restores → cold reboot |
| **Power Off** | `desktop_find_and_click("Power Off", use_uia_pattern=False)` | Cuts battery and holds off until Power On |

### Boot Mode Controls

| Button label | Call | Target SoC | Boot mode | Wait after |
|---|---|---|---|---|
| **Boot MD EDL** | `desktop_find_and_click("Boot MD EDL", use_uia_pattern=False)` | MD (primary) | EDL 9008 — COM7 | 5 s |
| **Boot SS EDL** | `desktop_find_and_click("Boot SS EDL", use_uia_pattern=False)` | SS / SAIL | SAIL EDL | 5 s |
| **Boot to MD Fastboot** | `desktop_find_and_click("Boot to MD Fastboot", use_uia_pattern=False)` | MD (primary) | Android fastboot | 15 s |
| **Boot to SS MD Fastboot** | `desktop_find_and_click("Boot to SS MD Fastboot", use_uia_pattern=False, fuzzy_threshold=60)` | SS / SAIL | SAIL fastboot | 15 s |
| **Boot to UEFI** | `desktop_find_and_click("Boot to UEFI", use_uia_pattern=False)` | MD | UEFI menu | 16 s |

> ⚠️ "Boot to SS MD Fastboot" may appear truncated in OCR (e.g. "oottoSSMDFastbod").
> Use `fuzzy_threshold=60` or search for a shorter substring via `desktop_find_element`.

**Terminology:**
- **MD** = Modem / Primary SoC (main application processor, SA8797P)
- **SS / SAIL** = Secondary SoC (secondary subsystem; also called SAIL in Qualcomm docs)
- **EDL** = Emergency Download mode — Qualcomm 9008 USB protocol for low-level flashing
- **Fastboot** = Android bootloader protocol for partition flashing / booting test images

### Internal GPIO Script — Boot MD EDL

```
sedl=0; uefi=0          # clear secondary EDL and UEFI pins
battery=1               # disconnect battery (power off target)
sleep(EDL_timing_ms)    # default 2500 ms
pedl=1                  # assert primary EDL pin
battery=0               # restore battery → target boots with pedl held → 9008 mode
sleep(3000)
pedl=0                  # release pin (device stays in 9008 until reset)
```

### Internal GPIO Script — Boot to MD Fastboot

```
pedl=0; sedl=0; uefi=0  # clear all boot mode pins
battery=1               # disconnect battery
sleep(fastboot_timing)  # default 12000 ms
fastboot=1              # assert fastboot pin
battery=0               # restore battery → boots to fastboot
sleep(3000)
fastboot=0              # release pin
```

## Typical Workflow

```
# 1. Make sure General tab is active
desktop_find_and_click("General", use_uia_pattern=False)

# 2. Put device in clean boot state
desktop_find_and_click("Power On", use_uia_pattern=False)
wait_interval(seconds=4)   # battery cycle ~900 ms; 4 s is safe

# 3. Enter desired boot mode
desktop_find_and_click("Boot MD EDL",         use_uia_pattern=False)  ;  wait_interval(seconds=5)
# — or —
desktop_find_and_click("Boot to MD Fastboot", use_uia_pattern=False)  ;  wait_interval(seconds=15)
# — or —
desktop_find_and_click("Boot SS EDL",         use_uia_pattern=False)  ;  wait_interval(seconds=5)

# 4. Verify real device state (see § Verification)

# 5. Recover — Power On back to normal boot
desktop_find_and_click("Power On", use_uia_pattern=False)
# Allow ~60–120 s for Android to fully boot before adb devices responds
```

## Verification — Real Device State

TAC's **"Status: Connected"** reflects connection to the NordAu RIDE debug board only.
It does **not** change when the target SoC transitions between boot modes.

Verify the target device's actual boot state with shell commands:

### MD EDL (9008) active

```powershell
Get-PnpDevice | Where-Object { $_.FriendlyName -match 'QDLoader' } |
    Select-Object FriendlyName, Status
# Active:  Qualcomm HS-USB QDLoader 9008 (COM7)   Status: OK
# Inactive: same row but Status: Unknown
```

### MD Fastboot active

```powershell
fastboot devices
# Expected: 158887aa    fastboot

Get-PnpDevice | Where-Object { $_.FriendlyName -match 'D00D' } |
    Select-Object FriendlyName, Status
# Expected: Android Bootloader Interface D00D (0001)  Status: OK
```

### Android (ADB) running

```powershell
adb devices -l
# Expected: 158887aa  device  ... (after ~60-120 s from Power On)
```

### Device state summary

| Alpaca action | QDLoader 9008 Status | D00D Bootloader Status | ADB | `fastboot devices` |
|---|---|---|---|---|
| Power On (booting) | Unknown | Unknown | empty | empty |
| Power On (Android up) | Unknown | Unknown | `158887aa device` | empty |
| Boot MD EDL | **OK** | Unknown | empty | empty |
| Boot to MD Fastboot | Unknown | **OK** | empty | `158887aa  fastboot` |
| Power Off | Unknown | Unknown | empty | empty |

## Connected Hardware Reference

| Item | Value |
|------|-------|
| Debug board | NordAu RIDE (PIC32CXAuto) on COM4 |
| Debug board serial | KARUSSELLXXBANTAMTDC00002MBMA |
| Target device name | Karussell (SA8797P) |
| Target USB serial | 158887AA |
| Alpaca install dir | `C:\Program Files (x86)\Qualcomm\Alpaca\` |
| TAC executable | `TAC.exe` in above dir |
| Desktop shortcut | `C:\Users\Public\Desktop\Alpaca Test Controller.lnk` |
| Config file | `C:\ProgramData\Qualcomm\Alpaca\tac_configs\TAC_PIC32CXAuto_54.tcnf` |
| User logs dir | `C:\Users\<user>\Documents\Alpaca\global\TAC_*.log` |

## Common Mistakes

| Mistake | Correct approach |
|---|---|
| `desktop_find_and_click("Boot MD EDL")` — no `use_uia_pattern` | Always add `use_uia_pattern=False`; without it the UIA click is silently swallowed |
| `desktop_snapshot` or `desktop_click_at` with hardcoded coords | Use `desktop_find_and_click(label, use_uia_pattern=False)` — OCR find + raw click in one call |
| Checking Alpaca status after a boot-mode click | `effect: none_detected` is normal; verify via `fastboot devices` / `Get-PnpDevice` / `adb devices` |
| Reading "Status: Connected" as device mode | Status: Connected = debug board connection, not target SoC boot state |
| Raw Buttons section instead of Quick Settings | Raw Buttons assert single GPIO pins — unsafe without knowing the full sequence |
