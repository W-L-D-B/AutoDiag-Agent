#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: wsl -d AutoDiagUbuntu -u root -- bash /mnt/d/Code/mi/AutoDiag_Agent/scripts/bootstrap-ubuntu.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
apt-get update
apt-get install -y --no-install-recommends \
  build-essential cmake ninja-build gdb git openssh-client ca-certificates curl \
  python3 python3-venv python3-pip python3-dev \
  can-utils iproute2 kmod pkg-config clang-format clang-tidy \
  unzip zip jq ripgrep sudo libgtest-dev nlohmann-json3-dev gcovr

if ! id autodiag >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash autodiag
fi
usermod -aG sudo autodiag

# Limit passwordless sudo to the two project-owned virtual-CAN operations.
install -m 0755 "$project_root/scripts/autodiag-vcan" /usr/local/sbin/autodiag-vcan
printf '%s\n' 'autodiag ALL=(root) NOPASSWD: /usr/local/sbin/autodiag-vcan up, /usr/local/sbin/autodiag-vcan down' \
  > /etc/sudoers.d/autodiag-vcan
chmod 0440 /etc/sudoers.d/autodiag-vcan
visudo -cf /etc/sudoers.d/autodiag-vcan
install -m 0644 "$project_root/scripts/autodiag-vcan.service" /etc/systemd/system/autodiag-vcan.service
systemctl daemon-reload
systemctl enable --now autodiag-vcan.service

install -d -o autodiag -g autodiag /home/autodiag/projects /home/autodiag/.venvs
project_link=/home/autodiag/projects/AutoDiag_Agent
if [ -L "$project_link" ]; then
  ln -sfnT "$project_root" "$project_link"
elif [ ! -e "$project_link" ]; then
  ln -s "$project_root" "$project_link"
else
  echo "Project link is occupied by a regular file or directory: $project_link" >&2
  exit 1
fi
chown -h autodiag:autodiag "$project_link"
python3 "$project_root/scripts/configure-wsl.py"

echo "System dependencies installed. Set the distro default user to autodiag from Windows."
