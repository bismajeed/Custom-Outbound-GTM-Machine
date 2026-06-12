"""Environment-driven configuration.

Bring-your-own-keys: every secret loads from the environment via python-dotenv.
Nothing here ever logs or prints a key value, and keys are never accepted as
CLI arguments. `get_config()` fails fast with a readable message if a required
key is missing.
"""

import os

from dotenv import load_dotenv

load_dotenv()

REQUIRED = ["ANTHROPIC_API_KEY", "APOLLO_API_KEY", "SMARTLEAD_API_KEY"]


def get_config() -> dict:
    """Load and validate configuration from the environment.

    Raises SystemExit (with a fix-it message) if any required key is missing.
    Returns a plain dict of resolved settings. Never logs key values.
    """
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        raise SystemExit(
            f"Missing required env vars: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )
    return {
        "anthropic_key": os.environ["ANTHROPIC_API_KEY"],
        "apollo_key": os.environ["APOLLO_API_KEY"],
        "smartlead_key": os.environ["SMARTLEAD_API_KEY"],
        "database_url": os.getenv("DATABASE_URL", "sqlite:///outbound.db"),
        "search_key": os.getenv("SEARCH_API_KEY"),
    }


def get_database_url() -> str:
    """Resolve only the database URL (no required-key validation).

    Used by commands like `init` that must touch the DB before keys exist.
    """
    return os.getenv("DATABASE_URL", "sqlite:///outbound.db")
