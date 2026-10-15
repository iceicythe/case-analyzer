"""Paths resolve within the portable workbench folder, never against launch cwd."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PROJECT_ROOT / 'static'
DEFAULT_RUNTIME = PROJECT_ROOT / 'runtime'
