"""Load local development credentials for command-line runs."""

from pathlib import Path

def load_local_env() -> None:
    """Load .env from the current project directory without overriding shell vars."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        # Packaged/production runs may provide credentials directly through the
        # process environment. Missing optional .env support must not mask the
        # real credential validation error.
        return
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
