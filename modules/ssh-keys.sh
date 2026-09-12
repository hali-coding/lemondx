#!/bin/sh
# name: SSH authorized keys
# description: Install the selected public keys into a user's authorized_keys.
# order: 30
# uses: ssh-keys
# param: TARGET_USER=root  Account that should accept these keys

user="${TARGET_USER:-root}"

if [ -z "${LEMONDX_SSH_KEYS:-}" ]; then
    die "no SSH keys were selected for this run"
fi

# The account may have been created by an earlier module in the same run.
if ! getent passwd "$user" >/dev/null; then
    die "user '$user' does not exist -- run the 'user account' module first, or set TARGET_USER"
fi

install_ssh_keys "$user"
