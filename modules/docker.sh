#!/bin/sh
# name: Docker
# description: Install Docker from the distro repositories and enable the service.
# os: debian ubuntu fedora alpine arch
# order: 60

case "$LEMONDX_PKG" in
    apk)    pkg_install docker docker-cli-compose; svc=docker ;;
    pacman) pkg_install docker docker-compose; svc=docker ;;
    apt)    pkg_install docker.io docker-compose-v2 || pkg_install docker.io; svc=docker ;;
    *)      pkg_install docker docker-compose || pkg_install docker; svc=docker ;;
esac

svc_enable "$svc"

# Let a bootstrapped user reach the daemon socket.
if [ -n "${USERNAME:-}" ] && getent passwd "$USERNAME" >/dev/null; then
    getent group docker >/dev/null || groupadd docker 2>/dev/null || true
    if have usermod; then usermod -aG docker "$USERNAME"; fi
    log "added '$USERNAME' to the docker group"
fi

have docker && docker --version || warn "docker installed but not on PATH yet"
