# Source this file: source scripts/activate.sh
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "${AUTODIAG_VENV_DIR:-$HOME/.venvs/autodiag}/bin/activate"
cd -- "$project_root"
unset project_root
