#!/bin/sh
# name: SSH server
# description: Install OpenSSH and start it, so you can ssh into the container.
# order: 20
# param: PERMIT_ROOT_LOGIN=prohibit-password  sshd PermitRootLogin value (yes, no, prohibit-password)
# param: PASSWORD_AUTH=no  Allow password authentication (yes or no)

case "$LEMONDX_PKG" in
    apk) pkg_install openssh; svc="sshd" ;;
    apt) pkg_install openssh-server; svc="ssh" ;;
    *)   pkg_install openssh-server || pkg_install openssh; svc="sshd" ;;
esac

# Alpine's package does not generate host keys on install.
if have ssh-keygen && [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    log "generating host keys"
    ssh-keygen -A
fi

config=/etc/ssh/sshd_config
if [ -f "$config" ]; then
    # Replace the directive if present, otherwise append it.
    set_directive() {
        local key="$1" value="$2"
        if grep -qE "^[#[:space:]]*${key}[[:space:]]" "$config"; then
            sed -i "s|^[#[:space:]]*${key}[[:space:]].*|${key} ${value}|" "$config"
        else
            printf '%s %s\n' "$key" "$value" >> "$config"
        fi
    }
    set_directive PermitRootLogin "${PERMIT_ROOT_LOGIN:-prohibit-password}"
    set_directive PasswordAuthentication "${PASSWORD_AUTH:-no}"
fi

svc_enable "$svc"
log "sshd listening; reach it at the container's IP on port 22"
