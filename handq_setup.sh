#!/usr/bin/env bash
# Version: 4.1
# =============================================================================
# handq_setup.sh — Linux HandQ (sub-agent daemon) setup script
# =============================================================================
#
# Installs the `handq_linux` command — the Linux "sub HandQ" entry point.
# One Windows HandQ controls many Linux HandQ over SSH (the remote_handq tool);
# each Linux box runs handq_linux.py as a resident, setsid-detached
# FlowControllerV2 daemon with a local emergency console. There is no tmux,
# no systemd, no state bar.
#
# THE INSTALL ROOT (new in 4.1)
#   Everything that must not be shared between machines — the binary, the config
#   (which carries the API key), the file-IPC pipe, pushed skills, and the agent's
#   workspace — lives under ONE root, resolved from the first usable of:
#       /local/mnt/workspace/<user>@handq     machine-local, usually quota-free
#       /var/tmp/<user>@handq                 machine-local, survives reboot
#       $HOME/handq/<user>@<host>             last resort (see below)
#   The root is chmod 700 because the config holds a credential and the first two
#   candidates are multi-user visible.
#
#   Why not $HOME: in this deployment $HOME is cloud-synced across several
#   physical Linux hosts for the same user. A $HOME-based root means two machines
#   share one install directory and one state directory, which is what prevented
#   the same user from running a daemon on two machines at once. Only the
#   last-resort candidate lives under $HOME, and it carries a host segment for
#   exactly that reason. The invariant: under no candidate do two live daemons
#   ever share a root.
#
#   The resolved root is recorded in ~/.config/handq/hosts/<shorthost> as
#   `export HANDQ_ROOT=...`. That file is the single authority for "where is
#   HandQ on this host" — handq_linux.py reads $HANDQ_ROOT and the Windows probe
#   greps this file, so neither re-derives the candidate chain and they cannot
#   drift apart.
#
# Usage:
#   bash handq_setup.sh                        # Install + verify
#   bash handq_setup.sh --config <path>        # Use a custom config file
#   bash handq_setup.sh --root <path>          # Force a specific install root
#   bash handq_setup.sh --print-root           # Print the resolved root, exit
#   bash handq_setup.sh --test                 # Re-run verification only (no install)
#
# What this script does:
#   1. Resolves the install root and copies the package into it (a no-op when the
#      Windows auto-deploy already extracted straight into the root)
#   2. Verifies the handq_linux entry point and config under the root
#   3. Installs `handq_linux` plus the `handq` / `hi` aliases into ~/.local/bin,
#      and records HANDQ_ROOT in the per-host config the dispatcher sources
#   4. Adds ~/.local/bin to PATH in your shell profile, if needed
#
# After install:
#   handq_linux              # open the local console (starts the daemon)
#   handq                    # alias — same thing
#   hi                       # short alias — same thing
#   handq_linux "fix the build and run the tests"   # one-shot goal
#   handq_linux --status     # print the daemon's state.json
#   handq_linux --exit       # stop the daemon
#
# The Windows side resolves the launch path from the recorded HANDQ_ROOT, not from
# `command -v` — so a stale dispatcher left on PATH by an older setup cannot
# hijack it.
#
# Exit codes:
#   0 — Success     1 — Fatal error or failing self-test     2 — Bad arguments
# =============================================================================

set -euo pipefail

# Guard: this script must be executed, not sourced.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    echo "handq_setup.sh: ERROR — do not source this script." >&2
    echo "  Run:  bash handq_setup.sh [--config PATH]" >&2
    return 1
fi

# ── Colour helpers ────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_RESET="\033[0m" C_BOLD="\033[1m" C_GREEN="\033[0;32m"
    C_YELLOW="\033[0;33m" C_RED="\033[0;31m" C_CYAN="\033[0;36m"
else
    C_RESET="" C_BOLD="" C_GREEN="" C_YELLOW="" C_RED="" C_CYAN=""
fi

print_header() {
    echo ""
    printf "${C_BOLD}${C_CYAN}╔══════════════════════════════════════════════════╗${C_RESET}\n"
    printf "${C_BOLD}${C_CYAN}║          HandQ (Linux) Setup                     ║${C_RESET}\n"
    printf "${C_BOLD}${C_CYAN}╚══════════════════════════════════════════════════╝${C_RESET}\n"
    echo ""
}

print_step() {
    printf "\n${C_BOLD}──────────────────────────────────────────────────────────────${C_RESET}\n"
    printf "  ${C_BOLD}$1${C_RESET}\n"
    [ -n "${2:-}" ] && printf "  $2\n"
    printf "${C_BOLD}──────────────────────────────────────────────────────────────${C_RESET}\n"
}

ok()   { printf "  ${C_GREEN}✅  $*${C_RESET}\n"; }
warn() { printf "  ${C_YELLOW}⚠️   $*${C_RESET}\n"; }
err()  { printf "  ${C_RED}❌  $*${C_RESET}\n" >&2; }
info() { printf "  ${C_CYAN}ℹ️   $*${C_RESET}\n"; }

_finish() { exit "${1:-0}"; }
die() { err "$*"; _finish 1; }

# Short hostname (no domain suffix). Only used for the LAST-RESORT root
# candidate below, where the root has to live under the cloud-synced $HOME and
# therefore must carry a host segment. Mirrors handq_linux.py's
# `socket.gethostname().split(".")[0]` and the remote probe's `hostname -s`.
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
WHO="$(whoami)"

# ── HandQ root ────────────────────────────────────────────────────────────────
# The root holds EVERYTHING that must not be shared between machines: the
# binary, the config (which carries the API key), the file-IPC pipe, pushed
# skills, and the agent's workspace. It deliberately does NOT live under $HOME:
# in this deployment $HOME is cloud-synced across several physical Linux hosts
# for the same user, so a $HOME-based root means two machines share one install
# and one state dir — which is exactly what stops the same user running a daemon
# on two machines at once.
#
# Candidates, in order. Each is probed for real (create + write + delete + free
# space), not merely tested with `mkdir -p`: on hosts with a per-user home quota
# the directory usually already exists, so `mkdir -p` is a no-op that proves
# nothing about whether a file can actually be created. Same reasoning as
# ssh_tool._resolve_job_base_dir.
#
#   1. /local/mnt/workspace/<user>@handq   machine-local, usually quota-free
#   2. /var/tmp/<user>@handq               machine-local, survives reboot.
#                                          NOT /tmp — that is frequently tmpfs:
#                                          small, and wiped on reboot, which
#                                          would silently delete the install.
#   3. $HOME/handq/<user>@<host>           last resort. $HOME is synced, so this
#                                          candidate MUST carry the host segment
#                                          or two machines collide again. It
#                                          means a per-host copy of the install,
#                                          which is only acceptable because it
#                                          happens exclusively on hosts that have
#                                          neither machine-local option.
#
# The invariant that matters: under NO candidate do two live daemons ever share
# one root.
#
# DUPLICATION NOTE: remote_handq_tool._PROBE carries the same chain, because on a
# first-ever install nothing is recorded yet and the Windows side must choose a
# deploy target before this script exists on the host. That duplication is
# bounded — the moment this script runs it WRITES the resolved root into
# ~/.config/handq/hosts/<shorthost>, and from then on both sides read the
# recorded value instead of re-deriving it. A divergence can therefore only
# affect the very first install, and it self-heals on the next probe. Keep the
# two chains in sync anyway.
HANDQ_ROOT_MIN_MB=600          # a Nuitka standalone dist is a few hundred MB
HANDQ_ROOT=""                  # filled in by _resolve_handq_root
HANDQ_ROOT_REJECTS=()          # "<candidate>: <why>" for every candidate refused

# Probe one candidate. Echoes the rejection reason and returns non-zero when the
# candidate is unusable; silent + zero when it is good.
_probe_root_candidate() {
    local cand="$1"
    local parent; parent="$(dirname "$cand")"
    [[ -d "$parent" ]] || { echo "parent directory ${parent} does not exist"; return 1; }
    if [[ -e "$cand" ]]; then
        # /local/mnt/workspace and /var/tmp are world-writable (and /var/tmp is
        # sticky), and the candidate name is predictable — so an existing entry
        # may belong to somebody else, or be a symlink planted by them. Refuse
        # rather than write into it.
        [[ -L "$cand" ]] && { echo "exists but is a symlink"; return 1; }
        [[ -d "$cand" ]] || { echo "exists but is not a directory"; return 1; }
        [[ -O "$cand" ]] || { echo "exists but is owned by another user"; return 1; }
    else
        mkdir -p "$cand" 2>/dev/null || { echo "cannot be created"; return 1; }
    fi
    # The config stored here carries the LLM API key. Under $HOME that was
    # covered by the home directory's own permissions; these candidates are
    # multi-user visible, so the restriction has to be explicit.
    chmod 700 "$cand" 2>/dev/null || true
    local probe="${cand}/.handq_probe.$$"
    ( : > "$probe" ) 2>/dev/null || { echo "not writable"; return 1; }
    rm -f "$probe" 2>/dev/null || true
    local avail_mb
    avail_mb="$(df -Pm "$cand" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [[ -n "$avail_mb" ]] && [[ "$avail_mb" =~ ^[0-9]+$ ]] \
       && (( avail_mb < HANDQ_ROOT_MIN_MB )); then
        echo "only ${avail_mb}MB free, need ${HANDQ_ROOT_MIN_MB}MB"
        return 1
    fi
    return 0
}

# Pick the first usable candidate. Sets HANDQ_ROOT; returns non-zero if every
# candidate failed (the caller reports the collected reasons and dies).
_resolve_handq_root() {
    local candidates=(
        "/local/mnt/workspace/${WHO}@handq"
        "/var/tmp/${WHO}@handq"
        "${HOME}/handq/${WHO}@${HOSTNAME_SHORT}"
    )
    local cand reason
    for cand in "${candidates[@]}"; do
        if reason="$(_probe_root_candidate "$cand")"; then
            HANDQ_ROOT="$cand"
            return 0
        fi
        HANDQ_ROOT_REJECTS+=("${cand}: ${reason}")
    done
    return 1
}

# ── Resolve script directory ──────────────────────────────────────────────────
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
# PKG_DIR is where the *package* was unpacked (this script's own directory).
# HANDQ_ROOT is where the *install* lives. They are the same directory on the
# Windows auto-deploy path (which extracts straight into the root and then runs
# <root>/handq_setup.sh), and different on a manual install, where the user
# extracted the tarball somewhere convenient — often under the synced $HOME.
# stage_into_root() copies the payload across so both paths converge on the root.
PKG_DIR="${SCRIPT_DIR}"
# Install target for the dispatcher. Deliberately ONLY ~/.local/bin:
#   * /usr/local/bin is itself a multi-user shared path, which is the thing this
#     layout exists to avoid;
#   * a dispatcher sudo-installed there is root-owned, so the next deploy (which
#     runs over SSH without sudo) cannot rewrite it and dies on EACCES under
#     `set -euo pipefail`, even when ~/.local/bin succeeded;
#   * ~/.local/bin is synced, so it already exists on every host, and the
#     dispatcher written into it is host-agnostic (it routes at runtime through
#     the per-host config), so sharing it across machines is correct.
# The Windows probe checks ~/.local/bin explicitly, precisely because it is
# usually absent from a non-interactive SSH PATH.
INSTALL_BIN_DIR="${HOME}/.local/bin"
# Kept only so install_command can WARN about a stale dispatcher left here by an
# older setup. Nothing is written to it any more.
LEGACY_SYSTEM_BIN_DIR="/usr/local/bin"
HOST_CONF_DIR="${HOME}/.config/handq/hosts"   # per-host exec config (one file per hostname)
COMMAND_NAME="handq_linux"
ALIASES=("handq" "hi")                        # convenience symlinks → handq_linux
REQUIRED_KEYS=("version" "llm" "session" "interaction_switches")
# llm model pool. New schema uses agent_models / available_models (checked
# subsets of the pool); a flat `models` list is still accepted for legacy
# configs. validate_config requires at least one, in this preference order —
# matches role_resolver.resolve_models_and_helper.
LLM_POOL_KEYS=("agent_models" "available_models" "models")

# =============================================================================
# Argument parsing
# =============================================================================
CONFIG_PATH=""
ROOT_OVERRIDE=""
TEST_MODE=false
PRINT_ROOT_MODE=false

usage() {
    cat <<EOF
Usage: $(basename "${BASH_SOURCE[0]}") [OPTIONS]

Options:
  --config <path>   Path to handq_config.yaml (default: <root>/handq_config.yaml)
  --root <path>     Install root override. Skips candidate-chain resolution.
  --print-root      Print the resolved install root and exit (no install).
  --test            Re-run verification only, skip install
  -h, --help        Show this help message

The install root holds the binary, config, file-IPC pipe, pushed skills and the
agent workspace. It is resolved from the first usable of:
  /local/mnt/workspace/<user>@handq
  /var/tmp/<user>@handq
  \$HOME/handq/<user>@<host>            (last resort; \$HOME may be cloud-synced)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            [[ -z "${2:-}" ]] && { err "--config requires a path"; usage; _finish 2; }
            CONFIG_PATH="$2"; shift 2 ;;
        --root)
            [[ -z "${2:-}" ]] && { err "--root requires a path"; usage; _finish 2; }
            ROOT_OVERRIDE="$2"; shift 2 ;;
        --print-root) PRINT_ROOT_MODE=true; shift ;;
        --test) TEST_MODE=true; shift ;;
        -h|--help) usage; _finish 0 ;;
        *) err "Unknown option: $1"; usage; _finish 2 ;;
    esac
done

if [[ -n "$CONFIG_PATH" ]]; then
    CONFIG_PATH="${CONFIG_PATH/#\~/$HOME}"
    CONFIG_PATH="$(realpath -m "$CONFIG_PATH")"
fi
# CONFIG_PATH is deliberately left empty when --config was omitted: it defaults
# to <root>/handq_config.yaml, and the root is not known until main() resolves it.

# ── Root resolution entry point ───────────────────────────────────────────────
# Honours --root, then a root already recorded for this host (so re-running setup
# never silently relocates a working install), then the candidate chain.
establish_root() {
    if [[ -n "$ROOT_OVERRIDE" ]]; then
        HANDQ_ROOT="${ROOT_OVERRIDE/#\~/$HOME}"
        HANDQ_ROOT="$(realpath -m "$HANDQ_ROOT")"
        local reason
        if ! reason="$(_probe_root_candidate "$HANDQ_ROOT")"; then
            die "--root ${HANDQ_ROOT} is not usable: ${reason}"
        fi
        return 0
    fi
    # An already-recorded root wins over the chain. Re-resolving on every run
    # would move the install the moment /local/mnt/workspace briefly filled up,
    # abandoning a perfectly good root (and its state) behind it.
    local recorded=""
    local host_conf="${HOST_CONF_DIR}/${HOSTNAME_SHORT}"
    if [[ -f "$host_conf" ]]; then
        recorded="$(sed -n 's/^export HANDQ_ROOT="\(.*\)"$/\1/p' "$host_conf" | head -1)"
    fi
    if [[ -n "$recorded" ]]; then
        local reason
        if reason="$(_probe_root_candidate "$recorded")"; then
            HANDQ_ROOT="$recorded"
            info "Reusing the root already recorded for ${HOSTNAME_SHORT}: ${HANDQ_ROOT}"
            return 0
        fi
        warn "Recorded root ${recorded} is no longer usable (${reason}) — re-resolving"
    fi
    if ! _resolve_handq_root; then
        err "No usable HandQ root found. Candidates tried:"
        local r
        for r in "${HANDQ_ROOT_REJECTS[@]}"; do
            printf '      %s\n' "$r" >&2
        done
        die "Pass --root <path> to choose one explicitly."
    fi
    return 0
}

# ── stage_into_root — make the root the install, not the unpack dir ───────────
stage_into_root() {
    # Manual install: the user extracted the package somewhere convenient (often
    # under the synced $HOME) and ran this script from there. The package is not
    # the install; the root is. Copy the payload across so a manual install lands
    # in exactly the same place the Windows auto-deploy uses.
    #
    # No-op on the auto-deploy path: remote_handq_tool._DEPLOY_SCRIPT extracts
    # straight into the root and invokes us as <root>/handq_setup.sh, so
    # PKG_DIR == HANDQ_ROOT there.
    [[ "$PKG_DIR" == "$HANDQ_ROOT" ]] && return 0

    print_step "Staging package into the root" "${PKG_DIR} → ${HANDQ_ROOT}"
    local copied=false
    if [[ -d "${PKG_DIR}/handq_linux.dist" ]]; then
        # Stage next to the live dist and swap, so an interrupted copy never
        # leaves a half-written handq_linux.dist behind.
        rm -rf "${HANDQ_ROOT}/.staging_dist"
        cp -r "${PKG_DIR}/handq_linux.dist" "${HANDQ_ROOT}/.staging_dist" \
            || die "Failed to copy handq_linux.dist into ${HANDQ_ROOT}"
        rm -rf "${HANDQ_ROOT}/handq_linux.dist"
        mv "${HANDQ_ROOT}/.staging_dist" "${HANDQ_ROOT}/handq_linux.dist"
        ok "Copied handq_linux.dist"
        copied=true
    fi
    for extra in handq_linux.bin handq_linux.py handq_setup.sh; do
        if [[ -e "${PKG_DIR}/${extra}" ]]; then
            cp -r "${PKG_DIR}/${extra}" "${HANDQ_ROOT}/${extra}" 2>/dev/null \
                && { ok "Copied ${extra}"; copied=true; } \
                || warn "Could not copy ${extra}"
        fi
    done
    # src/ matters for a source-checkout install (the daemon imports src.*).
    if [[ -d "${PKG_DIR}/src" && ! -d "${HANDQ_ROOT}/src" ]]; then
        cp -r "${PKG_DIR}/src" "${HANDQ_ROOT}/src" 2>/dev/null \
            && ok "Copied src/" || warn "Could not copy src/"
    fi
    # The config carries the API key. NEVER overwrite one already in the root —
    # it may have been fixed up by hand or pushed by the controller since the
    # package was built.
    if [[ -f "${PKG_DIR}/handq_config.yaml" ]]; then
        if [[ -f "${HANDQ_ROOT}/handq_config.yaml" ]]; then
            info "Keeping the existing ${HANDQ_ROOT}/handq_config.yaml (not overwritten)"
        else
            cp "${PKG_DIR}/handq_config.yaml" "${HANDQ_ROOT}/handq_config.yaml" \
                && ok "Copied handq_config.yaml"
        fi
    fi
    [[ "$copied" == false ]] && warn "Nothing was staged from ${PKG_DIR}"
    return 0
}

# =============================================================================
# check_required_files — locate the handq_linux entry point + config
# =============================================================================
check_required_files() {
    print_step "Checking required files" "root: ${HANDQ_ROOT}"
    # Resolved against the ROOT, not the package dir: stage_into_root has already
    # copied the payload in, and the root is what the dispatcher will point at.
    local py_main="${HANDQ_ROOT}/handq_linux.py"
    local bin_main="${HANDQ_ROOT}/handq_linux.bin"
    local standalone_bin="${HANDQ_ROOT}/handq_linux.dist/handq_linux.bin"
    local all_ok=true

    MAIN_EXEC="" USE_PYTHON=false
    # Standalone (Nuitka) layout: binary inside handq_linux.dist/
    if [[ -f "$standalone_bin" ]]; then
        [[ ! -x "$standalone_bin" ]] && { chmod +x "$standalone_bin"; info "Set executable bit on ${standalone_bin}"; }
        MAIN_EXEC="$standalone_bin"; ok "Found standalone binary: ${standalone_bin}"
    fi
    # Onefile / top-level binary layout
    if [[ -z "$MAIN_EXEC" && -f "$bin_main" ]]; then
        [[ ! -x "$bin_main" ]] && { chmod +x "$bin_main"; info "Set executable bit on ${bin_main}"; }
        MAIN_EXEC="$bin_main"; ok "Found binary: ${bin_main}"
    fi
    # Source layout: handq_linux.py next to src/
    if [[ -f "$py_main" ]]; then
        if [[ -z "$MAIN_EXEC" ]]; then
            MAIN_EXEC="$py_main"; USE_PYTHON=true
            ok "Found Python entry-point: ${py_main}"
            if [[ ! -d "${HANDQ_ROOT}/src" ]]; then
                warn "src/ not found next to handq_linux.py — the daemon imports src.* and will fail to start."
            fi
        else
            ok "Found Python entry-point (backup): ${py_main}"
        fi
    fi
    [[ -z "$MAIN_EXEC" ]] && { err "No handq_linux entry point found in ${HANDQ_ROOT} (looked for handq_linux.dist/handq_linux.bin, handq_linux.bin, handq_linux.py)"; all_ok=false; }

    if [[ -f "$CONFIG_PATH" ]]; then
        ok "Config file found: ${CONFIG_PATH}"
    else
        err "Config file not found: ${CONFIG_PATH}"; all_ok=false
    fi

    [[ "$all_ok" == false ]] && return 1 || return 0
}

# =============================================================================
# check_dependencies
# =============================================================================
check_dependencies() {
    print_step "Checking dependencies"
    local all_ok=true

    if [[ "$USE_PYTHON" == true ]]; then
        local python_exe
        python_exe="$(_resolve_python_exe)"
        if [[ -n "$python_exe" ]]; then
            ok "Python: ${python_exe}"
        else
            err "Python 3 not found (required to run handq_linux.py from source)"; all_ok=false
        fi
    else
        ok "Standalone binary — no external Python required"
    fi

    [[ "$all_ok" == false ]] && return 1 || return 0
}

# =============================================================================
# validate_config
# =============================================================================
validate_config() {
    print_step "Validating config file" "${CONFIG_PATH}"
    local all_ok=true

    # ── Real YAML parse via PyYAML (best-effort; skipped if unavailable) ──
    if command -v python3 &>/dev/null; then
        local yaml_out
        yaml_out="$(CFG="$CONFIG_PATH" python3 -c '
import os, sys
try:
    import yaml
except ImportError:
    print("SKIP_NO_YAML")
    sys.exit(0)
try:
    with open(os.environ["CFG"], "r", encoding="utf-8") as f:
        yaml.safe_load(f)
    print("OK")
except yaml.YAMLError as e:
    print("YAML_ERROR")
    print(str(e))
' 2>&1)"
        case "$yaml_out" in
            OK*)            ok "YAML syntax valid (parsed by PyYAML)" ;;
            SKIP_NO_YAML*)  info "PyYAML not installed — skipped deep YAML check" ;;
            YAML_ERROR*)
                err "Invalid YAML in ${CONFIG_PATH}:"
                printf '%s\n' "$yaml_out" | tail -n +2 | sed 's/^/      /'
                all_ok=false ;;
            *)              warn "YAML validation could not run: $yaml_out" ;;
        esac
    fi

    for key in "${REQUIRED_KEYS[@]}"; do
        if grep -qE "^${key}:" "$CONFIG_PATH"; then
            ok "Key present: '${key}'"
        else
            err "Missing key: '${key}'"; all_ok=false
        fi
    done

    # ── llm model pool ────────────────────────────────────────────────────
    # Accept the first present of agent_models / available_models / models.
    local pool_key=""
    for key in "${LLM_POOL_KEYS[@]}"; do
        if grep -qE "^[[:space:]]+${key}:" "$CONFIG_PATH"; then
            pool_key="$key"; break
        fi
    done
    if [[ -n "$pool_key" ]]; then
        ok "llm model pool present (llm.${pool_key})"
        local model_val
        model_val="$(awk -v key="$pool_key" '
            $0 ~ "^[[:space:]]+" key ":" {found=1; next}
            found && /^[[:space:]]*#/ {next}
            found && /^[[:space:]]+-/ {gsub(/^[[:space:]]+-[[:space:]]*/,""); gsub(/[[:space:]]*$/,""); print; exit}
            found && /^[[:space:]]+[^-]/ {exit}
        ' "$CONFIG_PATH" | tr -d '"'"'")"
        [[ -z "$model_val" || "$model_val" == "null" ]] \
            && warn "llm.${pool_key} appears empty" \
            || ok "llm.${pool_key}[0] = ${model_val}"
    else
        err "Missing llm model pool (need one of: ${LLM_POOL_KEYS[*]})"; all_ok=false
    fi

    # Support both API_KEY (direct value) and api_key_env (env var name) formats.
    local api_key_env_val
    api_key_env_val="$(grep -E "^[[:space:]]+api_key_env:" "$CONFIG_PATH" | head -1 | sed 's/.*api_key_env:[[:space:]]*//' | sed 's/[[:space:]]*#.*//' | tr -d '"'"'" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ -n "$api_key_env_val" && "$api_key_env_val" != "null" ]]; then
        if [[ "$api_key_env_val" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
            local api_key_resolved="${!api_key_env_val:-}"
            [[ -n "$api_key_resolved" ]] && ok "\$${api_key_env_val} is set" || warn "\$${api_key_env_val} is NOT set — set it before running handq_linux"
        else
            ok "llm.api_key_env contains a direct key value"
        fi
    else
        local api_key_direct
        api_key_direct="$(grep -E "^[[:space:]]+API_KEY:" "$CONFIG_PATH" | head -1 | sed 's/.*API_KEY:[[:space:]]*//' | sed 's/[[:space:]]*#.*//' | tr -d '"'"'" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        if [[ -n "$api_key_direct" && "$api_key_direct" != "null" ]]; then
            ok "llm.API_KEY is set"
        else
            warn "llm.API_KEY is empty — fill in your API key before running handq_linux"
        fi
    fi

    [[ "$all_ok" == false ]] && return 1 || return 0
}

# =============================================================================
# _resolve_python_exe — find the best python3 executable
# =============================================================================
_resolve_python_exe() {
    # 1. venv_path in config
    local _venv_path
    _venv_path="$(grep -E '^\s*venv_path\s*:' "$CONFIG_PATH" 2>/dev/null \
        | sed 's/.*venv_path[[:space:]]*:[[:space:]]*//' \
        | sed 's/[[:space:]]*#.*//' \
        | tr -d '"'"'" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ -n "$_venv_path" && "$_venv_path" != "null" && "$_venv_path" != "~" ]]; then
        local _venv_python="${_venv_path}/bin/python3"
        [[ -x "$_venv_python" ]] && echo "$_venv_python" && return
    fi
    # 2. a .venv/venv co-located with the install root (matches the remote probe)
    for c in "${HANDQ_ROOT}/.venv/bin/python3" "${HANDQ_ROOT}/.venv/bin/python" \
             "${HANDQ_ROOT}/venv/bin/python3" "${HANDQ_ROOT}/venv/bin/python"; do
        [[ -x "$c" ]] && echo "$c" && return
    done
    # 3. active VIRTUAL_ENV
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        local _venv_python="${VIRTUAL_ENV}/bin/python3"
        [[ -x "$_venv_python" ]] && echo "$_venv_python" && return
    fi
    # 4. PATH
    command -v python3 2>/dev/null && return
    echo ""
}

# =============================================================================
# install_command — write the handq_linux dispatcher + aliases
# =============================================================================
install_command() {
    print_step "Installing '${COMMAND_NAME}' command"

    # Single target: ~/.local/bin. See INSTALL_BIN_DIR's comment for why
    # /usr/local/bin is deliberately no longer written.
    local target_dir="$INSTALL_BIN_DIR"
    mkdir -p "$target_dir" 2>/dev/null || true
    [[ -d "$target_dir" && -w "$target_dir" ]] \
        || { err "Install directory not writable: ${target_dir}"; return 1; }

    # NO stale-wrapper removal. It used to delete ${COMMAND_NAME} and the aliases
    # from every candidate dir other than the chosen one — which, with a
    # cloud-synced $HOME, meant a host that installed into /usr/local/bin wiped
    # ~/.local/bin/handq_linux out from under every OTHER host that depended on
    # it (and that is the exact path the Windows probe checks explicitly). The
    # dispatcher is host-agnostic and rewritten in place below, so removal buys
    # nothing and cost us cross-host breakage on every deploy.
    #
    # A leftover dispatcher in /usr/local/bin cannot mislead the Windows control
    # path any more (remote_handq_tool._discover now takes its launch from the
    # recorded root rather than from `command -v`), but it WILL shadow the real
    # one for a human typing `handq_linux`. Warn, with the exact command, since
    # we cannot remove a root-owned file ourselves.
    local legacy_cmd="${LEGACY_SYSTEM_BIN_DIR}/${COMMAND_NAME}"
    if [[ -e "$legacy_cmd" ]] && ! grep -q '_HANDQ_CONF' "$legacy_cmd" 2>/dev/null; then
        warn "A pre-4.1 ${COMMAND_NAME} is still installed at ${legacy_cmd} and will"
        warn "  shadow this one on your PATH. Windows control is unaffected, but for"
        warn "  interactive use remove it:  sudo rm -f ${legacy_cmd}"
    fi

    local cmd_path="${target_dir}/${COMMAND_NAME}"
    local exec_line

    if [[ "$USE_PYTHON" == true ]]; then
        local python_exe
        python_exe="$(_resolve_python_exe)"
        [[ -z "$python_exe" ]] && { err "Python 3 not found"; return 1; }
        exec_line="\"${python_exe}\" \"${MAIN_EXEC}\" --config \"${CONFIG_PATH}\""
    else
        exec_line="\"${MAIN_EXEC}\" --config \"${CONFIG_PATH}\""
    fi

    # ── 1. Write per-host exec config ────────────────────────────────────────
    # Each host writes ONLY its own file; other hosts' files are untouched. This
    # file is the single authority for "where is HandQ on this host": it records
    # the resolved root, and both handq_linux.py (via $HANDQ_ROOT, exported here)
    # and the Windows probe read it instead of re-deriving the candidate chain.
    # That is what keeps three implementations from drifting apart.
    mkdir -p "$HOST_CONF_DIR"
    local host_conf="${HOST_CONF_DIR}/${HOSTNAME_SHORT}"
    {
        printf 'export HANDQ_ROOT=%s\n' "\"${HANDQ_ROOT}\""
        printf 'exec %s "$@"\n' "$exec_line"
    } > "$host_conf"
    chmod +x "$host_conf"
    ok "Host config  : ${host_conf}"
    ok "HANDQ_ROOT   : ${HANDQ_ROOT}"

    # ── 2. Write the static hostname dispatcher ──────────────────────────────
    # Identical on every host — routes to the correct root + binary at runtime via
    # the per-host config above.
    cat > "$cmd_path" <<'DISPATCHER'
#!/usr/bin/env bash
# handq_linux — Linux HandQ sub-agent daemon launcher / console.
# Hostname dispatcher — do NOT edit manually; managed by handq_setup.sh.
_HANDQ_HOST="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
_HANDQ_CONF="${HOME}/.config/handq/hosts/${_HANDQ_HOST}"
if [[ -f "$_HANDQ_CONF" ]]; then
    . "$_HANDQ_CONF"
else
    echo "HandQ: not configured for host ${_HANDQ_HOST}." >&2
    echo "  Run: bash handq_setup.sh --config <path>" >&2
    exit 1
fi
DISPATCHER

    chmod +x "$cmd_path"
    ok "Installed dispatcher: ${cmd_path}"

    # ── 3. Install convenience aliases as symlinks → handq_linux ─────────────
    for alias_name in "${ALIASES[@]}"; do
        local alias_path="${target_dir}/${alias_name}"
        ln -sf "$cmd_path" "$alias_path" 2>/dev/null \
            && ok "Installed alias: ${alias_path} → ${COMMAND_NAME}" \
            || warn "Could not create alias at ${alias_path}"
    done

    # ── 4. Ensure the install dir is on PATH ─────────────────────────────────
    # target_dir is now a constant (~/.local/bin) on every host, which is what
    # makes the grep guard below actually work: it used to be keyed on a
    # machine-dependent target, so a shared (cloud-synced) .bashrc accumulated one
    # export block per distinct target dir, on every deploy.
    if echo ":${PATH}:" | grep -q ":${target_dir}:"; then
        ok "${target_dir} is already on PATH"
    else
        local profile_file="" export_line="" source_cmd=""
        local shell_name
        shell_name="$(basename "${SHELL:-bash}")"
        case "$shell_name" in
            zsh)
                profile_file="${ZDOTDIR:-$HOME}/.zshrc"
                export_line="export PATH=\"${target_dir}:\$PATH\""
                source_cmd="source ${profile_file}" ;;
            bash)
                profile_file="${HOME}/.bashrc"
                export_line="export PATH=\"${target_dir}:\$PATH\""
                source_cmd="source ${profile_file}" ;;
            tcsh|csh)
                profile_file="${HOME}/.tcshrc"
                export_line="setenv PATH \"${target_dir}:\$PATH\""
                source_cmd="source ${profile_file}" ;;
            fish)
                profile_file="${HOME}/.config/fish/config.fish"
                export_line="set -x PATH \"${target_dir}\" \$PATH"
                source_cmd="source ${profile_file}" ;;
            *)
                profile_file="${HOME}/.profile"
                export_line="export PATH=\"${target_dir}:\$PATH\""
                source_cmd=". ${profile_file}" ;;
        esac
        local marker="# Added by handq_setup.sh"

        if [[ -f "$profile_file" ]] && grep -qF "$target_dir" "$profile_file" 2>/dev/null; then
            ok "${target_dir} already in ${profile_file}"
        else
            printf "\n%s\n%s\n" "$marker" "$export_line" >> "$profile_file"
            ok "Added PATH entry to ${profile_file}"
        fi

        info "To apply in this session: ${source_cmd}"
    fi

    INSTALLED_CMD_PATH="$cmd_path"
    INSTALLED_HOST_CONF_PATH="$host_conf"
    _EXEC_LINE="$exec_line"
    return 0
}

# =============================================================================
# Test suite
# =============================================================================
_T_PASS=0 _T_FAIL=0 _T_SKIP=0
_t_pass() { _T_PASS=$((_T_PASS+1)); ok  "PASS  $*"; }
_t_fail() { _T_FAIL=$((_T_FAIL+1)); err "FAIL  $*"; }
_t_skip() { _T_SKIP=$((_T_SKIP+1)); warn "SKIP  $*"; }

assert_eq()         { [[ "$2" == "$3" ]] && _t_pass "$1" || _t_fail "$1  (got: $(printf '%q' "$2")  want: $(printf '%q' "$3"))"; }
assert_file_exists(){ [[ -f "$2" ]]      && _t_pass "$1" || _t_fail "$1  (file not found: $2)"; }

_resolve_exec_line() {
    local py_main="${HANDQ_ROOT}/handq_linux.py"
    local bin_main="${HANDQ_ROOT}/handq_linux.bin"
    local standalone_bin="${HANDQ_ROOT}/handq_linux.dist/handq_linux.bin"
    if [[ -f "$standalone_bin" && -x "$standalone_bin" ]]; then
        echo "\"${standalone_bin}\" --config \"${CONFIG_PATH}\""
    elif [[ -f "$bin_main" && -x "$bin_main" ]]; then
        echo "\"${bin_main}\" --config \"${CONFIG_PATH}\""
    elif [[ -f "$py_main" ]]; then
        local python_exe; python_exe="$(_resolve_python_exe)"
        [[ -z "$python_exe" ]] && { echo ""; return; }
        echo "\"${python_exe}\" \"${py_main}\" --config \"${CONFIG_PATH}\""
    else
        echo ""
    fi
}

# ── Suite A: static install checks ─────────────────────────────────────────────
_suite_a_static() {
    print_step "Suite A — Static checks"

    local wrapper
    if [[ -n "${INSTALLED_CMD_PATH:-}" ]]; then
        wrapper="$INSTALLED_CMD_PATH"
    else
        # --test with no install in this run: judge the canonical location, not
        # `command -v`, which can resolve to a legacy /usr/local/bin dispatcher
        # that this script no longer manages.
        wrapper="${INSTALL_BIN_DIR}/${COMMAND_NAME}"
    fi

    assert_file_exists "A1: dispatcher file exists" "$wrapper"
    [[ -x "$wrapper" ]] && _t_pass "A1: dispatcher is executable" || _t_fail "A1: dispatcher not executable"

    # A2/A3: config path + main executable live in the per-host config.
    local host_conf="${INSTALLED_HOST_CONF_PATH:-${HOST_CONF_DIR}/${HOSTNAME_SHORT}}"
    assert_file_exists "A2a: per-host config exists" "$host_conf"

    local wrapper_config
    wrapper_config="$(grep -oP '(?<=--config ")[^"]+' "$host_conf" 2>/dev/null || true)"
    if [[ -n "$wrapper_config" && -f "$wrapper_config" ]]; then
        _t_pass "A2: host config references valid config path (${wrapper_config##*/})"
    elif [[ -n "$wrapper_config" ]]; then
        _t_fail "A2: host config config file not found: ${wrapper_config}"
    else
        _t_fail "A2: host config missing --config argument"
    fi

    local py_main="${HANDQ_ROOT}/handq_linux.py"
    local bin_main="${HANDQ_ROOT}/handq_linux.bin"
    local standalone_bin="${HANDQ_ROOT}/handq_linux.dist/handq_linux.bin"
    { grep -qF "$py_main" "$host_conf" 2>/dev/null || grep -qF "$bin_main" "$host_conf" 2>/dev/null \
      || grep -qF "$standalone_bin" "$host_conf" 2>/dev/null; } \
        && _t_pass "A3: host config references the handq_linux entry point" \
        || _t_fail "A3: host config missing the handq_linux entry point"

    assert_file_exists "A4: config file exists" "$CONFIG_PATH"

    # A5: aliases resolve to the dispatcher. Judged against INSTALL_BIN_DIR
    # directly rather than dirname($wrapper), so this cannot silently mis-target.
    for alias_name in "${ALIASES[@]}"; do
        local alias_path="${INSTALL_BIN_DIR}/${alias_name}"
        if [[ -L "$alias_path" || -f "$alias_path" ]]; then
            _t_pass "A5: alias '${alias_name}' installed"
        else
            _t_fail "A5: alias '${alias_name}' missing"
        fi
    done

    # A6: dispatcher must be a clean launcher — no PS1/PROMPT_COMMAND/tmux.
    # Guarded on readability first: grep on an unreadable/missing path returns
    # non-zero, which the old `if grep …; then FAIL else PASS` shape reported as a
    # PASS — a vacuous pass that hid exactly the case worth catching.
    if [[ ! -r "$wrapper" ]]; then
        _t_fail "A6: dispatcher not readable, cannot inspect: ${wrapper}"
    elif grep -qE "PROMPT_COMMAND|PS1|prompt-state|tmux" "$wrapper"; then
        _t_fail "A6: dispatcher contains legacy shell/tmux integration"
    else
        _t_pass "A6: dispatcher is a clean launcher (no shell/tmux integration)"
    fi

    # A7: shell rc files must NOT source handq (legacy auto-attach).
    local _found_legacy=false
    for _rc in "${HOME}/.bashrc" "${HOME}/.tcshrc" "${HOME}/.cshrc.local" "${HOME}/.zshrc"; do
        if [[ -f "$_rc" ]] && grep -qE "source.*handq|\..*handq" "$_rc" 2>/dev/null; then
            _t_fail "A7: legacy handq source found in ${_rc} — remove it to prevent auto-attach on shell start"
            _found_legacy=true
        fi
    done
    [[ "$_found_legacy" == false ]] && _t_pass "A7: no legacy handq source in shell rc files"
}

# ── Suite B: console-client smoke ──────────────────────────────────────────────
# handq_linux has no --prompt-state / tmux state machine (the old Suites B/C);
# instead we smoke-test that the resolved entry point actually launches.
_suite_b_smoke() {
    print_step "Suite B — Console-client smoke"

    local exec_line; exec_line="$(_resolve_exec_line)"
    [[ -z "$exec_line" ]] && { _t_skip "B: all tests (handq_linux entry not found / no python)"; return; }

    # B1: --help launches and prints usage — proves the binary/script is intact
    # (no missing shared objects, argparse loads, console path imports cleanly).
    local help_out help_rc
    help_out="$(eval "$exec_line --help" 2>&1)"; help_rc=$?
    if [[ $help_rc -eq 0 && "$help_out" == *handq_linux* ]]; then
        _t_pass "B1: --help launches and prints usage"
    else
        _t_fail "B1: --help failed (rc=${help_rc})"
    fi

    # B2: --status runs without crashing. With no daemon up it reports
    # "daemon not running" (rc 1); with one up it prints state.json (rc 0).
    # Either is fine — we only guard against a crash (segfault / missing .so).
    local st_out st_rc
    st_out="$(eval "$exec_line --status" 2>&1)"; st_rc=$?
    if [[ $st_rc -eq 0 || $st_rc -eq 1 ]]; then
        _t_pass "B2: --status runs cleanly (rc=${st_rc})"
    else
        _t_fail "B2: --status crashed (rc=${st_rc}): ${st_out}"
    fi
}

# ── Suite R: install-root invariants ──────────────────────────────────────────
# The whole point of the root is that two machines sharing one cloud-synced $HOME
# do not share an install or a state dir. These assertions cover that directly —
# without them the relocation could silently half-apply and nothing would notice.
_suite_r_root() {
    print_step "Suite R — Install root"

    if [[ -z "$HANDQ_ROOT" ]]; then
        _t_fail "R1: HANDQ_ROOT is empty"
        return
    fi
    [[ -d "$HANDQ_ROOT" ]] \
        && _t_pass "R1: root exists (${HANDQ_ROOT})" \
        || _t_fail "R1: root does not exist: ${HANDQ_ROOT}"

    # R2: the config carries the API key and these roots are multi-user visible.
    local mode
    mode="$(stat -c '%a' "$HANDQ_ROOT" 2>/dev/null || stat -f '%Lp' "$HANDQ_ROOT" 2>/dev/null || echo "")"
    if [[ "$mode" == "700" ]]; then
        _t_pass "R2: root permissions are 700"
    elif [[ -z "$mode" ]]; then
        _t_skip "R2: could not read root permissions (no usable stat)"
    else
        _t_fail "R2: root permissions are ${mode}, expected 700 (config holds the API key)"
    fi

    [[ -O "$HANDQ_ROOT" ]] \
        && _t_pass "R3: root is owned by $(whoami)" \
        || _t_fail "R3: root is NOT owned by $(whoami)"

    # R4: the recorded root is what we actually used. This is the contract
    # handq_linux.py and the Windows probe both read instead of re-deriving the
    # candidate chain; a mismatch here is how the three would drift apart.
    local host_conf="${INSTALLED_HOST_CONF_PATH:-${HOST_CONF_DIR}/${HOSTNAME_SHORT}}"
    if [[ -f "$host_conf" ]]; then
        local recorded
        recorded="$(sed -n 's/^export HANDQ_ROOT="\(.*\)"$/\1/p' "$host_conf" | head -1)"
        if [[ "$recorded" == "$HANDQ_ROOT" ]]; then
            _t_pass "R4: per-host config records the resolved root"
        else
            _t_fail "R4: per-host config records '${recorded}', resolved root is '${HANDQ_ROOT}'"
        fi
    else
        _t_fail "R4: per-host config missing: ${host_conf}"
    fi

    # R5: config and entry point must both live under the root, or the relocation
    # only half-happened and something still points into the synced $HOME.
    case "$CONFIG_PATH" in
        "${HANDQ_ROOT}"/*) _t_pass "R5: config lives under the root" ;;
        *) _t_fail "R5: config is outside the root: ${CONFIG_PATH}" ;;
    esac
    case "${MAIN_EXEC:-}" in
        "${HANDQ_ROOT}"/*) _t_pass "R6: entry point lives under the root" ;;
        "") _t_skip "R6: no entry point resolved" ;;
        *) _t_fail "R6: entry point is outside the root: ${MAIN_EXEC}" ;;
    esac

    # R7: nothing should still be pointing at the legacy shared locations. Not a
    # failure — the old install is deliberately never deleted (it may be another,
    # not-yet-migrated host's LIVE install, and the delete is unrecoverable) — but
    # it should be visible so the space can be reclaimed once the fleet is done.
    local legacy_root="${HOME}/handq"
    if [[ -d "${legacy_root}/handq_linux.dist" && "$HANDQ_ROOT" != "${legacy_root}"* ]]; then
        info "Legacy install still present at ${legacy_root}/handq_linux.dist"
        info "  It is no longer used by this host. Leaving it alone: another host that"
        info "  has not migrated yet may still be running from it. Remove it by hand"
        info "  once every host has been migrated."
    fi
    if [[ -d "${HOME}/.handq" ]]; then
        info "Legacy IPC dir still present at ${HOME}/.handq (superseded by the root)"
    fi
}

run_test_mode() {
    print_step "Test suite"
    # Tests intentionally probe non-zero exit codes (e.g. --status with no
    # daemon), so relax errexit for the duration.
    set +e

    _suite_r_root
    echo ""
    _suite_a_static
    echo ""
    _suite_b_smoke

    local total=$((_T_PASS + _T_FAIL + _T_SKIP))
    echo ""
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "  Results: %s passed  %s failed  %s skipped  (%s total)\n" \
        "$_T_PASS" "$_T_FAIL" "$_T_SKIP" "$total"
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    echo ""
    if [[ "$_T_FAIL" -eq 0 ]]; then
        printf "${C_GREEN}${C_BOLD}  ✅  All tests passed.${C_RESET}\n\n"
        return 0
    else
        printf "${C_RED}${C_BOLD}  ❌  ${_T_FAIL} test(s) failed.${C_RESET}\n\n"
        return 1
    fi
}

# =============================================================================
# Main
# =============================================================================
main() {
    # --print-root is machine-readable: resolve, print the bare path on stdout,
    # and emit nothing else. Handled before the banner so callers (and the Windows
    # probe) can consume the output directly.
    if [[ "$PRINT_ROOT_MODE" == true ]]; then
        establish_root >&2
        printf '%s\n' "$HANDQ_ROOT"
        _finish 0
    fi

    print_header

    # Resolve the install root before anything else — every other path below is
    # derived from it.
    establish_root

    # --test inspects an existing install; it must not stage a package over it.
    if [[ "$TEST_MODE" != true ]]; then
        stage_into_root
    fi

    # Default the config to the root now that the root is known.
    [[ -z "$CONFIG_PATH" ]] && CONFIG_PATH="${HANDQ_ROOT}/handq_config.yaml"

    if ! check_required_files; then
        die "Setup cannot continue — required files missing."
    fi

    if [[ "$TEST_MODE" == true ]]; then
        run_test_mode && _finish 0 || _finish 1
    fi

    if ! check_dependencies; then
        die "Dependency check failed."
    fi

    if ! validate_config; then
        die "Config validation failed."
    fi

    INSTALLED_CMD_PATH=""
    if ! install_command; then
        die "Command installation failed."
    fi

    echo ""
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "  ${C_BOLD}HandQ (Linux) Setup — Complete${C_RESET}\n"
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "  Install root : ${HANDQ_ROOT}\n"
    printf "  Package dir  : ${PKG_DIR}\n"
    printf "  Config       : ${CONFIG_PATH}\n"
    printf "  Command      : ${INSTALLED_CMD_PATH:-<not installed>}  (aliases: ${ALIASES[*]})\n"
    printf "  IPC + state  : ${HANDQ_ROOT}\n"
    printf "  Workspace    : ${HANDQ_ROOT}/workspace/<session_id>\n"
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    echo ""
    info "Run 'handq_linux' (or 'handq' / 'hi') from any directory to open the console."
    info "The Windows HandQ controls this host via SSH and wakes the daemon automatically."
    echo ""

    run_test_mode
    local test_rc=$?

    echo ""
    if [[ $test_rc -eq 0 ]]; then
        info "Installation verified — run 'handq_linux' to start."
    else
        warn "Installation complete but some tests failed — check output above."
    fi
    # Propagate the verdict. This used to end on info/warn (both return 0), so the
    # script exited 0 even with failing assertions — which meant
    # remote_handq_tool._install_human_aliases' `if rc != 0` never fired for a
    # self-test failure, and its docstring's claim to surface setup problems was
    # false for exactly this class.
    _finish "$test_rc"
}

main "$@"
