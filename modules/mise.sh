#!/bin/sh
# name: mise
# description: Install mise system-wide with btop, BOOTSTRAP=true in its environment, and an optional /etc/mise/config.toml.
# order: 80
# text: MISE_CONFIG=  Contents of /etc/mise/config.toml (blank leaves the file alone)

have curl || pkg_install curl ca-certificates

# The installer defaults to ~/.local/bin of whoever runs it, which here is
# root; /usr/local/bin puts it on every user's PATH.
log "installing mise"
curl -fsSL https://mise.run | MISE_INSTALL_PATH=/usr/local/bin/mise sh
mise --version

# What this module always sets goes in conf.d, which mise merges with
# config.toml, so a config.toml supplied below replaces none of it.
mkdir -p /etc/mise/conf.d
cat > /etc/mise/conf.d/lemondx.toml <<'EOF'
[tools]
btop = "latest"

[env]
BOOTSTRAP = "true"
EOF

if [ -n "${MISE_CONFIG:-}" ]; then
    log "writing /etc/mise/config.toml"
    printf '%s\n' "$MISE_CONFIG" > /etc/mise/config.toml
fi
chmod 644 /etc/mise/conf.d/lemondx.toml /etc/mise/config.toml 2>/dev/null || true

# mise only applies [env] in a shell it has been activated in.
cat > /etc/profile.d/mise.sh <<'EOF'
if [ -n "${BASH_VERSION:-}" ] && command -v mise >/dev/null 2>&1; then
    eval "$(mise activate bash)"
elif [ -n "${ZSH_VERSION:-}" ] && command -v mise >/dev/null 2>&1; then
    eval "$(mise activate zsh)"
fi
EOF

# --system puts the install under /usr/local/share/mise, which every user's
# mise reads, rather than root's own data directory. Its sudo is for a user
# publishing there; this runs as root and minimal images have no sudo.
log "installing tools"
MISE_SYSTEM_PACKAGES_SUDO=false mise install --system --yes
