"""Create the one merchant login (this app has a single merchant).

    python scripts/create_merchant.py merchant@example.com 'a-long-password' ["Store name"]

Or set MERCHANT_EMAIL / MERCHANT_PASSWORD in .env and start the server: the account is created on first start.
Once it exists the dashboard and Demo Console need that sign-in (or MERCHANT_API_KEY, if you set one).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app import accounts  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        try:
            user = accounts.create_merchant(db, sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "Backstop Store")
        except ValueError as e:
            print(f"Not created: {e}")
            return 1
    print(f"Merchant account created: {user.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
