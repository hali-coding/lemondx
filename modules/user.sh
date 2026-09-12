#!/bin/sh
# name: User account
# description: Create a login user with passwordless sudo, and give it the selected SSH keys.
# order: 25
# uses: ssh-keys
# param: USERNAME=dev  Name of the account to create
# param: SHELL_PATH=/bin/bash  Login shell

user="${USERNAME:-dev}"
shell="${SHELL_PATH:-/bin/bash}"

have "$shell" || shell=/bin/sh

if getent passwd "$user" >/dev/null; then
    log "user '$user' already exists"
else
    log "creating user '$user'"
    if have useradd; then
        useradd --create-home --shell "$shell" "$user"
    elif have adduser; then      # busybox / alpine
        adduser -D -s "$shell" "$user"
    else
        die "no useradd or adduser available"
    fi
fi

# Add to whichever admin group this distro uses.
for group in sudo wheel adm; do
    if getent group "$group" >/dev/null; then
        if have usermod; then usermod -aG "$group" "$user"
        elif have addgroup; then addgroup "$user" "$group"
        fi
        log "added '$user' to '$group'"
        break
    fi
done

if [ -d /etc/sudoers.d ]; then
    printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$user" > "/etc/sudoers.d/90-$user"
    chmod 440 "/etc/sudoers.d/90-$user"
    log "granted passwordless sudo"
fi

# This account logs in by key, so it gets no usable password. Set the shadow
# field to "*" (disabled) rather than "!" (locked): OpenSSH built without PAM,
# as on Alpine, refuses a locked account outright -- "User ... not allowed
# because account is locked" -- even for public-key authentication.
if have usermod; then
    usermod -p '*' "$user"
elif have chpasswd; then
    printf '%s:*\n' "$user" | chpasswd -e
else
    warn "could not disable the password for '$user'"
fi

[ -n "${LEMONDX_SSH_KEYS:-}" ] && install_ssh_keys "$user"
log "user '$user' ready"
