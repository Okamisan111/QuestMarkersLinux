#!/usr/bin/env bash
# Build the native Wayland layer-shell presenter.
#
# Needs: gcc, wayland-client, wayland-scanner, and the wlr-layer-shell
# protocol XML (vendored here as wlr-layer-shell-unstable-v1.xml).
# xdg-shell.xml is taken from the Qt6 tree when wayland-protocols is absent.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f wlr-layer-shell-client-protocol.h ]; then
    wayland-scanner client-header \
        wlr-layer-shell-unstable-v1.xml wlr-layer-shell-client-protocol.h
    wayland-scanner private-code \
        wlr-layer-shell-unstable-v1.xml wlr-layer-shell-protocol.c
fi

XDG_XML="${XDG_XML:-/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml}"
[ -f "$XDG_XML" ] || XDG_XML=/usr/share/qt6/wayland/protocols/xdg-shell/xdg-shell.xml

if [ ! -f xdg-shell-client-protocol.h ]; then
    wayland-scanner client-header "$XDG_XML" xdg-shell-client-protocol.h
    wayland-scanner private-code "$XDG_XML" xdg-shell-protocol.c
fi

gcc -O2 -Wall -Wextra -o wayland_present \
    wayland_present.c wlr-layer-shell-protocol.c xdg-shell-protocol.c \
    $(pkg-config --cflags --libs wayland-client)

echo "built: $(pwd)/wayland_present"
