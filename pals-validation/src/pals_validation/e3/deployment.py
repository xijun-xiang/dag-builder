"""Explicit cluster allowlist, separate from the scientific E3 configuration."""
from dataclasses import asdict, dataclass
import os
from pathlib import Path

from .schema import require


@dataclass(frozen=True)
class ClusterProfile:
    name: str
    root: str
    slurm_conf: str
    slurm_bin: str
    launcher: str
    reservation: str | None = None

    def record(self) -> dict:
        return asdict(self)


PROFILES = {
    "b1": ClusterProfile("b1", "/work/projects/polyullm/xxj/PALS",
        "/cm/shared/apps/slurm/etc/slurm/slurm.conf", "/cm/local/apps/slurm/current/bin",
        "b1-e3-greedy.sbatch"),
    "a1": ClusterProfile("a1", "/work/projects/polyullm/xxj/pals",
        "/cm/shared/apps/slurm/var/etc/slurm/slurm.conf", "/cm/shared/apps/slurm/current/bin",
        "a1-e3-greedy.sbatch", "pretrain"),
    "b1-code-agent": ClusterProfile("b1-code-agent", "/work/projects/polyullm/xxj/PALS",
        "/cm/shared/apps/slurm/etc/slurm/slurm.conf", "/cm/local/apps/slurm/current/bin",
        "b1-e3-greedy.sbatch", "code-agent"),
}


def profile() -> ClusterProfile:
    name = os.environ.get("PALS_CLUSTER", "b1")
    require(name in PROFILES, "unknown cluster; no arbitrary project-root override")
    return PROFILES[name]


def assert_project_path(path: Path) -> Path:
    fence = Path(profile().root)
    require(fence.resolve(strict=True) == fence, "project root is a symlink")
    require(path.is_absolute() and path.resolve(strict=True) == path and
            (path == fence or fence in path.parents), "outside selected project or symlink")
    return path
