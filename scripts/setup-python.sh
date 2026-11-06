#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${AUTODIAG_VENV_DIR:-$HOME/.venvs/autodiag}"
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install --upgrade pip
requirements_file="$project_root/requirements-dev.txt"
if [ -f "$project_root/requirements-dev.lock" ]; then
  requirements_file="$project_root/requirements-dev.lock"
fi
"$venv_dir/bin/python" -m pip install -r "$requirements_file"
"$venv_dir/bin/python" -m pip check
echo "Activate: source $venv_dir/bin/activate"
