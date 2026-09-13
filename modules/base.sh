#!/bin/sh
# name: Base tools
# description: Refresh the package index and install curl, CA certificates, sudo and a text editor.
# order: 10

pkg_install ca-certificates curl tmux

# Package names differ a little between families.
case "$LEMONDX_PKG" in
    apk)    pkg_install sudo vim tzdata bash ;;
    pacman) pkg_install sudo vim ;;
    *)      pkg_install sudo vim ;;
esac

log "base tools ready on $LEMONDX_OS_ID"
