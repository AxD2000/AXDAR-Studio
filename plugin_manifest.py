"""Plugin manifest validation for V1 schema.

This module contains a single public function `validate(m)` that checks a
manifest dictionary against the expected schema. It performs explicit
validation and raises :class:`ValueError` for any issue. The function
returns the validated manifest unchanged on success.
"""

import re
from typing import Any, Dict, List

# Regular expressions for identifiers
_LOWER_DOTTED_ID_RE = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)*$")
_SIMPLE_ID_RE = re.compile(r"^[a-z0-9_]+$")

# Allowed adapter
_ALLOWED_ADAPTER = {"kind": "cli", "entry": "action.py"}
# Allowed permissions
_ALLOWED_PERMISSIONS = ["execute:local"]

# Helper functions

def _ensure_type(name: str, value: Any, expected_type: type):
    if type(value) is not expected_type:
        raise ValueError(f"{name} must be of type {expected_type.__name__}")


def _ensure_keys(obj: Dict[str, Any], required: List[str], optional: List[str] = []):
    for key in required:
        if key not in obj:
            raise ValueError(f"Missing required key: {key}")
    for key in obj:
        if key not in required and key not in optional:
            raise ValueError(f"Unexpected key: {key}")


def _validate_version(version: str):
    if not re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,6}){2}", version):
        raise ValueError("version must have three 1–6 digit components")


def validate(m: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a plugin manifest dict.

    Parameters
    ----------
    m: dict
        Manifest to validate.

    Returns
    -------
    dict
        The same manifest if validation passes.

    Raises
    ------
    ValueError
        If the manifest is malformed or unsupported.
    """

    if not isinstance(m, dict):
        raise ValueError("manifest must be a dictionary")

    # Validate top‑level keys
    _ensure_keys(m, required=["manifest_version", "host_api", "id", "version", "label", "adapter", "permissions", "timeout_seconds", "dependencies", "actions"])

    # manifest_version
    _ensure_type("manifest_version", m["manifest_version"], int)
    if m["manifest_version"] != 1:
        raise ValueError("unsupported manifest_version")

    # host_api
    _ensure_type("host_api", m["host_api"], int)
    if m["host_api"] != 1:
        raise ValueError("unsupported host_api")

    # id
    _ensure_type("id", m["id"], str)
    if len(m["id"]) > 80 or not _LOWER_DOTTED_ID_RE.fullmatch(m["id"]):
        raise ValueError("id must be a lowercase dotted identifier <= 80 chars")

    # version
    _ensure_type("version", m["version"], str)
    _validate_version(m["version"])

    # label
    _ensure_type("label", m["label"], str)
    if not (1 <= len(m["label"]) <= 80):
        raise ValueError("label length must be 1–80 characters")

    # adapter
    if m["adapter"] != _ALLOWED_ADAPTER:
        raise ValueError("adapter must be {'kind':'cli','entry':'action.py'}")

    # permissions
    if m["permissions"] != _ALLOWED_PERMISSIONS:
        raise ValueError("permissions must be ['execute:local']")

    # timeout_seconds
    _ensure_type("timeout_seconds", m["timeout_seconds"], int)
    if not (1 <= m["timeout_seconds"] <= 10):
        raise ValueError("timeout_seconds must be between 1 and 10 inclusive")

    # dependencies
    if m["dependencies"] != []:
        raise ValueError("dependencies must be an empty list")

    # actions
    _ensure_type("actions", m["actions"], list)
    if not (1 <= len(m["actions"]) <= 16):
        raise ValueError("actions list must contain 1–16 items")

    action_ids = set()
    for idx, act in enumerate(m["actions"], start=1):
        if not isinstance(act, dict):
            raise ValueError(f"action #{idx} must be a dict")
        _ensure_keys(act, required=["id", "label", "execution", "input", "result"])

        # id
        _ensure_type("action id", act["id"], str)
        if len(act["id"]) > 80 or not _SIMPLE_ID_RE.fullmatch(act["id"]):
            raise ValueError("action ID must be a simple lowercase ASCII identifier")
        if act["id"] in action_ids:
            raise ValueError(f"duplicate action id: {act['id']}")
        action_ids.add(act["id"])

        # label
        _ensure_type("action label", act["label"], str)
        if not (1 <= len(act["label"]) <= 80):
            raise ValueError("action label length must be 1–80 characters")

        # execution, input, result
        if act["execution"] != "local":
            raise ValueError("action execution must be 'local'")
        if act["input"] != "text":
            raise ValueError("action input must be 'text'")
        if act["result"] != "text":
            raise ValueError("action result must be 'text'")

    return m
