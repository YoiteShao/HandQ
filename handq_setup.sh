#!/usr/bin/env bash
# Version: 4.0
# =============================================================================
# handq_setup.sh — Linux HandQ (sub-agent daemon) setup script
# =============================================================================
#
# Installs the `handq_linux` command — the Linux "sub HandQ" entry point.
# One Windows HandQ controls many Linux HandQ over SSH (the remote_handq tool);
# each Linux box runs handq_linux.py as a resident, setsid-detached
# FlowControllerV2 daemon with a local emergency console. There is no tmux,
# no systemd, no state bar — Windows drives the daemon through a file pipe
# under ~/.handq/<user>@<host>/, and the local console shares the same pipe.
#
# Usage:
#   bash handq_setup.sh                        # Install + verify
#   bash handq_setup.sh --config <path>        # Use a custom config file
#   bash handq_setup.sh --test                 # Re-run verification only (no install)
#
# What this script does:
#   1. Verifies the handq_linux entry point (binary or .py) and config
#   2. Installs `handq_linux` as a standalone command in /usr/local/bin
#      (preferred) or ~/.local/bin, plus `handq` and `hi` aliases
#   3. Adds the install dir to PATH in your shell profile, if needed
#
# After install:
#   handq_linux              # open the local console (starts the daemon)
#   handq                    # alias — same thing
#   hi                       # short alias — same thing
#   handq_linux "fix the build and run the tests"   # one-shot goal
#   handq_linux --status     # print the daemon's state.json
#   handq_linux --exit       # stop the daemon
#
# The Windows side discovers this command via `command -v handq_linux`
# (or ~/.local/bin/handq_linux) and wakes the daemon with `--_daemon`.
#
# Exit codes:
#   0 — Success     1 — Fatal error     2 — Bad arguments
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

# Short hostname (no domain suffix) — used to construct the per-host .handq/ dir.
# Mirrors handq_linux.py's `socket.gethostname().split(".")[0]` and the remote
# probe's `hostname -s`, so all three resolve the SAME ~/.handq/<user>@<host>.
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
WHO="$(whoami)"
# HOME-based IPC dir (handq_linux.py uses Path.home(), NOT the script dir).
HANDQ_DIR="${HOME}/.handq/${WHO}@${HOSTNAME_SHORT}"

# ── Resolve script directory ──────────────────────────────────────────────────
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_CONFIG="${SCRIPT_DIR}/handq_config.yaml"
INSTALL_DIR="${SCRIPT_DIR}"
SYSTEM_INSTALL_DIR="/usr/local/bin"
FALLBACK_INSTALL_DIR="${HOME}/.local/bin"
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
TEST_MODE=false

usage() {
    cat <<EOF
Usage: $(basename "${BASH_SOURCE[0]}") [OPTIONS]

Options:
  --config <path>   Path to handq_config.yaml (default: ./handq_config.yaml)
  --test            Re-run verification only, skip install
  -h, --help        Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            [[ -z "${2:-}" ]] && { err "--config requires a path"; usage; _finish 2; }
            CONFIG_PATH="$2"; shift 2 ;;
        --test) TEST_MODE=true; shift ;;
        -h|--help) usage; _finish 0 ;;
        *) err "Unknown option: $1"; usage; _finish 2 ;;
    esac
done

if [[ -n "$CONFIG_PATH" ]]; then
    CONFIG_PATH="${CONFIG_PATH/#\~/$HOME}"
    CONFIG_PATH="$(realpath -m "$CONFIG_PATH")"
else
    CONFIG_PATH="$DEFAULT_CONFIG"
fi

# =============================================================================
# check_required_files — locate the handq_linux entry point + config
# =============================================================================
check_required_files() {
    print_step "Checking required files"
    local py_main="${SCRIPT_DIR}/handq_linux.py"
    local bin_main="${SCRIPT_DIR}/handq_linux.bin"
    local standalone_bin="${SCRIPT_DIR}/handq_linux.dist/handq_linux.bin"
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
            if [[ ! -d "${SCRIPT_DIR}/src" ]]; then
                warn "src/ not found next to handq_linux.py — the daemon imports src.* and will fail to start."
            fi
        else
            ok "Found Python entry-point (backup): ${py_main}"
        fi
    fi
    [[ -z "$MAIN_EXEC" ]] && { err "No handq_linux entry point found in ${SCRIPT_DIR} (looked for handq_linux.dist/handq_linux.bin, handq_linux.bin, handq_linux.py)"; all_ok=false; }

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
    # 2. a .venv/venv co-located with the script (matches the remote probe)
    for c in "${SCRIPT_DIR}/.venv/bin/python3" "${SCRIPT_DIR}/.venv/bin/python" \
             "${SCRIPT_DIR}/venv/bin/python3" "${SCRIPT_DIR}/venv/bin/python"; do
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

    # Install priority:
    #   1. /usr/local/bin  — system-wide, on the PATH of non-interactive SSH
    #      sessions (so `command -v handq_linux` works for the Windows probe).
    #   2. ~/.local/bin    — current-user fallback; the Windows probe checks
    #      this path explicitly since it is usually NOT on the SSH PATH.
    local target_dir=""
    for candidate in "$SYSTEM_INSTALL_DIR" "$FALLBACK_INSTALL_DIR"; do
        mkdir -p "$candidate" 2>/dev/null || true
        if [[ -d "$candidate" && -w "$candidate" ]]; then
            target_dir="$candidate"; break
        fi
    done
    [[ -z "$target_dir" ]] && { err "No writable install directory found (tried ${SYSTEM_INSTALL_DIR}, ${FALLBACK_INSTALL_DIR})"; return 1; }

    # Remove stale wrappers/aliases from all OTHER candidate locations.
    for stale_dir in "$SYSTEM_INSTALL_DIR" "$FALLBACK_INSTALL_DIR" "$INSTALL_DIR"; do
        [[ "$stale_dir" == "$target_dir" ]] && continue
        for stale_name in "$COMMAND_NAME" "${ALIASES[@]}"; do
            local stale="${stale_dir}/${stale_name}"
            [[ -e "$stale" || -L "$stale" ]] && ( rm -f "$stale" 2>/dev/null && warn "Removed stale: ${stale}" ) || true
        done
    done

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
    # Each host writes ONLY its own file; other hosts' files are untouched.
    # The dispatcher sources this and forwards "$@", so the Windows side can
    # append --_daemon / --status / a goal and the config is always injected.
    mkdir -p "$HOST_CONF_DIR"
    local host_conf="${HOST_CONF_DIR}/${HOSTNAME_SHORT}"
    printf 'exec %s "$@"\n' "$exec_line" > "$host_conf"
    chmod +x "$host_conf"
    ok "Host config  : ${host_conf}"

    # ── 2. Write the static hostname dispatcher ──────────────────────────────
    # Identical on every host — routes to the correct binary at runtime via the
    # per-host config above.
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
    local py_main="${SCRIPT_DIR}/handq_linux.py"
    local bin_main="${SCRIPT_DIR}/handq_linux.bin"
    local standalone_bin="${SCRIPT_DIR}/handq_linux.dist/handq_linux.bin"
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
        wrapper="$(command -v "${COMMAND_NAME}" 2>/dev/null || true)"
        [[ -z "$wrapper" ]] && wrapper="${FALLBACK_INSTALL_DIR}/${COMMAND_NAME}"
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

    local py_main="${SCRIPT_DIR}/handq_linux.py"
    local bin_main="${SCRIPT_DIR}/handq_linux.bin"
    local standalone_bin="${SCRIPT_DIR}/handq_linux.dist/handq_linux.bin"
    { grep -qF "$py_main" "$host_conf" 2>/dev/null || grep -qF "$bin_main" "$host_conf" 2>/dev/null \
      || grep -qF "$standalone_bin" "$host_conf" 2>/dev/null; } \
        && _t_pass "A3: host config references the handq_linux entry point" \
        || _t_fail "A3: host config missing the handq_linux entry point"

    assert_file_exists "A4: config file exists" "$CONFIG_PATH"

    # A5: aliases resolve to the dispatcher.
    local _alias_dir; _alias_dir="$(dirname "$wrapper")"
    for alias_name in "${ALIASES[@]}"; do
        local alias_path="${_alias_dir}/${alias_name}"
        if [[ -L "$alias_path" || -f "$alias_path" ]]; then
            _t_pass "A5: alias '${alias_name}' installed"
        else
            _t_fail "A5: alias '${alias_name}' missing"
        fi
    done

    # A6: dispatcher must be a clean launcher — no PS1/PROMPT_COMMAND/tmux.
    if grep -qE "PROMPT_COMMAND|PS1|prompt-state|tmux" "$wrapper" 2>/dev/null; then
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

run_test_mode() {
    print_step "Test suite"
    # Tests intentionally probe non-zero exit codes (e.g. --status with no
    # daemon), so relax errexit for the duration.
    set +e

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
    print_header

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
    printf "  Script dir : ${SCRIPT_DIR}\n"
    printf "  Config     : ${CONFIG_PATH}\n"
    printf "  Command    : ${INSTALLED_CMD_PATH:-<not installed>}  (aliases: ${ALIASES[*]})\n"
    printf "  IPC dir    : ${HANDQ_DIR}\n"
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
}

main "$@"
