"""Load ALL the demo data in one command, so anyone can try every feature (and every analysis) on a fresh database.

    python scripts/seed_demo_all.py              # everything below
    python scripts/seed_demo_all.py --only analysis,collections
    python scripts/seed_demo_all.py --skip policy

Steps (each is safe to re-run - they skip what already exists):
  storefront   5 named shoppers (@demo.com, password `password123`) with backdated order history and tiers
  analysis     ~170 more shoppers + orders/cart events that feed the Incentive, Loss & Recovery and Tier analyses
  collections  settled failed-autopay-debit runs, promises, voice calls and paid B2B invoices that feed the Collections analysis
  personas     the 7 Demo Console "test customers" (history, consent and memory already in the DB)
  policy       the approved store policy (data/store_policy.md) as the agent's knowledge, so policy questions can be answered
  merchant     the merchant login from MERCHANT_EMAIL / MERCHANT_PASSWORD in .env (never changes an existing password)

It writes straight to the DB in DATABASE_URL (the server need not be running) and refuses to run when ENVIRONMENT=production.
Undo with `python scripts/reset_demo_data.py --yes`.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

STEPS = ["storefront", "analysis", "collections", "personas", "policy", "merchant"]
SCRIPT = {"storefront": "seed_storefront_customers.py", "analysis": "seed_analysis_data.py", "collections": "seed_collections_data.py"}


def _db():
    from app.database import Base, SessionLocal, engine
    import app.models, app.rag_models  # noqa: F401  (register every table)
    Base.metadata.create_all(bind=engine)
    return SessionLocal()


def run_step(name: str) -> None:
    if name in SCRIPT:
        subprocess.run([sys.executable, str(ROOT / "scripts" / SCRIPT[name])], check=True, cwd=ROOT)
        return
    db = _db()
    try:
        if name == "personas":
            from app import demo_personas
            out = demo_personas.seed_all(db)
            print(f"  {len(out)} test customers ready")
        elif name == "policy":
            from app import knowledge
            text = (ROOT / "data" / "store_policy.md").read_text(encoding="utf-8")
            try:
                knowledge.add_document(db, "Backstop Store policy", text, approved=True)
                print("  store policy loaded and approved")
            except Exception as e:      # the same text is already a document: nothing to do
                if getattr(e, "status_code", None) != 409:
                    raise
                print("  store policy already loaded")
        elif name == "merchant":
            from app import accounts
            made = accounts.bootstrap_merchant(db)
            print("  merchant login created" if made else "  merchant login already there (or MERCHANT_EMAIL / MERCHANT_PASSWORD not set)")
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Load the demo data.")
    ap.add_argument("--only", help="comma-separated steps to run: " + ", ".join(STEPS))
    ap.add_argument("--skip", help="comma-separated steps to leave out")
    a = ap.parse_args()
    from app.config import settings
    if settings.environment == "production":
        print("Refusing to load demo data with ENVIRONMENT=production.")
        return 2
    chosen = [s for s in (a.only.split(",") if a.only else STEPS) if s]
    chosen = [s for s in chosen if s not in (a.skip.split(",") if a.skip else [])]
    unknown = [s for s in chosen if s not in STEPS]
    if unknown:
        print(f"Unknown step(s): {', '.join(unknown)}. Steps: {', '.join(STEPS)}")
        return 2
    from app.database import engine
    print(f"Loading demo data into {engine.url.render_as_string(hide_password=True)}")
    for s in STEPS:
        if s in chosen:
            print(f"== {s}")
            run_step(s)
    print("Done. Start the app and sign in (README: Credentials).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
