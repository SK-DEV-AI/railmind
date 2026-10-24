#!/usr/bin/env bash
# Regenerate both typed clients from openapi.yaml.
# If this fails, the contract broke. Fix the contract, do not edit the output.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> structural checks"
python3 scripts/check_contract.py

echo "==> spec conformance"
if [ -x .venv/bin/python ]; then
  .venv/bin/python -c "
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename
spec, _ = read_from_filename('openapi.yaml')
validate(spec)
print('PASS openapi-spec-validator')
"
else
  echo "SKIP .venv missing; run: python3 -m venv .venv && .venv/bin/pip install openapi-spec-validator datamodel-code-generator"
fi

echo "==> typescript"
mkdir -p packages/contract/src
npx --yes openapi-typescript@7 openapi.yaml -o packages/contract/src/api-types.ts

echo "==> python (pydantic v2)"
mkdir -p railmind/wire
if [ -x .venv/bin/datamodel-codegen ]; then
  .venv/bin/datamodel-codegen \
    --input openapi.yaml \
    --input-file-type openapi \
    --output railmind/wire/models.py \
    --output-model-type pydantic_v2.BaseModel \
    --target-python-version 3.12 \
    --use-standard-collections \
    --use-union-operator \
    --disable-timestamp
else
  echo "SKIP datamodel-codegen missing in .venv"
fi

echo "==> done"
wc -l packages/contract/src/api-types.ts railmind/wire/models.py 2>/dev/null || true
