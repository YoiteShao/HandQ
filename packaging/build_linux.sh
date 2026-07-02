#!/bin/bash
set -e  # Exit on error

# ── Project root (absolute, script-location-relative) ────────────────────────
# Derive the project root from the script's own location so that paths like
# --include-data-dir are correct regardless of the caller's working directory.
# The script lives in <project_root>/packaging/, so PROJECT_ROOT is one level up.
PACKAGING_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "${PACKAGING_DIR}/.." && pwd )"

# ── Interpreter ──────────────────────────────────────────────────────────────
# Allow the caller to override which Python is used.  Defaults to python3.
# Example: PYTHON=python3.10 ./build_cross_platform_optimized.sh linux
PYTHON="${PYTHON:-python3}"

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
print_perf()    { echo -e "${CYAN}[PERF]${NC} $1"; }

# ── Detect optimal parallel job count ────────────────────────────────────────
# Optimization #2: use physical core count rather than logical (hyperthreaded)
# thread count.  GCC is memory-bandwidth-bound; too many parallel jobs cause
# cache thrashing and can actually slow down the build.
_detect_jobs() {
    if [ -n "${JOBS:-}" ]; then
        echo "$JOBS"
        return
    fi
    # Try to get physical core count via lscpu
    local physical
    physical=$(lscpu -p=Core,Socket 2>/dev/null | grep -v '^#' | sort -u | wc -l)
    if [ "${physical:-0}" -gt 0 ] 2>/dev/null; then
        # Cap at 16 to avoid excessive memory pressure on large machines
        echo $(( physical > 16 ? 16 : physical ))
    else
        # Fallback: logical cores, capped at 16
        local logical
        logical=$(nproc 2>/dev/null || echo 4)
        echo $(( logical > 16 ? 16 : logical ))
    fi
}
NUITKA_JOBS=$(_detect_jobs)

# ── Verbose flags (gated) ─────────────────────────────────────────────────────
# Optimization #3: --show-progress and --show-memory add I/O overhead.
# Only enable them when NUITKA_VERBOSE=1 is explicitly set.
VERBOSE_FLAGS=""
if [ "${NUITKA_VERBOSE:-0}" = "1" ]; then
    VERBOSE_FLAGS="--show-progress --show-memory"
    print_info "Verbose mode enabled (NUITKA_VERBOSE=1)"
fi

# ── ccache setup ─────────────────────────────────────────────────────────────
# Optimization #1: Use ccache to cache compiled C objects.
# On a second build where only one Python file changed, all unchanged modules
# are served from cache in milliseconds instead of being recompiled.
_setup_ccache() {
    if command -v ccache &>/dev/null; then
        # Use Nuitka's native ccache integration (avoids Scons 'ccache gcc' version-detection failure)
        export NUITKA_CCACHE_BINARY="$(command -v ccache)"
        # Ensure a generous cache size (10 GB)
        ccache --set-config=max_size=10G 2>/dev/null || true
        ccache --set-config=compression=true 2>/dev/null || true
        print_perf "ccache enabled: $(ccache --get-config=cache_dir 2>/dev/null || echo 'default cache dir')"
        print_perf "ccache stats before build:"
        ccache --show-stats 2>/dev/null | grep -E 'cache hit|cache miss|files in cache|cache size' | \
            sed 's/^/         /' || true
    else
        print_warning "ccache not found — install it for 3-10× faster incremental builds"
        print_warning "  Ubuntu/Debian: sudo apt-get install ccache"
    fi
}

# ── Print ccache stats after build ───────────────────────────────────────────
_print_ccache_stats() {
    if command -v ccache &>/dev/null; then
        print_perf "ccache stats after build:"
        ccache --show-stats 2>/dev/null | grep -E 'cache hit|cache miss|files in cache|cache size' | \
            sed 's/^/         /' || true
    fi
}

# ── Common --nofollow-import-to flags ─────────────────────────────────────────
# Optimization #7: exclude stdlib modules and unused framework sub-packages
# that are never needed in a CLI/TUI application.  Each exclusion reduces the
# number of C files Nuitka must generate and compile.
NOFOLLOW_COMMON=(
    # Original exclusions (keep)
    "--nofollow-import-to=pytest"
    "--nofollow-import-to=tests"
    "--nofollow-import-to=setuptools"
    "--nofollow-import-to=pip"
    "--nofollow-import-to=wheel"

    # GUI toolkits — never needed in a terminal app
    "--nofollow-import-to=tkinter"
    "--nofollow-import-to=turtle"
    "--nofollow-import-to=idlelib"
    "--nofollow-import-to=wx"
    "--nofollow-import-to=PyQt5"
    "--nofollow-import-to=PyQt6"
    "--nofollow-import-to=PySide2"
    "--nofollow-import-to=PySide6"

    # Testing / debugging infrastructure
    # NOTE: unittest must NOT be excluded — anthropic SDK (and deps like httpx/anyio)
    # import unittest.mock in production code paths.  Excluding it causes Nuitka's
    # hard-import assertion to call abort() at runtime → "Fatal Python error: Aborted".
    # "--nofollow-import-to=unittest"   # DO NOT RE-ADD
    # "--nofollow-import-to=doctest"    # keep commented: may also be a hard import
    "--nofollow-import-to=pdb"
    "--nofollow-import-to=pdbpp"
    "--nofollow-import-to=profile"
    "--nofollow-import-to=cProfile"
    "--nofollow-import-to=pstats"
    "--nofollow-import-to=timeit"
    "--nofollow-import-to=trace"

    # Package management / installation tools
    "--nofollow-import-to=distutils"
    "--nofollow-import-to=ensurepip"
    "--nofollow-import-to=venv"
    "--nofollow-import-to=lib2to3"

    # Legacy / rarely-used stdlib network modules
    "--nofollow-import-to=xmlrpc"
    "--nofollow-import-to=ftplib"
    "--nofollow-import-to=imaplib"
    "--nofollow-import-to=poplib"
    "--nofollow-import-to=smtplib"
    "--nofollow-import-to=telnetlib"
    "--nofollow-import-to=nntplib"

    # Audio (not needed in a text-based tool)
    "--nofollow-import-to=sndhdr"
    "--nofollow-import-to=sunau"
    "--nofollow-import-to=aifc"
    "--nofollow-import-to=audioop"

    # Rich sub-module not needed in terminal context

    # IPython / notebook ecosystem (often pulled in transitively)
    "--nofollow-import-to=IPython"
    "--nofollow-import-to=ipykernel"
    "--nofollow-import-to=notebook"
    "--nofollow-import-to=nbformat"
    "--nofollow-import-to=nbconvert"

    # Windows-only: import gated by `if _IS_WINDOWS:` in tool_registry.py,
    # so this module is never reachable at runtime on Linux; skip compilation.
    "--nofollow-import-to=src.tools.remote_handq_tool"
)

# ── Common Nuitka include flags ───────────────────────────────────────────────
# These are identical for Linux and Windows builds.
INCLUDE_COMMON=(
    "--include-package=src"
    "--include-package=yaml"
    # rich is kept although handq_linux.py has no TUI: --include-package=src
    # still compiles src/ui/status_tui.py (the only rich consumer), so dropping
    # rich would break the build for no meaningful size win.
    "--include-package=rich"
    "--include-package=json_repair"
    # QGenie SDK temporarily disabled — uncomment when re-enabling qgenie support
    # "--include-module=qgenie_service"
    # "--include-package=qgenie"
    # "--include-package=pydantic"
    "--include-package=anthropic"       # AnthropicStreamingService (src/infrastructure/)
    "--include-package=httpx"           # dep of anthropic SDK
    "--include-package=paramiko"        # SSHTool (src/tools/ssh_tool.py)
    "--include-package=keyring"         # SSH credential storage (src/tools/ssh_tool.py)
    "--include-package=keyrings"        # keyrings.alt: file-based backend for headless Linux envs
    "--include-package=cffi"            # required by cryptography (paramiko dep); must be explicit
    "--include-package=cryptography"    # required by paramiko for SSH crypto
    "--include-package=pdfplumber"      # ReadTool: top-level import — must bundle or daemon start fails
    "--include-package=pdfminer"        # pdfplumber core dep (pdfminer.six → pdfminer package)
    "--include-package=PIL"             # pdfplumber dep (Pillow)
    "--include-package=pypdfium2"       # pdfplumber dep (required >= 0.10.0); remove if not installed
    "--include-package=chardet"         # pdfminer.six encoding dep
    # Note: ./handq_config.yaml is NOT embedded — it lives at the dist root
    # so users can edit it directly before running handq_setup.sh.
)

# ── Prerequisites check ───────────────────────────────────────────────────────
check_prerequisites() {
    print_info "Checking prerequisites..."

    # Python
    if ! command -v "$PYTHON" &>/dev/null; then
        print_error "Python interpreter not found: $PYTHON"
        exit 1
    fi
    PYTHON_VERSION=$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
    print_info "Python interpreter: $PYTHON ($PYTHON_VERSION)"

    # Nuitka
    if ! "$PYTHON" -c "import nuitka" &>/dev/null; then
        print_error "Nuitka is not installed. Install it with: pip install nuitka"
        exit 1
    fi
    NUITKA_VERSION=$("$PYTHON" -m nuitka --version 2>&1 | head -1)
    print_success "Nuitka: $NUITKA_VERSION"

    # GCC
    if ! command -v gcc &>/dev/null; then
        print_warning "GCC not found — Linux builds will fail."
        print_info "  Install: sudo apt-get install gcc python3-dev"
    else
        print_success "GCC: $(gcc --version | head -1)"
    fi

    # patchelf (required for standalone mode on Linux)
    if ! command -v patchelf &>/dev/null; then
        print_error "patchelf is not installed (required for Nuitka standalone on Linux)."
        print_info "  Ubuntu/Debian: sudo apt-get install patchelf"
        exit 1
    fi
    print_success "patchelf: $(patchelf --version 2>&1 | head -1)"

    # ccache (optional but highly recommended)
    _setup_ccache

    print_info "Parallel jobs: $NUITKA_JOBS (physical cores, capped at 16)"
}


# ── Linux build ───────────────────────────────────────────────────────────────
build_linux() {
    print_info "============================================================================"
    print_info "Building Linux executable (optimized)..."
    print_info "============================================================================"

    # Detect GLIBC version
    GLIBC_VERSION=$(ldd --version | head -n1 | grep -oP '\d+\.\d+$' || echo "unknown")
    print_info "GLIBC version: $GLIBC_VERSION"

    OUTPUT_DIR="linux-glibc${GLIBC_VERSION}"
    # Nuitka intermediate files (*.build, *.dist, *.onefile-build) go here
    BUILD_CACHE_DIR="dist/.nuitka_cache"
    # Final distributable package: use DIST_OUT if set, otherwise default to dist/$OUTPUT_DIR
    if [ -n "${DIST_OUT:-}" ]; then
        DIST_DIR="${DIST_OUT}"
    else
        DIST_DIR="dist/$OUTPUT_DIR"
    fi

    # ── Incremental build support ─────────────────────────────────────────────
    # Only wipe the build cache when CLEAN_BUILD=1.  Nuitka reuses .c/.o files
    # for unchanged modules; ccache serves cached objects on top of that.
    if [ "${CLEAN_BUILD:-0}" = "1" ]; then
        print_info "CLEAN_BUILD=1 — removing build cache..."
        rm -rf "../${BUILD_CACHE_DIR}"
    else
        print_info "Incremental build (set CLEAN_BUILD=1 to force a full rebuild)"
    fi
    mkdir -p "../${BUILD_CACHE_DIR}" "../${DIST_DIR}"
    print_info "Build cache  : ${BUILD_CACHE_DIR}  (intermediate Nuitka files)"
    print_info "Dist package : ${DIST_DIR}  (final distributable)"

    print_info "Starting Nuitka compilation for Linux (standalone)..."
    print_info "Parallel jobs: $NUITKA_JOBS"
    print_warning "First build: 5–15 min.  Subsequent builds with ccache: 1–3 min."

    BUILD_START=$(date +%s)

    # Strip /usr/lib/ccache from PATH so Nuitka's Scons backend resolves the
    # real gcc, not the ccache wrapper.  The ccache wrapper causes Scons to
    # read "ccache gcc" as the compiler version string → FATAL version-detect
    # failure.  Nuitka uses ccache via NUITKA_CCACHE_BINARY instead (set above).
    export PATH="$(echo "$PATH" | tr ':' '\n' | grep -v '/usr/lib/ccache' | tr '\n' ':' | sed 's/:$//')"

    cd ..
    "$PYTHON" -m nuitka \
        --standalone \
        --assume-yes-for-downloads \
        $VERBOSE_FLAGS \
        --jobs="$NUITKA_JOBS" \
        --output-dir="${BUILD_CACHE_DIR}" \
        \
        "${INCLUDE_COMMON[@]}" \
        \
        "${NOFOLLOW_COMMON[@]}" \
        \
        --python-flag=no_site \
        \
        --disable-plugin=anti-bloat \
        \
        --lto=auto \
        \
        handq_linux.py

    BUILD_RESULT=$?
    BUILD_END=$(date +%s)
    ELAPSED=$(( BUILD_END - BUILD_START ))
    print_perf "Linux Nuitka compilation took ${ELAPSED}s ($(( ELAPSED / 60 ))m $(( ELAPSED % 60 ))s)"
    _print_ccache_stats

    if [ $BUILD_RESULT -ne 0 ]; then
        print_error "Linux Nuitka compilation failed!"
        cd packaging
        return 1
    fi

    # Standalone mode: Nuitka produces handq_linux.dist/ directory
    STANDALONE_SRC="${BUILD_CACHE_DIR}/handq_linux.dist"
    BIN_PATH="${DIST_DIR}/handq_linux.dist/handq_linux.bin"

    if [ ! -d "$STANDALONE_SRC" ]; then
        print_error "Expected standalone dist dir not found: $STANDALONE_SRC"
        cd packaging
        return 1
    fi

    # Copy the entire standalone dist directory into the clean dist dir
    cp -r "$STANDALONE_SRC" "${DIST_DIR}/handq_linux.dist"

    # ── Assemble the distributable package ───────────────────────────────────
    # dist/$OUTPUT_DIR/ structure (user-visible):
    #   handq_config.yaml    ← users edit this first (the bare binary auto-loads
    #                          it from here; handq_setup.sh also passes it as
    #                          --config to the installed handq_linux command)
    #   handq_setup.sh       ← users run this to install the handq_linux command
    #   handq_linux.dist/    ← binary + all C extensions/deps (don't edit)
    print_info "Assembling distributable package in ${DIST_DIR}/..."
    cp "${PROJECT_ROOT}/handq_setup.sh" "${DIST_DIR}/handq_setup.sh"
    chmod +x "${DIST_DIR}/handq_setup.sh"
    # Config at top level — easy for users to find and edit before running setup
    cp "${PROJECT_ROOT}/handq_config.example.yaml" "${DIST_DIR}/handq_config.yaml"

    print_info "Package contents:"
    ls -lh "${DIST_DIR}/handq_config.yaml" "${DIST_DIR}/handq_setup.sh"
    echo "  handq_linux.dist/  ($(du -sh "${DIST_DIR}/handq_linux.dist" | cut -f1) standalone binary + deps)"

    print_success "Linux build completed successfully!"
    print_info "============================================================================"
    print_info "Dist package : ${DIST_DIR}/"
    print_info "  handq_linux.dist/  — standalone binary + all dependencies"
    print_info "  handq_setup.sh     — setup/install script"
    print_info "  handq_config.yaml"
    print_info ""
    print_info "Build cache  : ${BUILD_CACHE_DIR}/  (intermediate files, not for distribution)"
    print_info "Build time   : ${ELAPSED}s"
    print_info ""
    print_warning "IMPORTANT: This build requires GLIBC >= $GLIBC_VERSION on the target system."
    print_warning "For broader compatibility, build inside an Ubuntu 20.04 Docker container."
    print_info ""
    print_info "To package for distribution:"
    print_info "  tar -czf handq-linux-glibc${GLIBC_VERSION}.tar.gz -C \"$(dirname "${DIST_DIR}")\" \"$(basename "${DIST_DIR}")\""
    print_info "============================================================================"

    cd packaging
    return 0
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    # Parse arguments
    while getopts ":o:" opt; do
        case $opt in
            o) DIST_OUT="$OPTARG" ;;
            \?) print_error "Unknown option: -$OPTARG"; exit 1 ;;
            :)  print_error "Option -$OPTARG requires an argument."; exit 1 ;;
        esac
    done

    echo "============================================================================"
    echo "HandQ Linux Build Script"
    echo "============================================================================"
    echo ""
    echo "  Python interpreter : $PYTHON"
    echo "  Parallel jobs      : $NUITKA_JOBS"
    echo "  Incremental build  : $([ "${CLEAN_BUILD:-0}" = "1" ] && echo 'NO (CLEAN_BUILD=1)' || echo 'YES')"
    echo "  Verbose output     : $([ "${NUITKA_VERBOSE:-0}" = "1" ] && echo 'YES' || echo 'NO')"
    [ -n "${DIST_OUT:-}" ] && echo "  Output path        : $DIST_OUT"
    echo ""

    check_prerequisites
    build_linux

    OVERALL_END=$(date +%s)

    echo ""
    print_info "============================================================================"
    print_success "Build completed!"
    print_info "============================================================================"
    echo ""
    print_info "Next steps:"
    print_info "  1. Edit dist/linux-glibc${GLIBC_VERSION}/handq_config.yaml"
    print_info "  2. tar -czf handq-linux-glibc${GLIBC_VERSION}.tar.gz -C dist linux-glibc${GLIBC_VERSION}"
    print_info "  3. On target: tar xzf handq-linux-glibc${GLIBC_VERSION}.tar.gz && bash linux-glibc${GLIBC_VERSION}/handq_setup.sh"
    echo ""
}

# Run main
main "$@"
