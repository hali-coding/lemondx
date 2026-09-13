#!/bin/sh
# name: SSH login
# description: Install and configure OpenSSH, create a user with passwordless sudo, and install your key for it.
# order: 20
# uses: ssh-keys
# param: USERNAME=dev  Name of the account to create
# param: SHELL_PATH=/bin/bash  Login shell
# param: PERMIT_ROOT_LOGIN=prohibit-password  sshd PermitRootLogin value (yes, no, prohibit-password)
# param: PASSWORD_AUTH=no  Allow password authentication (yes or no)

# This module used to be three (ssh-server, user, ssh-keys). Splitting them
# let you pick "just a user" or "just sshd", but a user with no way in and a
# server with no one to log in as are not useful on their own -- every real
# use of this needed all three, so a key is mandatory (`uses: ssh-keys`
# above makes the runner refuse to start without one) and there is no way to
# ask for only part of this.

[ -n "${LEMONDX_SSH_KEYS:-}" ] || die "no SSH key was selected for this run"

user="${USERNAME:-dev}"
shell="${SHELL_PATH:-/bin/bash}"

have "$shell" || shell=/bin/sh

# --- sshd --------------------------------------------------------------

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

# --- user ----------------------------------------------------------------

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

install_ssh_keys "$user"
log "user '$user' ready; ssh in with: ssh $user@<container-ip>"
