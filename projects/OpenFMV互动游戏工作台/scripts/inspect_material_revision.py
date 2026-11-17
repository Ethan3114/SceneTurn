import argparse
import json
from pathlib import Path

import jsonpatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.models import ProjectVersion


def main():
    parser = argparse.ArgumentParser(description="只显示两个实际项目版本的变化路径")
    parser.add_argument("project_id")
    parser.add_argument("before", type=int)
    parser.add_argument("after", type=int)
    args = parser.parse_args()
    database = ROOT / "data/openfmv.sqlite3"
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        versions = [session.scalar(select(ProjectVersion).where(ProjectVersion.project_id == args.project_id, ProjectVersion.revision == revision)) for revision in (args.before, args.after)]
        for attribute in ("data_json", "canvases_json"):
            patch = jsonpatch.make_patch(getattr(versions[0], attribute), getattr(versions[1], attribute))
            print(json.dumps({"section": attribute, "changes": [{"op": item["op"], "path": item["path"]} for item in patch.patch]}, ensure_ascii=False))
    engine.dispose()


if __name__ == "__main__":
    main()
