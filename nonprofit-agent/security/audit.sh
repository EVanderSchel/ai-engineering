#!/usr/bin/env bash
# Check every locked production dependency (uv.lock) for known vulnerabilities. Fails on any that
# isn't listed, with a reason, in accepted-vulnerabilities.txt. Needs uv.
#
#     security/audit.sh
set -euo pipefail
cd "$(dirname "$0")/.."

requirements=$(mktemp)
# A fresh cache each run: always current advisory data, and no clashes with other pip-audit versions.
cache=$(mktemp -d)
trap 'rm -rf "$requirements" "$cache"' EXIT

# Exact versions from the lock (checksums aren't needed to look up vulnerabilities).
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt -q > "$requirements"

packages=$(grep -cE '^[a-zA-Z0-9]' "$requirements" || true)
if [ "$packages" -eq 0 ]; then
  echo "No production dependencies to audit yet."
  exit 0
fi

ignores=()
while read -r id _; do
  [[ -z "$id" || "$id" == \#* ]] && continue
  ignores+=(--ignore-vuln "$id")
done < security/accepted-vulnerabilities.txt

echo "Auditing $packages locked packages ($(( ${#ignores[@]} / 2 )) accepted advisories)"
# --no-deps: the list is already complete and exactly pinned (pip-audit warns that hashes are
# preferred; they're verified at install time instead, where they matter).
uvx pip-audit==2.10.1 -r "$requirements" --disable-pip --no-deps --progress-spinner off \
  --cache-dir "$cache" "${ignores[@]}" 2> >(grep -v -e "fully hash their pinned" -e "pip-compile" >&2)
