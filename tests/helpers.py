"""Shared helpers for the tests."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_example_config():
    with open(ROOT / "config.example.toml", "rb") as f:
        return tomllib.load(f)


def job(title, location="Remote - Europe", description="Enterprise customers and AI.", workplace="Remote", remote=True):
    return {"title": title, "location": location, "workplace": workplace, "remote": remote,
            "description": description, "id": "1", "url": "https://example.com/job/1", "posted_at": None}
