"""Configuration loader for the Discord Moderation Bot.

Loads environment variables from .env via python-dotenv.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env if present
env_path = Path(".env")
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "")
TYPESAFE_API_KEY: str = os.environ.get("TYPESAFE_API_KEY", "")
DATABASE_PATH: str = os.environ.get("DATABASE_PATH", "bot_data.db")
COMMAND_PREFIX: str = os.environ.get("COMMAND_PREFIX", "!")
