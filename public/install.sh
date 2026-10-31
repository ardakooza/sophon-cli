#!/bin/sh
set -eu

ARCHIVE="https://github.com/ardakooza/sophon-cli/archive/refs/heads/main.tar.gz"
VERSION_URL="https://raw.githubusercontent.com/ardakooza/sophon-cli/main/src/sophon/VERSION"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10+ is required. On Termux, run: pkg install python" >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Sophon requires Python 3.10 or newer." >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
    echo "curl and tar are required to install Sophon." >&2
    exit 1
fi

if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ] && [ -w "$PREFIX/bin" ]; then
    BIN_DIR="$PREFIX/bin"
else
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/sophon"
TARGET="$BIN_DIR/sophon"
LEGACY_TARGET="$BIN_DIR/xernel"
LEGACY_INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/xernel"
mkdir -p "$INSTALL_DIR"

is_legacy_sophon_link() {
    [ -L "$TARGET" ] || return 1
    LEGACY_TARGET=$(readlink "$TARGET") || return 1
    if [ -f "$LEGACY_TARGET" ]; then
        grep -q 'Launch the shared Sophon CLI directly from a source checkout' "$LEGACY_TARGET"
        return $?
    fi
    case "$LEGACY_TARGET" in
        */sophon/sophon) ;;
        *) return 1 ;;
    esac
    [ ! -e "$LEGACY_TARGET" ]
}

is_sophon_package_launcher() {
    [ -f "$TARGET" ] || return 1
    grep -Eq 'from sophon\.core import main|sophon\.core:main' "$TARGET"
}

is_managed_legacy_xernel() {
    if [ -L "$LEGACY_TARGET" ]; then
        LEGACY_LINK=$(readlink "$LEGACY_TARGET") || return 1
        case "$LEGACY_LINK" in
            "$LEGACY_INSTALL_DIR/venv/bin/xernel") return 0 ;;
            */xernel/xernel) [ ! -e "$LEGACY_LINK" ] && return 0 ;;
        esac
    fi
    if [ -f "$LEGACY_TARGET" ]; then
        grep -Eq 'Xernel managed launcher|from xernel\.core import main|xernel\.core:main|Launch the shared Xernel CLI directly from a source checkout' "$LEGACY_TARGET"
        return $?
    fi
    return 1
}

remove_managed_legacy_xernel() {
    if is_managed_legacy_xernel; then
        rm -f "$LEGACY_TARGET"
    fi
}

REPLACE_TARGET=0
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if [ -L "$TARGET" ] && [ "$(readlink "$TARGET")" = "$INSTALL_DIR/venv/bin/sophon" ]; then
        :
    elif [ -f "$TARGET" ] && grep -q 'Sophon managed launcher' "$TARGET"; then
        :
    elif is_legacy_sophon_link; then
        REPLACE_TARGET=1
    elif is_sophon_package_launcher; then
        REPLACE_TARGET=1
    else
        echo "Refusing to replace an existing command: $TARGET" >&2
        echo "Remove it yourself, then rerun this installer." >&2
        exit 1
    fi
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM
echo "Checking Sophon version..."
REMOTE_VERSION=$(curl -fsSL --max-time 10 "$VERSION_URL" 2>/dev/null || true)
MANAGED_INSTALL=0
if [ -x "$INSTALL_DIR/venv/bin/python" ]; then
    if [ -L "$TARGET" ] && [ "$(readlink "$TARGET")" = "$INSTALL_DIR/venv/bin/sophon" ]; then
        MANAGED_INSTALL=1
    elif [ -f "$TARGET" ] && grep -q 'Sophon managed launcher' "$TARGET"; then
        MANAGED_INSTALL=1
    fi
fi
if [ "$MANAGED_INSTALL" -eq 1 ] && [ -n "$REMOTE_VERSION" ]; then
    INSTALLED_VERSION=$("$INSTALL_DIR/venv/bin/python" -c 'from sophon import __version__; print(__version__)' 2>/dev/null || true)
    if [ "$INSTALLED_VERSION" = "$REMOTE_VERSION" ]; then
        remove_managed_legacy_xernel
        echo "Sophon is already up to date ($REMOTE_VERSION)."
        exit 0
    fi
fi

echo "Downloading latest Sophon..."
curl -fsSL "$ARCHIVE" -o "$TEMP_DIR/sophon.tar.gz"
tar -xzf "$TEMP_DIR/sophon.tar.gz" -C "$TEMP_DIR"
SOURCE_DIR="$TEMP_DIR/sophon-cli-main"

if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
PIP_LOG="$TEMP_DIR/pip.log"
PIP_STATUS_FILE="$TEMP_DIR/pip.status"
if [ -t 1 ]; then
    echo "Installing Sophon..."
    (
        if "$INSTALL_DIR/venv/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade --force-reinstall "$SOURCE_DIR" >"$PIP_LOG" 2>&1; then
            status=0
        else
            status=$?
        fi
        printf '%s\n' "$status" > "$PIP_STATUS_FILE"
    ) &
    PIP_PID=$!
    SPIN=0
    while [ ! -f "$PIP_STATUS_FILE" ]; do
        case $((SPIN % 4)) in
            0) FRAME='|' ;;
            1) FRAME='/' ;;
            2) FRAME='-' ;;
            3) FRAME='\\' ;;
        esac
        printf '\r  %s Working...' "$FRAME"
        SPIN=$((SPIN + 1))
        sleep 0.12
    done
    wait "$PIP_PID" || true
    PIP_STATUS=$(cat "$PIP_STATUS_FILE")
    printf '\r\033[2K'
else
    echo "Installing Sophon..."
    if "$INSTALL_DIR/venv/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade --force-reinstall "$SOURCE_DIR" >"$PIP_LOG" 2>&1; then
        PIP_STATUS=0
    else
        PIP_STATUS=$?
    fi
fi
if [ "$PIP_STATUS" -ne 0 ]; then
    echo "Sophon installation failed. Details:" >&2
    cat "$PIP_LOG" >&2
    exit "$PIP_STATUS"
fi

if [ -L "$TARGET" ]; then
    ln -sfn "$INSTALL_DIR/venv/bin/sophon" "$TARGET.new"
    mv -f "$TARGET.new" "$TARGET"
elif [ "$REPLACE_TARGET" -eq 1 ] || [ ! -e "$TARGET" ]; then
    WRAPPER="$TARGET.new"
    cat > "$WRAPPER" <<EOF
#!/bin/sh
# Sophon managed launcher
exec "$INSTALL_DIR/venv/bin/python" -m sophon.cli "\$@"
EOF
    chmod 755 "$WRAPPER"
    mv "$WRAPPER" "$TARGET"
fi

remove_managed_legacy_xernel

if [ -n "$REMOTE_VERSION" ]; then
    echo "Sophon $REMOTE_VERSION installed. Run 'sophon' to start."
else
    echo "Sophon installed. Run 'sophon' to start."
fi
case ":$PATH:" in
    *":$BIN_DIR:"*) echo "Run it with: sophon" ;;
    *) echo "Add $BIN_DIR to PATH, then run: sophon" ;;
esac
