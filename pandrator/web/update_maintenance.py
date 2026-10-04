"""Share the update marker between HTTP intake and job claims."""

from pathlib import Path


def update_maintenance_active(root: Path) -> bool:
    return (root / "maintenance.json").is_file()
