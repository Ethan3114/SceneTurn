import json
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.core.security import decode_secret
from app.models import TextProviderSettings


def main():
    settings = Settings(data_dir=ROOT / "data")
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        provider = session.get(TextProviderSettings, "platform")
        key = decode_secret(provider.api_key_encrypted, settings)
    started = time.monotonic()
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        response = client.get("https://api.deepseek.com/models", headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
    payload = response.json()
    print(json.dumps({"httpStatus": response.status_code, "seconds": round(time.monotonic() - started, 3),
        "models": [item["id"] for item in payload["data"]]}, ensure_ascii=False))
    engine.dispose()


if __name__ == "__main__":
    main()
