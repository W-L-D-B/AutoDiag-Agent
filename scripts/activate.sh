# Source this file: source scripts/activate.sh
_autodiag_activation_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "${AUTODIAG_VENV_DIR:-$HOME/.venvs/autodiag}/bin/activate"
cd -- "$_autodiag_activation_root"
unset _autodiag_activation_root
