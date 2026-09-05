"""Vercel entry point. Every request is rewritten here (vercel.json) and
handed to the FastAPI app in server/app.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.app import app  # noqa: E402,F401  — Vercel looks for `app`
