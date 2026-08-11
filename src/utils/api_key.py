import os


def get_api_key_from_state(state: dict, api_key_name: str) -> str:
    """Get an API key from the process environment, never persisted state."""
    return os.getenv(api_key_name)
