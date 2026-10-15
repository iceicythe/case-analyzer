"""Archive the exact resources used for frozen predictions, never relabel data."""
import hashlib
from pathlib import Path

from app.paths import PROJECT_ROOT
from app.storage import atomic_json


def archive_resources(root, plan):
    root = Path(root)
    if plan.get('resource_snapshot'):
        return plan
    resources = [(name, PROJECT_ROOT / name.removeprefix('@app/'), expected)
                 for name, expected in plan['frozen_files'].items() if name.startswith('@app/')]
    # Verify everything before making an archive. Never bless modified resources.
    for _, path, expected in resources:
        if not path.resolve().is_relative_to(PROJECT_ROOT.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('冻结资源已变化，不能建立原版本快照')
    for name, path, expected in resources:
        target = root / 'baseline_resources' / name.removeprefix('@app/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError('冻结资源复制校验失败')
    updated = dict(plan, resource_snapshot='baseline_resources')
    atomic_json(root / 'benchmark.json', updated)
    return updated


def resource_base(root, plan):
    name = plan.get('resource_snapshot')
    if name is None:
        return PROJECT_ROOT
    # Fixed local archive location; reject alternate paths from the manifest.
    if name != 'baseline_resources':
        raise ValueError('冻结资源快照位置无效')
    return Path(root) / name
