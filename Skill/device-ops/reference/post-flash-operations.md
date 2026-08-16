# Post-Flash Operations — Perf Variant, Version Signal Probing, Apps-Only Flash Verification

Not part of the TAC/Firehose flash pipeline in `scripts/` — these are manual
adb/fastboot steps performed *after* a normal meta flash completes. Kept here
because they are commonly needed follow-ups to "flash meta".

## 1. Flashing the Perf Variant of a Meta

A "perf" build swaps only the `apps` domain images for a performance-tuned
variant, then repoints the NSP config so ADAS-tuned settings apply to the
IVI/HPASS_IVI paths. Everything else (UFS main domain, SPINOR/SAIL, CDT)
stays whatever the base meta already flashed.

### Step 1 — Read the currently-flashed apps build ID

```bash
adb shell cat /firmware/verinfo/ver_info.txt
# if empty/error, try the other known mount point:
adb shell cat /vendor/firmware_mnt/verinfo/ver_info.txt
```

Look for the `apps` field in the JSON output — this tells you which apps
build the currently-flashed meta shipped, which you use to locate the
sibling perf build directory on the share. Which of the two paths responds
is not fixed — see §2 for why — so try both.

### Step 2 — Locate the perf apps images

Perf images live in a sibling directory to the main meta, following this
pattern:

```
<apps_build_root>\apps_proc\poky\build\tmp-glibc\deploy\images\<machine>-automotive-perf
```

Example:
```
pushd \\grilled\nsid-sha-spsp-02\LY.AU.0.1.1-42200-gen5meta.1-1\apps_proc\poky\build\tmp-glibc\deploy\images\sa8797-automotive-perf
```

### Step 3 — Fastboot flash the perf images

Device must be in fastboot (see `tac.py md-fastboot`, then power-cycle out
of EDL first if coming from a fresh Firehose flash — see flash-meta.md's
step 9 pitfall).

```bash
fastboot flash boot_a    .\sa8797-boot.img
fastboot flash system_a  .\machine-image-sa8797.ext4
fastboot flash persist   .\sa8797-persist.ext4
fastboot flash userdata  .\sa8797-usrfs.ext4
fastboot flash abl_a     .\sa8797-abl.elf
fastboot flash vbmeta_a  sa8797-vbmeta.img
```

Filenames vary by machine/meta — the pattern is `<machine>-<partition>.<ext>`
except `system_a` which uses `machine-image-<machine>.ext4`.

### Step 4 — Remap NSP config for perf tuning

Boot to adb (do NOT reboot yet — do this while still able to shell in from
the fastboot-flashed image, or after one reboot once adb is back):

```bash
adb shell
mv /etc/nsp_config_IVI.xml       /etc/nsp_config_IVI_bak.xml
mv /etc/nsp_config_HPASS_IVI.xml /etc/nsp_config_HPASS_IVI_bak.xml
cp /etc/nsp_config_ADAS.xml      /etc/nsp_config_IVI.xml
cp /etc/nsp_config_ADAS.xml      /etc/nsp_config_HPASS_IVI.xml
exit
```

### Step 5 — Reboot

```bash
fastboot reboot
```

Meta is now running the perf apps variant with ADAS-tuned NSP config on the
IVI paths.

## 2. Two Independent Dimensions: Which VM adb Bridges To, and What OS That VM Runs

**Do not conflate these.** Neither is fixed, and one does not determine the
other:

- **Which VM adb is currently bridged to (PVM vs GVM)** depends on how the
  adbd bridge on the host is configured *at that moment* — the same physical
  device can bridge to a different VM on a different connection.
  `TestApps/STSApp/ADBConnection.py:getIdentifier` shows this concretely: a
  single adb device-id can expose **multiple transport_ids** (multiple VMs
  behind one adb id), and the code explicitly picks a transport_id at
  connect time (`"Selecting transport id %s for HOS PVM"`) — there's no
  fixed "this device id = this VM" mapping.
- **What OS that VM happens to run (QNX / Yocto Linux / AOSP Android)** is a
  property of that specific meta's build, not of "GVM-ness". A GVM can be
  Yocto Linux on one platform/meta and AOSP Android on another.

**Consequence: never hardcode "if GVM then path X" or "if GVM then
Android".** Always probe and use whichever signal is actually present on
*this* connection, *this* time. `device_info.py`'s `get_device_info()` does
this correctly — it tries every known signal (both `ver_info.txt` mount
points, AOSP `getprop`, Yocto `/etc/os-release`) and reports whichever
returns non-empty, rather than branching on a VM-type label.

### ver_info.txt: two known mount points, try both

Confirmed from `CommonUtils_Python/Libraries/Chipsets/Chipset.py`
(`getVerInfoPath`) and `TestApps/STSApp/ADBConnection.py:getVerInfo` (which
literally tries a path list and falls through):

| Path | Seen used for |
|------|---------------|
| `/firmware/verinfo/ver_info.txt` | QNX, LV, AGL, and some Linux VMs (`ADBConnection.py`'s primary candidate) |
| `/vendor/firmware_mnt/verinfo/ver_info.txt` | `Chipset.py`'s mapping for `DeviceOSNames.LA`; also `ADBConnection.py`'s fallback candidate |

Neither path is exclusively "the PVM one" or "the GVM one" in practice —
`ADBConnection.py` itself tries `/vendor/firmware_mnt/...` FIRST, then falls
back to `/firmware/...`, which is the opposite order from `Chipset.py`'s
implied QNX-first mapping. **Just try both and use whichever responds.**

### Determining the OS on the other end of the current adb connection

Use `adb shell uname -a` and check the actual string, per the working
pattern in `TestApps/STSApp/ADBProfiler.py:getOSType`:

```python
osTypeRaw = adbShell("uname -a")
if 'GNU/Linux' in osTypeRaw:   return "LV"   # glibc-based Linux userspace
if 'Linux' in osTypeRaw:       return "LA"   # Android/busybox-style Linux
```

Then, only once you know it's some flavor of Linux, separately probe
whether an AOSP property system exists (`getprop` non-empty) — do not assume
either way from the uname result alone. This was empirically necessary: one
real SA8797P connection had `uname -a` report a `GNU/Linux`-tagged kernel
string yet `getprop` returned nothing and `/etc/os-release` identified it as
Yocto/OpenEmbedded (`ID_LIKE="automotive"`) — the kernel string alone was
not sufficient to conclude "this is/isn't Android".

### File Format

`ver_info.txt` is **JSON**, not flat key=value text (confirmed from
`TestApps/STSApp/Utilities.py:parseVerInfo`):

```json
{
  "Metabuild_Info": {
    "Meta_Build_ID": "SA8797P.HGY.5.1.7.0.c1-00194-STD.INT-1",
    "Product_Flavor": "nonsafe_ivi_pvm_lagvm"
  }
}
```

Some older test libraries `grep` it as if it were flat text (works
incidentally because the key names appear as JSON string literals on their
own lines) — prefer `json.loads()` for reliability.

## 3. Verifying an Apps-Only Flash Succeeded

**`ver_info.txt`'s `Meta_Build_ID` is NOT a valid signal for apps-only flash
verification.** It's written by the firmware/XBL layer at full-meta-flash
time and reflects "which meta was flashed overall" — flashing only the
`apps` domain via fastboot does not touch XBL, so `Meta_Build_ID` stays
unchanged even though apps genuinely changed. Checking it after an
apps-only flash will falsely suggest nothing happened.

### Probe for whichever version signal this specific apps build exposes

There is no fixed rule for which signal exists — it depends on what the
apps image's build system is, which varies by platform/meta, not by "GVM
vs PVM":

| Apps build system | Version file | Field | Confirmed usage |
|--------------------|---------------|-------|------------------|
| Yocto/OpenEmbedded (`apps_proc/poky/...`) | `/etc/os-release` | `VERSION_ID` (or `VERSION`) | Same technique as `QPM3QNXDumpParser/InvokeQPM3QnxRamdumpParser.py:92` (`cat /etc/os-release \| grep -i version_id`); real output captured on one SA8797P connection (below) |
| AOSP/Android apps build | `getprop` | `ro.build.fingerprint` / `ro.build.display.id` | `TestApps/STSApp/ADBProfiler.py`, various `.robot` files |

**Probe before trusting either**: run `adb shell getprop ro.build.fingerprint`
and `adb shell cat /etc/os-release` both; use whichever returns non-empty on
*this* connection. Do not assume based on whether you connected to a "GVM" —
that label alone doesn't determine which build system produced its apps
image.

One real capture (Yocto-flavored connection):

```
$ cat /etc/os-release
ID=auto
ID_LIKE="automotive"
NAME="auto"
VERSION="AU_LINUX_EMBEDDED_LY.AU.0.1.1.C1_TARGET_ALL.01.774.059-2-gf2f3929eb890"
VERSION_ID=au_linux_embedded_ly.au.0.1.1.c1_target_all.01.774.059-2-gf2f3929eb890
```

The `LY.AU.0.1.1` token matches the apps build root naming used to locate
perf images in §1 (`LY.AU.0.1.1-42200-gen5meta.1-1`) — this is the apps
partition's own version stamp, independent of `Meta_Build_ID`.

Also available: `/etc/version` (a bare date-stamp string, less specific) and
`/proc/version` (kernel build string, changes only if `boot_a` was reflashed
too — not useful for an apps/system-only flash).

### Verification pattern

Capture the value *before* flashing, flash, reboot, capture again, diff —
using whichever field family is non-empty on this connection:

```bash
BEFORE=$(adb shell cat /etc/os-release | grep VERSION_ID)
# ... flash apps images, reboot ...
adb wait-for-device
AFTER=$(adb shell cat /etc/os-release | grep VERSION_ID)
# BEFORE != AFTER confirms the apps partition actually changed
```

Or, on a connection where `getprop` is populated, use `ro.build.fingerprint`
the same way instead.

`device_info.py info` collects all signal families on every call —
`Meta_Build_ID`/`Product_Flavor` (firmware layer, do not use for apps-only
verification), `build_fingerprint`/`build_display_id` (present only if this
connection's userspace is AOSP-based), and `os_release_version_id`/
`os_release_pretty_name` (present only if this connection's userspace is
Yocto/OpenEmbedded-based). Run it once before and once after an apps-only
flash and diff whichever field family is non-empty — don't assume which one
in advance.
