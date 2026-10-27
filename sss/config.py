import json
import os
from pathlib import Path

from dotenv import dotenv_values


def load_config(path: Path, env_path: Path = Path(".env")) -> dict:
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    # Read only the chosen file; credentials containing ${...} must stay literal.
    env = dotenv_values(env_path, encoding="utf-8-sig", interpolate=False) if env_path.exists() else {}
    for key in ("canvas_token", "share_token", "reviews_cookie"):
        name = "SSS_" + key.upper()
        config[key] = os.environ.get(name, env.get(name, config.get(key, ""))) or ""
    return config


def credential_values(config: dict) -> list[str]:
    values = [config.get("canvas_token"), config.get("share_token"), config.get("reviews_cookie")]
    # Also redact individual cookie values if a response ever echoes one.
    values += [part.partition("=")[2].strip() for part in config.get("reviews_cookie", "").split(";")]
    return [v for v in values if v and len(v) > 5]
