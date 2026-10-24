#!/usr/bin/env python3
"""Structural checks on openapi.yaml that a plain YAML parse would miss.

Catches the three things that actually break this contract:
  1. duplicate mapping keys (PyYAML silently keeps the last one)
  2. broken $ref pointers
  3. operations without operationId / responses, or required fields that
     no schema declares

Does not replace openapi-spec-validator, which checks spec conformance.
Run both. Exit code 1 means the contract is broken.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parent.parent / "openapi.yaml"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys."""


def _no_duplicate_keys(loader: yaml.Loader, node: yaml.Node, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate key {key!r} at {key_node.start_mark}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def collect_refs(node: object, out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                out.append(value)
            else:
                collect_refs(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_refs(value, out)


def resolve(doc: dict, ref: str) -> object | None:
    if not ref.startswith("#/"):
        return None
    current: object = doc
    for part in ref[2:].split("/"):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def main() -> int:
    problems: list[str] = []

    try:
        doc = yaml.load(CONTRACT.read_text(), Loader=StrictLoader)
    except yaml.YAMLError as exc:
        print(f"FAIL yaml: {exc}")
        return 1

    if not isinstance(doc, dict):
        print("FAIL contract is not a mapping")
        return 1

    version = doc.get("openapi", "")
    if not version.startswith("3.1"):
        problems.append(f"expected OpenAPI 3.1.x, found {version!r}")

    refs: list[str] = []
    collect_refs(doc, refs)
    for ref in sorted(set(refs)):
        if resolve(doc, ref) is None:
            problems.append(f"broken $ref {ref}")

    operations = 0
    for path, item in doc.get("paths", {}).items():
        if not isinstance(item, dict):
            problems.append(f"path {path} is not a mapping")
            continue
        for method, op in item.items():
            if method not in HTTP_METHODS:
                continue
            operations += 1
            if not isinstance(op, dict):
                problems.append(f"{method.upper()} {path} is not a mapping")
                continue
            if "operationId" not in op:
                problems.append(f"{method.upper()} {path} has no operationId")
            if "responses" not in op:
                problems.append(f"{method.upper()} {path} has no responses")
            if "summary" not in op:
                problems.append(f"{method.upper()} {path} has no summary")

    schemas = doc.get("components", {}).get("schemas", {})
    for name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        required = schema.get("required") or []
        properties = schema.get("properties") or {}
        for field in required:
            if properties and field not in properties:
                problems.append(f"schema {name} requires undeclared field {field!r}")

    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1

    print(
        f"PASS {CONTRACT.name}: {version}, {operations} operations, "
        f"{len(schemas)} schemas, {len(set(refs))} refs, no duplicate keys"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
