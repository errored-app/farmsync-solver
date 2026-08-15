import json

from .paths import config_file

# Resolved by paths.py, not from __file__. A frozen build's __file__ lives in a
# per-launch temp directory, so computing it here meant the packaged .exe could
# never find its config at all.
CONFIG_FILE = config_file()

with open(CONFIG_FILE, encoding="utf-8") as f:
    _config = json.load(f)


class Util:
    @staticmethod
    def config() -> dict:
        return _config

    @staticmethod
    def short(s: str, n: int = 18) -> str:
        return s if len(s) <= n else s[:n - 1] + "..."
