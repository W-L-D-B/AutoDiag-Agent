#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_root"
source scripts/activate.sh
mkdir -p artifacts/qa

for preset in linux-debug linux-sanitize linux-coverage; do
  cmake --preset "$preset"
  cmake --build --preset "$preset" --parallel 2
  if [ "$preset" = linux-coverage ]; then
    coverage_dir="$HOME/.cache/autodiag/build/coverage"
    while IFS= read -r profile; do
      case "$profile" in
        "$coverage_dir"/*.gcda) rm -- "$profile" ;;
        *) echo "Unexpected coverage profile path" >&2; exit 1 ;;
      esac
    done < <(rg --files --hidden --no-ignore -g '*.gcda' "$coverage_dir")
  fi
  ctest --preset "$preset" --output-on-failure \
    --output-junit "$project_root/artifacts/qa/$preset-ctest.xml"
done

gcovr "$HOME/.cache/autodiag/build/coverage" \
  --root "$project_root" \
  --filter 'src/' --filter 'apps/' --filter 'include/' \
  --json-summary artifacts/qa/coverage-summary.json \
  --html-details artifacts/qa/coverage.html \
  --print-summary --fail-under-line 80

mapfile -t cpp_files < <(rg --files include src apps tests/unit tools -g '*.cpp' -g '*.hpp')
clang-format --dry-run --Werror "${cpp_files[@]}"
mapfile -t production_sources < <(rg --files src apps -g '*.cpp')
clang-tidy -p "$HOME/.cache/autodiag/build/debug" "${production_sources[@]}" --quiet
python -m ruff check scripts tools tests autodiag_agent evaluation android
python -m ruff format --check scripts tools tests autodiag_agent evaluation android
bash -n scripts/*.sh scripts/autodiag-vcan
git diff --check
echo 'All local quality gates passed.'
