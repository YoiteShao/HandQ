#!/usr/bin/env bash
# Version: 3.0
# =============================================================================
# handq_setup.sh — HandQ Setup Script
# =============================================================================
#
# Usage:
#   bash handq_setup.sh                        # Install + verify
#   bash handq_setup.sh --config <path>        # Use a custom config file
#   bash handq_setup.sh --test                 # Re-run verification only (no install)
#
# What this script does:
#   1. Verifies Python, tmux, required files and config
#   2. Installs 'handq' as a standalone executable in ~/.local/bin/
#   3. Adds ~/.local/bin to PATH in your shell profile (~/.bashrc or ~/.zshrc)
#   4. Configures tmux status bar to show HandQ state (state0/1/2/3)
#
# First-time install (two steps):
#   bash handq_setup.sh --config ./myconfig.yaml
#   source ~/.bashrc        # bash/zsh; tcsh: source ~/.tcshrc
#
# Exit codes:
#   0 — Success
#   1 — Fatal error
#   2 — Bad arguments
# =============================================================================

set -euo pipefail

# Guard: this script must be executed, not sourced.
# Sourcing it pollutes the caller's shell with internal variables and functions.
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
    printf "${C_BOLD}${C_CYAN}║          HandQ Setup                             ║${C_RESET}\n"
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

_finish() {
    local code="${1:-0}"
    exit "$code"
}
die() { err "$*"; _finish 1; }

# Short hostname (no domain suffix) — used to construct per-host .handq/ dirs
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"

# ── Legacy session cleanup helper ─────────────────────────────────────────────
# Outputs a shell snippet that removes old HandQ functions/vars from the current
# session.  Usage:  eval "$(handq_setup_cleanup)"
handq_setup_cleanup() {
    cat <<'CLEANUP'
unset -f handq _handq_update_prompt _handq_capture_check _handq_install_prompt hi 2>/dev/null || true
unset HANDQ_PS1_PATCHED HANDQ_PROMPT 2>/dev/null || true
CLEANUP
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
DEFAULT_CONFIG="${SCRIPT_DIR}/handq_config.yaml"
INSTALL_DIR="${SCRIPT_DIR}"
SYSTEM_INSTALL_DIR="/usr/local/bin"
FALLBACK_INSTALL_DIR="${HOME}/.local/bin"
HOST_CONF_DIR="${HOME}/.config/handq/hosts"   # per-host exec config (one file per hostname)
COMMAND_NAME="handq"
REQUIRED_KEYS=("version" "llm" "session" "interaction_switches")
REQUIRED_LLM_KEYS=("models")
REQUIRED_SESSION_KEYS=()

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
# check_required_files
# =============================================================================
check_required_files() {
    print_step "Checking required files"
    local py_main="${SCRIPT_DIR}/handq.py"
    local bin_main="${SCRIPT_DIR}/handq.bin"
    local all_ok=true

    MAIN_EXEC="" USE_PYTHON=false
    # Standalone layout: binary inside handq.dist/
    local standalone_bin="${SCRIPT_DIR}/handq.dist/handq.bin"
    if [[ -f "$standalone_bin" ]]; then
        [[ ! -x "$standalone_bin" ]] && { chmod +x "$standalone_bin"; info "Set executable bit on ${standalone_bin}"; }
        MAIN_EXEC="$standalone_bin"; ok "Found standalone binary: ${standalone_bin}"
    fi
    # Legacy / onefile layout: binary at top level
    if [[ -z "$MAIN_EXEC" && -f "$bin_main" ]]; then
        [[ ! -x "$bin_main" ]] && { chmod +x "$bin_main"; info "Set executable bit on ${bin_main}"; }
        MAIN_EXEC="$bin_main"; ok "Found binary: ${bin_main}"
    fi
    if [[ -f "$py_main" ]]; then
        if [[ -z "$MAIN_EXEC" ]]; then
            MAIN_EXEC="$py_main"; USE_PYTHON=true
            ok "Found Python entry-point: ${py_main}"
        else
            ok "Found Python entry-point (backup): ${py_main}"
        fi
    fi
    [[ -z "$MAIN_EXEC" ]] && { err "Neither handq.py nor handq.bin found in ${SCRIPT_DIR}"; all_ok=false; }

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

    # Python
    if [[ "$USE_PYTHON" == true ]]; then
        local python_exe
        python_exe="$(_resolve_python_exe)"
        if [[ -n "$python_exe" ]]; then
            ok "Python: ${python_exe}"
        else
            err "Python 3 not found"; all_ok=false
        fi
    fi

    # tmux
    TMUX_AVAILABLE=false
    if command -v tmux &>/dev/null; then
        local tmux_ver
        tmux_ver="$(tmux -V 2>/dev/null || echo 'unknown')"
        ok "tmux: ${tmux_ver} — status bar integration enabled"
        TMUX_AVAILABLE=true
    else
        warn "tmux not found — HandQ state bar unavailable"
        warn "  Install tmux for the best experience: apt install tmux"
    fi

    [[ "$all_ok" == false ]] && return 1 || return 0
}

# =============================================================================
# validate_config
# =============================================================================
validate_config() {
    print_step "Validating config file" "${CONFIG_PATH}"
    local all_ok=true

    # ── Real YAML parse via PyYAML ────────────────────────────────────────
    # The grep checks below only confirm key names appear on a line; they
    # don't catch syntax errors like a missing space after a colon
    # (e.g. "API_KEY:abc" instead of "API_KEY: abc"), which silently
    # invalidates the entire `llm:` block and leaves all LLM roles empty
    # at runtime. Best-effort: skipped if python3 or PyYAML aren't present.
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
    for key in "${REQUIRED_LLM_KEYS[@]}"; do
        if grep -qE "^[[:space:]]+${key}:" "$CONFIG_PATH"; then
            ok "llm.${key} present"
        else
            err "Missing llm.${key}"; all_ok=false
        fi
    done
    for key in "${REQUIRED_SESSION_KEYS[@]+"${REQUIRED_SESSION_KEYS[@]}"}"; do
        if grep -qE "^[[:space:]]+${key}:" "$CONFIG_PATH"; then
            ok "session.${key} present"
        else
            err "Missing session.${key}"; all_ok=false
        fi
    done

    local model_val
    model_val="$(awk '/^[[:space:]]+models:/{found=1; next} found && /^[[:space:]]*#/{next} found && /^[[:space:]]+-/{gsub(/^[[:space:]]+-[[:space:]]*/,""); gsub(/[[:space:]]*$/,""); print; exit} found && /^[[:space:]]+[^-]/{exit}' "$CONFIG_PATH" | tr -d '"'"'")"
    [[ -z "$model_val" || "$model_val" == "null" ]] && warn "llm.models appears empty" || ok "llm.models[0] = ${model_val}"

    # Support both API_KEY (direct value) and api_key_env (env var name) formats.
    local api_key_env_val
    api_key_env_val="$(grep -E "^[[:space:]]+api_key_env:" "$CONFIG_PATH" | head -1 | sed 's/.*api_key_env:[[:space:]]*//' | sed 's/[[:space:]]*#.*//' | tr -d '"'"'" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ -n "$api_key_env_val" && "$api_key_env_val" != "null" ]]; then
        if [[ "$api_key_env_val" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
            local api_key_resolved="${!api_key_env_val:-}"
            [[ -n "$api_key_resolved" ]] && ok "\$${api_key_env_val} is set" || warn "\$${api_key_env_val} is NOT set — set it before running handq"
        else
            ok "llm.api_key_env contains a direct key value"
        fi
    else
        # Check for direct API_KEY value
        local api_key_direct
        api_key_direct="$(grep -E "^[[:space:]]+API_KEY:" "$CONFIG_PATH" | head -1 | sed 's/.*API_KEY:[[:space:]]*//' | sed 's/[[:space:]]*#.*//' | tr -d '"'"'" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        if [[ -n "$api_key_direct" && "$api_key_direct" != "null" ]]; then
            ok "llm.API_KEY is set"
        else
            warn "llm.API_KEY is empty — fill in your API key before running handq"
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
    # 2. active VIRTUAL_ENV
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        local _venv_python="${VIRTUAL_ENV}/bin/python3"
        [[ -x "$_venv_python" ]] && echo "$_venv_python" && return
    fi
    # 3. PATH
    command -v python3 2>/dev/null && return
    echo ""
}

# =============================================================================
# install_command — write standalone executable wrapper
# =============================================================================
install_command() {
    print_step "Installing '${COMMAND_NAME}' command"

    # Install priority:
    #   1. /usr/local/bin  — system-wide, accessible to all users (preferred)
    #   2. ~/.local/bin    — current user only (fallback when no write access)
    # SCRIPT_DIR is never used as install target to avoid PATH pollution.
    local target_dir=""
    for candidate in "$SYSTEM_INSTALL_DIR" "$FALLBACK_INSTALL_DIR"; do
        mkdir -p "$candidate" 2>/dev/null || true
        if [[ -d "$candidate" && -w "$candidate" ]]; then
            target_dir="$candidate"; break
        fi
    done
    [[ -z "$target_dir" ]] && { err "No writable install directory found (tried ${SYSTEM_INSTALL_DIR}, ${FALLBACK_INSTALL_DIR})"; return 1; }

    # Remove stale wrappers from all OTHER candidate locations to prevent PATH confusion
    for stale_dir in "$SYSTEM_INSTALL_DIR" "$FALLBACK_INSTALL_DIR" "$INSTALL_DIR"; do
        [[ "$stale_dir" == "$target_dir" ]] && continue
        for stale_name in "$COMMAND_NAME" "hi"; do
            local stale="${stale_dir}/${stale_name}"
            [[ -f "$stale" || -L "$stale" ]] && ( rm -f "$stale" 2>/dev/null && warn "Removed stale: ${stale}" ) || true
        done
    done

    local cmd_path="${target_dir}/${COMMAND_NAME}"
    local exec_line prompt_state_line

    if [[ "$USE_PYTHON" == true ]]; then
        local python_exe
        python_exe="$(_resolve_python_exe)"
        [[ -z "$python_exe" ]] && { err "Python 3 not found"; return 1; }
        exec_line="\"${python_exe}\" \"${MAIN_EXEC}\" --config \"${CONFIG_PATH}\""
        prompt_state_line="\"${python_exe}\" \"${MAIN_EXEC}\" --config \"${CONFIG_PATH}\" prompt-state"
    else
        exec_line="\"${MAIN_EXEC}\" --config \"${CONFIG_PATH}\""
        prompt_state_line="\"${MAIN_EXEC}\" --config \"${CONFIG_PATH}\" prompt-state"
    fi

    # ── 1. Write per-host exec config ────────────────────────────────────────
    # Each host writes ONLY its own file; other hosts' files are untouched.
    mkdir -p "$HOST_CONF_DIR"
    local host_conf="${HOST_CONF_DIR}/${HOSTNAME_SHORT}"
    printf 'exec %s "$@"\n' "$exec_line" > "$host_conf"
    chmod +x "$host_conf"
    ok "Host config  : ${host_conf}"

    # ── 2. Write static hostname dispatcher ──────────────────────────────────
    # Content is identical regardless of host/installation — safe to overwrite.
    # Routes 'handq' to the correct binary for the current machine at runtime.
    cat > "$cmd_path" <<'DISPATCHER'
#!/usr/bin/env bash
# handq — HandQ AI task execution agent
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

    # Install 'hi' as a symlink alias for 'handq'
    local hi_path="${target_dir}/hi"
    ln -sf "$cmd_path" "$hi_path" 2>/dev/null && ok "Installed alias: ${hi_path} → handq" \
        || warn "Could not create hi symlink at ${hi_path}"

    # Detect whether legacy shell functions are live in the current session.
    # If the old wrapper was ever sourced/eval'd, handq() or _handq_update_prompt
    # may still exist as shell functions.  Emit a one-time cleanup snippet.
    local _needs_cleanup=false
    if declare -f handq &>/dev/null 2>&1; then _needs_cleanup=true; fi
    if declare -f _handq_update_prompt &>/dev/null 2>&1; then _needs_cleanup=true; fi
    if [[ -n "${HANDQ_PS1_PATCHED:-}" ]]; then _needs_cleanup=true; fi

    if [[ "$_needs_cleanup" == true ]]; then
        warn "Legacy HandQ shell functions detected in current session."
        info "Run the following to clean up this session (one-time):"
        printf "\n    ${C_CYAN}eval \"\$(handq_setup_cleanup)\"${C_RESET}\n\n"
        info "Or paste directly:"
        printf "    ${C_CYAN}unset -f handq _handq_update_prompt _handq_capture_check _handq_install_prompt hi 2>/dev/null; unset HANDQ_PS1_PATCHED HANDQ_PROMPT 2>/dev/null${C_RESET}\n\n"
    fi

    # Ensure it's on PATH — write export line into shell profile if needed,
    # and emit the export to stdout so eval applies it to the current session.
    if echo ":${PATH}:" | grep -q ":${target_dir}:"; then
        ok "${target_dir} is already on PATH"
    else
        # Detect the user's shell profile file and correct syntax
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
    _PROMPT_STATE_LINE="$prompt_state_line"
    return 0
}

# =============================================================================
# configure_tmux — insert HandQ state into tmux status bar
# =============================================================================
configure_tmux() {
    [[ "$TMUX_AVAILABLE" == false ]] && return 0

    print_step "Configuring HandQ tmux (isolated config)"

    local handq_dir="${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}"
    local handq_conf="${handq_dir}/tmux.conf"
    local handq_status_script="${handq_dir}/tmux_status.py"
    mkdir -p "$handq_dir"

    # ── Remove any legacy HandQ block from ~/.tmux.conf ───────────────────
    local user_tmux_conf="${HOME}/.tmux.conf"
    if [[ -f "$user_tmux_conf" ]] && grep -q "HANDQ_TMUX_STATUS" "$user_tmux_conf" 2>/dev/null; then
        sed -i '/HANDQ_TMUX_STATUS/,/End HandQ/d' "$user_tmux_conf"
        # Also clean up any leftover HandQ-managed lines
        sed -i '/^\s*set-environment -gu PS[1234]/d' "$user_tmux_conf"
        ok "Removed legacy HandQ block from ${user_tmux_conf}"
    fi

    # ── Ensure tmux_status.py placeholder exists ──────────────────────────
    if [[ ! -f "$handq_status_script" ]]; then
        printf '# HandQ tmux status placeholder — replaced on first handq run\n' \
            > "$handq_status_script"
        ok "Created tmux_status.py placeholder: ${handq_status_script}"
    fi

    local python_exe
    python_exe="$(_resolve_python_exe)"
    [[ -z "$python_exe" ]] && python_exe="python3"

    local status_left="#(${python_exe} ${handq_status_script}) #[default] ◈ #S "

    # ── Write isolated HandQ tmux config ─────────────────────────────────
    {
        printf '# HandQ isolated tmux config — generated by handq_setup.sh\n'
        printf '# This file is loaded via -f and does NOT affect ~/.tmux.conf\n'
        printf '\n'
        printf 'set -g status-left "%s"\n' "${status_left}"
        printf 'set -g status-left-length 60\n'
        printf 'set -g status-right "#[dim]Alt+↑↓ scroll  #[default]%%H:%%M %%d-%%b"\n'
        printf 'set -g status-interval 1\n'
        printf 'set -g status-style bg=default\n'
        printf '\n'
        printf '# Alt+Up/Down: scroll without stealing focus\n'
        printf 'bind-key -n M-Up copy-mode \; send-keys -X scroll-up\n'
        printf 'bind-key -n M-Down send-keys -X scroll-down\n'
        printf '\n'
        printf '# Remove PS1/PS2 from tmux environment (prevents zsh startup errors)\n'
        printf 'set-environment -gu PS1\n'
        printf 'set-environment -gu PS2\n'
        printf 'set-environment -gu PS3\n'
        printf 'set-environment -gu PS4\n'
    } > "$handq_conf"

    ok "HandQ tmux config written: ${handq_conf}"
    info "This config is loaded only by the HandQ tmux server (isolated from your ~/.tmux.conf)"

    # ── Apply to running HandQ tmux server if active ──────────────────────
    local _handq_sock="handq-${USER}@${HOSTNAME_SHORT}"
    if tmux -L "$_handq_sock" has-session 2>/dev/null; then
        tmux -L "$_handq_sock" source-file "$handq_conf" 2>/dev/null \
            && ok "Applied to running HandQ tmux server" \
            || warn "Could not apply to running HandQ tmux server (will apply on next start)"
    fi

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
assert_empty()      { [[ -z "$2" ]]      && _t_pass "$1" || _t_fail "$1  (expected empty, got: $(printf '%q' "$2"))"; }
assert_nonempty()   { [[ -n "$2" ]]      && _t_pass "$1" || _t_fail "$1  (expected non-empty)"; }
assert_file_exists(){ [[ -f "$2" ]]      && _t_pass "$1" || _t_fail "$1  (file not found: $2)"; }
assert_file_absent(){ [[ ! -f "$2" ]]    && _t_pass "$1" || _t_fail "$1  (file should not exist: $2)"; }

_resolve_exec_line() {
    local py_main="${SCRIPT_DIR}/handq.py"
    local bin_main="${SCRIPT_DIR}/handq.bin"
    local standalone_bin="${SCRIPT_DIR}/handq.dist/handq.bin"
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

_write_test_state() {
    local status="${1:-}" active="${2:-true}"
    mkdir -p "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}"
    if [[ -n "$status" ]]; then
        printf '{"handq_active":%s,"task_status":"%s","session_id":"test"}\n' "$active" "$status" > "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/state.json"
    else
        printf '{"handq_active":%s,"session_id":"test"}\n' "$active" > "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/state.json"
    fi
}

_clean_handq_state() {
    rm -f "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/state.json" "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/handq.pid" \
          "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/prompt_daemon.pid" \
          "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/confirmation_request.json" \
          "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/confirmation_response.txt"
    rm -f "${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/messages"/*.txt 2>/dev/null || true
}

# ── Suite A: static checks ────────────────────────────────────────────────────
_suite_a_static() {
    print_step "Suite A — Static checks"

    # Check the actually-installed wrapper written by install_command.
    # Prefer INSTALLED_CMD_PATH (set by install_command) over command -v,
    # which may find a stale copy in a different PATH directory.
    local wrapper
    if [[ -n "${INSTALLED_CMD_PATH:-}" ]]; then
        wrapper="$INSTALLED_CMD_PATH"
    else
        wrapper="$(command -v "${COMMAND_NAME}" 2>/dev/null || true)"
        [[ -z "$wrapper" ]] && wrapper="${FALLBACK_INSTALL_DIR}/${COMMAND_NAME}"
    fi

    assert_file_exists "A1: dispatcher file exists" "$wrapper"
    [[ -x "$wrapper" ]] && _t_pass "A1: dispatcher is executable" || _t_fail "A1: dispatcher not executable"

    # A2/A3: config path and main executable are in the per-host config, not the dispatcher
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

    local py_main="${SCRIPT_DIR}/handq.py"
    local bin_main="${SCRIPT_DIR}/handq.bin"
    local standalone_bin="${SCRIPT_DIR}/handq.dist/handq.bin"
    { grep -qF "$py_main" "$host_conf" 2>/dev/null || grep -qF "$bin_main" "$host_conf" 2>/dev/null \
      || grep -qF "$standalone_bin" "$host_conf" 2>/dev/null; } \
        && _t_pass "A3: host config references main executable" \
        || _t_fail "A3: host config missing main executable"

    assert_file_exists "A4: config file exists" "$CONFIG_PATH"

    if [[ -f "$py_main" ]]; then
        command -v python3 &>/dev/null && _t_pass "A5: python3 available" || _t_fail "A5: python3 not found"
    else
        _t_skip "A5: python3 check (using binary)"
    fi

    # A6: wrapper must NOT contain PROMPT_COMMAND or PS1 patching
    if grep -qE "PROMPT_COMMAND|PS1|source.*handq" "$wrapper" 2>/dev/null; then
        _t_fail "A6: wrapper contains shell integration (should be clean executable)"
    else
        _t_pass "A6: wrapper is a clean executable (no shell integration)"
    fi

    # A7: shell rc files must NOT source handq (legacy shell integration)
    local _found_legacy=false
    for _rc in "${HOME}/.bashrc" "${HOME}/.tcshrc" "${HOME}/.cshrc.local" "${HOME}/.zshrc"; do
        if [[ -f "$_rc" ]] && grep -qE "source.*handq|\..*handq" "$_rc" 2>/dev/null; then
            _t_fail "A7: legacy handq source found in ${_rc} — remove it to prevent auto-attach on shell start"
            _found_legacy=true
        fi
    done
    [[ "$_found_legacy" == false ]] && _t_pass "A7: no legacy handq source in shell rc files"
}

# ── Suite B: state machine ────────────────────────────────────────────────────
_suite_b_state_machine() {
    print_step "Suite B — State machine"

    local exec_line; exec_line="$(_resolve_exec_line)"
    [[ -z "$exec_line" ]] && { _t_skip "B: all tests (handq.py not found)"; return; }

    local conf_req="${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/confirmation_request.json"
    local out

    _clean_handq_state
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_empty "B1: state0 → prompt-state empty" "$out"

    _clean_handq_state; _write_test_state ""
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_eq "B2: state1 → [HandQ]" "$out" "[HandQ]"

    _clean_handq_state; _write_test_state "running"
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_eq "B3: state2 → [HandQ Running]" "$out" "[HandQ Running]"

    _clean_handq_state; _write_test_state "completed"
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_eq "B4: state3 → [HandQ Complete]" "$out" "[HandQ Complete]"

    _clean_handq_state; _write_test_state "running"
    echo '{"type":"tool","tool_name":"bash"}' > "$conf_req"
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_eq "B5: state4 → [HandQ Confirm?]" "$out" "[HandQ Confirm?]"
    rm -f "$conf_req"

    _clean_handq_state; _write_test_state "running" "false"
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_empty "B6: handq_active=false → state0 empty" "$out"

    _clean_handq_state
    echo '{"type":"tool","tool_name":"bash"}' > "$conf_req"
    eval "$exec_line --new" >/dev/null 2>&1 || true
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_eq "B7: --new → state1 [HandQ]" "$out" "[HandQ]"
    assert_file_absent "B7: --new removes stale confirmation_request" "$conf_req"

    _clean_handq_state; _write_test_state "running"
    eval "$exec_line --exit" >/dev/null 2>&1 || true
    out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
    assert_empty "B8: --exit → state0 empty" "$out"

    _clean_handq_state
}

# ── Suite C: tmux integration ─────────────────────────────────────────────────
_suite_c_tmux() {
    print_step "Suite C — tmux integration"

    if [[ "$TMUX_AVAILABLE" == false ]]; then
        _t_skip "C: all tmux tests (tmux not installed)"
        return
    fi

    local tmux_conf="${SCRIPT_DIR}/.handq/${USER}@${HOSTNAME_SHORT}/tmux.conf"

    # C1: HandQ isolated tmux.conf exists
    [[ -f "$tmux_conf" ]] \
        && _t_pass "C1: HandQ isolated tmux.conf exists" \
        || _t_fail "C1: HandQ isolated tmux.conf missing (run handq_setup.sh)"

    # C2: status-right uses %H:%M
    grep -qE "status-right.*%H:%M" "$tmux_conf" 2>/dev/null \
        && _t_pass "C2: status-right has clean time format" \
        || _t_fail "C2: status-right missing time format"

    # C3: status-interval is set to 1 (spinner animation)
    grep -qE "status-interval\s+1" "$tmux_conf" 2>/dev/null \
        && _t_pass "C3: status-interval 1 (spinner animation)" \
        || _t_fail "C3: status-interval not set to 1"

    # C4: prompt-state returns correct value for state1
    local exec_line; exec_line="$(_resolve_exec_line)"
    if [[ -n "$exec_line" ]]; then
        _clean_handq_state; _write_test_state ""
        local out; out="$(eval "$exec_line --prompt-state" 2>/dev/null)"
        assert_eq "C4: prompt-state output for state1" "$out" "[HandQ]"
        _clean_handq_state
    else
        _t_skip "C4: prompt-state test (handq.py not found)"
    fi

    # C5: handq tmux session has session-closed hook set (prevents orphan sessions)
    local _handq_user="${USER:-default}@${HOSTNAME_SHORT}"
    local _handq_sock="handq-${_handq_user}"
    if tmux -L "$_handq_sock" has-session -t "handq-${_handq_user}" 2>/dev/null; then
        local hook_val
        hook_val="$(tmux -L "$_handq_sock" show-hooks -t "handq-${_handq_user}" 2>/dev/null | grep session-closed || true)"
        if [[ -n "$hook_val" ]]; then
            _t_pass "C5: handq tmux session has session-closed hook"
        else
            _t_fail "C5: handq tmux session missing session-closed hook — orphan session risk"
        fi
    else
        _t_skip "C5: session-closed hook (no handq session running)"
    fi
}

run_test_mode() {
    print_step "Test suite"
    TMUX_AVAILABLE=false
    command -v tmux &>/dev/null && TMUX_AVAILABLE=true

    local exec_line; exec_line="$(_resolve_exec_line)"
    [[ -n "$exec_line" ]] && { _EXEC_LINE="$exec_line"; _PROMPT_STATE_LINE="$exec_line --prompt-state"; }

    _suite_a_static
    echo ""
    _suite_b_state_machine
    echo ""
    _suite_c_tmux

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

    configure_tmux

    echo ""
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "  ${C_BOLD}HandQ Setup — Complete${C_RESET}\n"
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "  Script dir : ${SCRIPT_DIR}\n"
    printf "  Config     : ${CONFIG_PATH}\n"
    printf "  Executable : ${INSTALLED_CMD_PATH:-<not installed>}\n"
    printf "  tmux       : %s\n" "$([[ "$TMUX_AVAILABLE" == true ]] && echo "configured" || echo "not available")"
    printf "${C_BOLD}══════════════════════════════════════════════════════════════${C_RESET}\n"
    echo ""
    info "Run 'handq' from any directory to start."
    [[ "$TMUX_AVAILABLE" == true ]] && info "HandQ state appears in your tmux status bar."
    echo ""

    run_test_mode
    local test_rc=$?

    echo ""
    if [[ $test_rc -eq 0 ]]; then
        info "Installation verified — run 'handq' to start."
    else
        warn "Installation complete but some tests failed — check output above."
    fi
}

main "$@"
