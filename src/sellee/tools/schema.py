"""A minimal hand-rolled JSON-Schema subset — validation without a pip dependency.

Stdlib-only rules out jsonschema, and the tool surface needs only a small slice: type, required,
properties, enum, additionalProperties, array items, and string maxLength/minLength. The posture
is reject-never-sanitize — an unknown parameter is an error, never silently dropped, so a caller
can never smuggle a field past a schema by misspelling a real one. Schemas are plain data on
each ToolSpec, so tools/list can serve them verbatim.

That subset is closed, not merely incomplete: `unsupported_keywords` names anything outside it so
a schema carrying an unenforced keyword is rejected where it is declared rather than read as a
constraint that quietly does nothing.
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


# Every keyword validate() honours, and nothing else. Keep this in step with validate(): a
# keyword listed here but not enforced below is exactly the silent no-op this set exists to stop.
_ENFORCED_KEYWORDS = frozenset(
    {
        "type",
        "required",
        "properties",
        "additionalProperties",
        "enum",
        "items",
        "maxLength",
        "minLength",
    }
)

# Keywords that describe rather than constrain. Nothing validates them because there is nothing
# to validate — they are documentation, served verbatim to the model by tools/list — so they are
# allowed through rather than reported as constraints that do nothing.
_ANNOTATION_KEYWORDS = frozenset({"description", "title", "default", "examples"})

_KNOWN_KEYWORDS = _ENFORCED_KEYWORDS | _ANNOTATION_KEYWORDS


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

    # Gated on the *declared* type rather than on the value's: a value that is not the declared
    # string already failed above (the type check raises), so len() here is always a string's.
    if expected == "string":
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _fail(path, f"must be at most {schema['maxLength']} characters")
        if "minLength" in schema and len(value) < schema["minLength"]:
            _fail(path, f"must be at least {schema['minLength']} characters")

    if expected == "object":
        _validate_object(schema, value, path)
    elif expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{path}[{i}]")


def unsupported_keywords(schema: dict, path: str = "") -> list:
    """Dotted paths to every keyword in `schema` (recursively) that is neither enforced nor a
    known annotation.

    Callers check this where a schema is declared, not where it is used: validate() ignores what it
    does not know, so an unrecognised keyword would read as a live constraint while doing nothing.
    Returning the paths rather than raising keeps the caller's error wording its own.
    """
    found = [(f"{path}.{key}" if path else key) for key in schema if key not in _KNOWN_KEYWORDS]
    # Only the boolean form of additionalProperties is enforced. The schema-valued form would
    # read as "extra keys validate against this" while actually admitting anything — the same
    # silent no-op as an unknown keyword, wearing a known keyword's name.
    if not isinstance(schema.get("additionalProperties", False), bool):
        found.append(f"{path}.additionalProperties" if path else "additionalProperties")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, subschema in properties.items():
            if isinstance(subschema, dict):
                found.extend(unsupported_keywords(subschema, f"{path}.{name}" if path else name))
    items = schema.get("items")
    if isinstance(items, dict):
        found.extend(unsupported_keywords(items, f"{path}[]"))
    return found


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
