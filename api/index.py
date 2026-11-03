"""Vercel entrypoint for the FastAPI server.

The application remains in server/ so the Docker deployment keeps using the
same module. Vercel invokes this ASGI app as a Python serverless function.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from main import app  # noqa: E402

