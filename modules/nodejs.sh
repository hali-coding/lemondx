#!/bin/sh
# name: Node.js
# description: Install Node.js and npm from the distro repositories.
# order: 70

case "$LEMONDX_PKG" in
    apk) pkg_install nodejs npm ;;
    apt) pkg_install nodejs npm ;;
    *)   pkg_install nodejs npm ;;
esac

have node && node --version
have npm  && npm --version
