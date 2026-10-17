#!/bin/sh
# Install (not register) the GitHub Actions runner as user ci on a test VM.
#
#   install-runner.sh <ip-last-octet>
#
# Registration is a separate, deliberate step (see SKILL.md): the repo is
# public, so a registered runner is reachable by fork PRs.
set -eu
HOST=${PVE_HOST:-root@sysvmbox5}
ip=${PVE_NET:-192.168.29}.$1
ver=$(gh api repos/actions/runner/releases/latest --jq .tag_name | sed 's/^v//')
ssh -o BatchMode=yes -J "$HOST" "ci@$ip" "set -e
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -sSfL https://github.com/actions/runner/releases/download/v$ver/actions-runner-linux-x64-$ver.tar.gz | tar xz
sudo ./bin/installdependencies.sh >/tmp/runner-deps.log 2>&1 || { tail -5 /tmp/runner-deps.log; exit 1; }
sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q -o Dpkg::Lock::Timeout=300 install git python3 curl >/dev/null
echo \"runner $ver installed in ~/actions-runner on \$(hostname)\""
