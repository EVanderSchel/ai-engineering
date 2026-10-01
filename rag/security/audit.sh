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

# Exact versions from the lock. Checksums aren't needed to look up vulnerabilities, and the PyTorch
# index doesn't publish them for torch. "+cpu" marks PyTorch's CPU build; advisories are filed
# against the plain version, so drop it before looking torch up.
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt -q \
  | sed -E 's/^(torch==[0-9.]+)\+cpu/\1/' > "$requirements"

ignores=()
while read -r id _; do
  [[ -z "$id" || "$id" == \#* ]] && continue
  ignores+=(--ignore-vuln "$id")
done < security/accepted-vulnerabilities.txt

echo "Auditing $(grep -cE '^[a-zA-Z0-9]' "$requirements") locked packages ($(( ${#ignores[@]} / 2 )) accepted advisories)"
# --no-deps: the list is already complete and exactly pinned (pip-audit warns that hashes are
# preferred; they're verified at install time instead, where they matter).
uvx pip-audit==2.10.1 -r "$requirements" --disable-pip --no-deps --progress-spinner off \
  --cache-dir "$cache" "${ignores[@]}" 2> >(grep -v -e "fully hash their pinned" -e "pip-compile" >&2)
