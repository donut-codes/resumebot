#!/usr/bin/env bash
# Set the app's version everywhere it lives: backend/pyproject.toml, the backend's settings.VERSION (shown on the page
# and in /health), frontend/package.json and its lockfile. Run by the release script (the project rules, "Releasing"); branches
# never bump their own version. Bug fix: patch. Feature: minor, patch reset. Adapted from another project's script.
# Usage: bash app/scripts/bump-version.sh 1.6.0
set -euo pipefail

NEW="${1:?usage: bump-version.sh X.Y.Z}"
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "not a version: $NEW" >&2; exit 1; }
cd "$(dirname "$0")/.."  # app/

sed -i -E "s/^version = \"[0-9.]+\"/version = \"$NEW\"/" backend/pyproject.toml
sed -i -E "s/^VERSION = \"[0-9.]+\"/VERSION = \"$NEW\"/" backend/app/settings.py
# Windows has a python3 that only offers to install Python: prefer its launcher there, python3 everywhere else.
if command -v py >/dev/null 2>&1; then PY=(py -3); else PY=(python3); fi
"${PY[@]}" - "$NEW" <<'EOF'
import json, sys
new = sys.argv[1]
for path in ("frontend/package.json", "frontend/package-lock.json"):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = new
    if "packages" in data:  # the lockfile repeats the root package's version
        data["packages"][""]["version"] = new
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
EOF
grep -H -m1 -E '^version|^VERSION' backend/pyproject.toml backend/app/settings.py
