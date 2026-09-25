"""Narrow opt-in for reviewed local model code; no online code resolution."""
from pathlib import Path
from .io import sha256, read


def local_code_policy(model_path, config):
    policy = config.get("reviewed_local_code")
    if policy is None:
        return False
    root = Path(model_path).resolve(strict=True)
    if policy.get("revision") != config["model_revision"] or not policy.get("review_note"):
        raise ValueError("Custom code requires a revision-bound review note")
    expected = policy.get("files", {})
    actual = {str(p.relative_to(root)) for p in root.rglob("*.py")
              if ".cache" not in p.relative_to(root).parts and ".git" not in p.relative_to(root).parts}
    if not expected or set(expected) != actual:
        raise ValueError("Custom Python file inventory differs from reviewed allowlist")
    for name, value in expected.items():
        path = root / name
        if path.is_symlink() or root not in path.resolve().parents or sha256(path) != value:
            raise ValueError("Reviewed model code changed: " + name)
    for name in ("config.json", "tokenizer_config.json"):
        for ref in read(root / name).get("auto_map", {}).values():
            for item in ref if isinstance(ref, (list, tuple)) else [ref]:
                if item is None:
                    continue
                module, _, cls = item.rpartition(".")
                if not cls.isidentifier() or not module.isidentifier() or module + ".py" not in expected:
                    raise ValueError("auto_map must refer only to reviewed local modules")
    return True
