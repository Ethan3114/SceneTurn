import argparse
import hashlib
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.core.security import encode_secret, initialize_secret_store
from app.db import SessionLocal
from app.image_models import ImageAccount
from app.models import User
from app.services.projects import lock_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    config = dotenv_values(args.source)
    keys = [value.strip() for value in config.get("SENSETIME_API_KEYS", "").split(",") if value.strip()]
    if len(keys) != 3 or len(set(keys)) != 3:
        raise ValueError("原项目必须配置三个不同的商汤账号凭据")
    settings = get_settings()
    initialize_secret_store(settings)
    with SessionLocal() as session:
        lock_write(session)
        user = session.scalar(select(User).where(User.email == args.email))
        if user is None:
            raise ValueError("目标用户不存在")
        for slot, key in enumerate(keys, 1):
            account = session.scalar(select(ImageAccount).where(ImageAccount.owner_id == user.id, ImageAccount.slot == slot))
            fingerprint = hashlib.sha256(key.encode()).hexdigest()
            if account is not None:
                if account.credential_hash != fingerprint or account.active_job_id:
                    raise ValueError("现有账号配置或占用状态需要人工核对")
                continue
            session.add(ImageAccount(owner_id=user.id, slot=slot, credential_hash=fingerprint,
                api_key_encrypted=encode_secret(key, settings), status="available", last_assigned_at=0))
        session.commit()
    print("已安全导入3个商汤免费账号；每账号并发1，总并发3。")


if __name__ == "__main__":
    main()
