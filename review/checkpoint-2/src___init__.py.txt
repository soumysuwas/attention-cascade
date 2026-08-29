"""Package init. Loads .env before any module reads os.environ.

Does: pull GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION / AC_LLM_MODE out of .env into the
process environment, once, before config.py is imported by anything.
Does not: hold any logic. Nothing else belongs here.
Exists because: config.py reads os.environ at import time, so the .env load has to happen
strictly earlier than the first `from . import config` anywhere in the package.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
