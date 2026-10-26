import json
import os
from pathlib import Path


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for key in ("canvas_token", "share_token", "reviews_cookie"):
        config[key] = os.getenv("SSS_" + key.upper(), config.get(key, ""))
    return config


def credential_values(config: dict) -> list[str]:
    values = [config.get("canvas_token"), config.get("share_token"), config.get("reviews_cookie")]
    # Also redact individual cookie values if a response ever echoes one.
    values += [part.partition("=")[2].strip() for part in config.get("reviews_cookie", "").split(";")]
    return [v for v in values if v and len(v) > 5]
