#!/usr/bin/env bash
# One command that proves the repo is coherent.
#
# Order matters: the contract is validated first, because generated code, tests,
# and the web client are all downstream of it. If the contract is broken, nothing
# after this point means anything.
#
# Usage:  ./scripts/check.sh
# Needs:  .venv with pydantic, pytest, ruff, openapi-spec-validator,
#         datamodel-codegen; plus node for openapi-typescript.

set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
PYTEST=".venv/bin/pytest"
RUFF=".venv/bin/ruff"

for tool in "$PY" "$PYTEST" "$RUFF"; do
  if [ ! -x "$tool" ]; then
    echo "FAIL missing $tool — create the venv first:"
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/pip install pydantic pytest pytest-asyncio ruff openapi-spec-validator datamodel-code-generator"
    exit 2
  fi
done

echo "==> contract structure"
python3 scripts/check_contract.py

echo "==> contract conformance"
"$PY" -c "
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename
spec, _ = read_from_filename('openapi.yaml')
validate(spec)
print('PASS openapi-spec-validator')
"

echo "==> generated clients in sync with the contract"
./scripts/generate-clients.sh >/tmp/railmind-gen.log 2>&1 || {
  echo "FAIL client generation — see /tmp/railmind-gen.log"
  tail -20 /tmp/railmind-gen.log
  exit 1
}
if ! git diff --quiet -- railmind/wire/models.py packages/contract/src/api-types.ts; then
  echo "FAIL generated code is stale: the contract changed without regenerating,"
  echo "     or someone hand-edited a generated file. Run ./scripts/generate-clients.sh"
  echo "     and commit the result."
  git diff --stat -- railmind/wire/models.py packages/contract/src/api-types.ts
  exit 1
fi
echo "PASS generated code matches openapi.yaml"

echo "==> lint"
"$RUFF" check .

echo "==> format"
"$RUFF" format --check .

echo "==> tests"
"$PYTEST"

echo
echo "ALL GREEN"
