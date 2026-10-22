"""Empty the database (every table, schema kept) so you can start clean - or reload demo data afterwards.

    python scripts/reset_demo_data.py --yes                 # wipe, then recreate the merchant login from .env
    python scripts/reset_demo_data.py --yes --then-seed     # wipe, then run scripts/seed_demo_all.py

DESTRUCTIVE: it deletes EVERYTHING in the database named in DATABASE_URL (shoppers, orders, invoices, mandates, audit log...).
It prints that name first, needs --yes, and refuses when ENVIRONMENT=production. Take a backup first if the data matters:
    pg_dump -Fc -d <database> -f backup.dump
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    ap = argparse.ArgumentParser(description="Wipe the database.")
    ap.add_argument("--yes", action="store_true", help="confirm that you want everything deleted")
    ap.add_argument("--then-seed", action="store_true", help="load the demo data afterwards")
    a = ap.parse_args()
    from sqlalchemy import text
    from app.config import settings
    from app.database import Base, SessionLocal, engine
    import app.models, app.rag_models  # noqa: F401
    if settings.environment == "production":
        print("Refusing to wipe a database with ENVIRONMENT=production.")
        return 2
    target = engine.url.render_as_string(hide_password=True)
    if not a.yes:
        print(f"This would delete EVERYTHING in {target}. Run again with --yes to do it.")
        return 2
    Base.metadata.create_all(bind=engine)
    names = ", ".join(f'"{t}"' for t in Base.metadata.tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
    print(f"Emptied {len(Base.metadata.tables)} tables in {target}")
    from app import accounts
    with SessionLocal() as db:
        made = accounts.bootstrap_merchant(db)
    print("Merchant login recreated from .env" if made else "No merchant login created (MERCHANT_EMAIL / MERCHANT_PASSWORD not set)")
    if a.then_seed:
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo_all.py"), "--skip", "merchant"], cwd=ROOT).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
