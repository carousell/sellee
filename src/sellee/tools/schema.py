"""A minimal hand-rolled JSON-Schema subset — validation without a pip dependency.

Stdlib-only rules out jsonschema, and the tool surface needs only a small slice: type,
required, properties, enum, additionalProperties, and array items. The posture is
reject-never-sanitize — an unknown parameter is an error, never silently dropped, so a caller
can never smuggle a field past a schema by misspelling a real one. Schemas are plain data on
each ToolSpec, so tools/list can serve them verbatim.
"""

from __future__ import annotations


class ValidationError(ValueError):
    """Input did not conform to a tool's schema. The message names the offending path."""


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is an int subclass; JSON true/false is never a number/integer.
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def _fail(path: str, message: str) -> None:
    where = path or "input"
    raise ValidationError(f"{where}: {message}")


def validate(schema: dict, value: object, path: str = "") -> None:
    """Validate `value` against `schema`, raising ValidationError on the first violation."""
    expected = schema.get("type")
    if expected is not None:
        check = _TYPE_CHECKS.get(expected)
        if check is None:
            raise ValidationError(f"unsupported schema type {expected!r}")
        if not check(value):
            _fail(path, f"expected {expected}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        _fail(path, f"must be one of {schema['enum']!r}")

    if expected == "object":
        _validate_object(schema, value, path)
    elif expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{i}]")


def _validate_object(schema: dict, value: dict, path: str) -> None:
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    # additionalProperties defaults to False: unknown keys are rejected, never dropped.
    allow_additional = schema.get("additionalProperties", False)

    for name in required:
        if name not in value:
            _fail(path, f"missing required property {name!r}")

    for key, item in value.items():
        subpath = f"{path}.{key}" if path else key
        if key in properties:
            validate(properties[key], item, subpath)
        elif not allow_additional:
            _fail(path, f"unknown property {key!r}")
