"""Narrow, hash-bound operator omission for one observed provider overrun.

This does not accept the anomalous response or weaken the normal response
contract. The raw request and response stay in the immutable attempt ledger.
"""

import hashlib
from pathlib import Path

from .response_contract import check_response
from .storage import read_json


OMISSION = {
    "schema_version": "mmlu_operator_infrastructure_omission_v1",
    "subject": "professional_law",
    "item_id": "9603e2f5989a50f1cc0b",
    "stage": "solve",
    "attempt": "attempt-00",
    "request_sha256": "c38c3fe417f271c7890a407c1b32f714ed055a2d80f1d97e910cd12e9c05ea4e",
    "response_sha256": "54c759fd5d0265941b9742488694207f57210dc2b3a6e5b5945e527dfe0c6f37",
    "reason": "usage_exceeds_reserved_allowance",
}


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_operator_omission(root):
    """Return the authorized exception only when every frozen byte matches."""
    root = Path(root)
    manifest = root / "operator_infrastructure_omission.json"
    if not manifest.exists():
        return None
    if manifest.is_symlink() or root.name != OMISSION["subject"]:
        raise ValueError("operator omission outside its authorized MMLU subject")
    omission = read_json(manifest)
    if omission != OMISSION:
        raise ValueError("operator omission does not match the authorized case")
    attempt = root / "items" / omission["item_id"] / omission["stage"] / omission["attempt"]
    request_path, response_path = attempt / "request.json", attempt / "response.json"
    if (not request_path.is_file() or not response_path.is_file()
            or request_path.is_symlink() or response_path.is_symlink()
            or _sha256(request_path) != omission["request_sha256"]
            or _sha256(response_path) != omission["response_sha256"]):
        raise ValueError("operator omission evidence changed")
    request = read_json(request_path)
    response = read_json(response_path)["body"]
    if (request["payload"].get("model") != "deepseek-v4-flash"
            or request["payload"].get("max_tokens") != 32768
            or request["reserved_tokens"] != 36256):
        raise ValueError("operator omission request controls changed")
    check = check_response(request["payload"], response, request["reserved_tokens"],
                           strict=True, content_gated=True)
    if (check["violations"] != [omission["reason"]]
            or "completion_exceeds_requested_max_tokens" not in check["warnings"]
            or check["reported_tokens"] != 41360):
        raise ValueError("operator omission is not the observed contract violation")
    return omission


def is_omitted_attempt(root, omission, attempt):
    if omission is None:
        return False
    return Path(attempt) == (Path(root) / "items" / omission["item_id"]
                             / omission["stage"] / omission["attempt"])
