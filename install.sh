#!/bin/sh
set -eu

ARCHIVE="https://github.com/ardakooza/xernel/archive/refs/heads/main.tar.gz"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.10+ is required. On Termux, run: pkg install python" >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Xernel requires Python 3.10 or newer." >&2
    exit 1
fi
if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
    echo "curl and tar are required to install Xernel." >&2
    exit 1
fi

if [ -n "${PREFIX:-}" ] && [ -d "$PREFIX/bin" ] && [ -w "$PREFIX/bin" ]; then
    BIN_DIR="$PREFIX/bin"
else
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/xernel"
TARGET="$BIN_DIR/xernel"
mkdir -p "$INSTALL_DIR"

is_legacy_xernel_link() {
    [ -L "$TARGET" ] || return 1
    LEGACY_TARGET=$(readlink "$TARGET") || return 1
    if [ -f "$LEGACY_TARGET" ]; then
        grep -q 'Launch the shared Xernel CLI directly from a source checkout' "$LEGACY_TARGET"
        return $?
    fi
    case "$LEGACY_TARGET" in
        */xernel/xernel) ;;
        *) return 1 ;;
    esac
    [ ! -e "$LEGACY_TARGET" ]
}

is_xernel_package_launcher() {
    [ -f "$TARGET" ] || return 1
    grep -Eq 'from xernel\.core import main|xernel\.core:main' "$TARGET"
}

REPLACE_TARGET=0
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if [ -L "$TARGET" ] && [ "$(readlink "$TARGET")" = "$INSTALL_DIR/venv/bin/xernel" ]; then
        :
    elif [ -f "$TARGET" ] && grep -q 'Xernel managed launcher' "$TARGET"; then
        :
    elif is_legacy_xernel_link; then
        REPLACE_TARGET=1
    elif is_xernel_package_launcher; then
        REPLACE_TARGET=1
    else
        echo "Refusing to replace an existing command: $TARGET" >&2
        echo "Remove it yourself, then rerun this installer." >&2
        exit 1
    fi
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM
echo "Downloading Xernel from GitHub..."
curl -fsSL "$ARCHIVE" -o "$TEMP_DIR/xernel.tar.gz"
tar -xzf "$TEMP_DIR/xernel.tar.gz" -C "$TEMP_DIR"
SOURCE_DIR="$TEMP_DIR/xernel-main"

if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/python" -m pip install --disable-pip-version-check --upgrade --force-reinstall "$SOURCE_DIR"

if [ -L "$TARGET" ]; then
    ln -sfn "$INSTALL_DIR/venv/bin/xernel" "$TARGET.new"
    mv -f "$TARGET.new" "$TARGET"
elif [ "$REPLACE_TARGET" -eq 1 ] || [ ! -e "$TARGET" ]; then
    WRAPPER="$TARGET.new"
    cat > "$WRAPPER" <<EOF
#!/bin/sh
# Xernel managed launcher
exec "$INSTALL_DIR/venv/bin/python" -m xernel.cli "\$@"
EOF
    chmod 755 "$WRAPPER"
    mv "$WRAPPER" "$TARGET"
fi

echo "Xernel installed at $TARGET"
case ":$PATH:" in
    *":$BIN_DIR:"*) echo "Run it with: xernel" ;;
    *) echo "Add $BIN_DIR to PATH, then run: xernel" ;;
esac
