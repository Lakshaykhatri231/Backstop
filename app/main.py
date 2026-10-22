import threading
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import production_problems, settings
from app.database import Base, engine
from app.merchant_auth import router as merchant_auth_router, require_merchant
from app.webhook import router as webhook_router
from app.outcomes import router as outcomes_router
from app.debug import router as debug_router
from app.dropoff import run_poller
from app.storefront import router as storefront_router, run_cart_idle_sweep
from app.merchant import router as merchant_router
from app.insights_router import router as insights_router
from app.maintenance import start_sweep_thread
from app.scheduler import run_scheduler
from app.promises import router as promises_router
from app.receivables import router as receivables_router
from app.voice import router as voice_router
from app.contacts import router as contacts_router
from app.mandate import router as mandate_router
from app.autopay import router as autopay_router
from app.payment_links import router as payment_links_router
from app import order_links  # noqa: F401  registers the failed-order link + reminder handlers
from app.selfserve import router as selfserve_router
from app.knowledge import router as knowledge_router
from app.memory import router as memory_router
from app.demo import router as demo_router
from app.accounts import router as accounts_router

app = FastAPI(title="Revenue Recovery Agent", version="0.2.0")

# The React (Vite) SPA lives in frontend/, built to frontend/dist/. FastAPI
# serves that build directly - same origin as the API, so no CORS is ever
# needed. `npm run build` in frontend/ before starting uvicorn (see README).
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"


@app.on_event("startup")
def on_startup():
    # Fail closed: a production deploy with demo secrets / open merchant
    # endpoints should refuse to boot, not boot quietly.
    problems = production_problems(settings)
    if problems:
        raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
    from app import rag_models  # noqa: F401  (registers the knowledge / memory tables)
    Base.metadata.create_all(bind=engine)
    from app import embeddings, vectorstore
    vectorstore.init(engine)      # pgvector + HNSW if the extension exists; in-process search otherwise
    embeddings.warm_up()          # load the embedding model in the background
    poller_thread = threading.Thread(target=run_poller, daemon=True, name="dropoff-poller")
    poller_thread.start()
    cart_sweep_thread = threading.Thread(target=run_cart_idle_sweep, daemon=True, name="cart-idle-sweep")
    cart_sweep_thread.start()
    # Keeps time-based tier components (recency, rolling behaviour window)
    # from going stale on customers who simply stop shopping, and resolves
    # abandoned CREATED orders. See app/maintenance.py.
    start_sweep_thread()
    # Fires every due ScheduledAction (mandate retries, promise reminders,
    # invoice chase steps, voice calls). See app/scheduler.py.
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True, name="scheduler")
    scheduler_thread.start()
    # The merchant login, from MERCHANT_EMAIL / MERCHANT_PASSWORD (no-op when unset).
    from app import accounts
    from app.database import SessionLocal
    with SessionLocal() as _db:
        accounts.bootstrap_merchant(_db)


# Every one of these paths is a client-side route inside the same built SPA -
# TanStack Router reads window.location.pathname and renders the right page,
# so they all just serve the same index.html. None collide with the API
# path prefixes below (/auth, /catalog, /cart, /checkout, /merchant,
# /outcomes, /audit, /insights, /debug), so router-include order doesn't matter.
@app.get("/")
def root():
    return FileResponse(FRONTEND_INDEX)


@app.get("/dashboard")
def dashboard():
    return FileResponse(FRONTEND_INDEX)


@app.get("/store")
def store():
    return FileResponse(FRONTEND_INDEX)


@app.get("/login")
def login_page():
    return FileResponse(FRONTEND_INDEX)


@app.get("/support")
def support():
    return FileResponse(FRONTEND_INDEX)


@app.get("/invoices")
def invoices_page():
    return FileResponse(FRONTEND_INDEX)


@app.get("/account")
def account_page():
    return FileResponse(FRONTEND_INDEX)


@app.get("/account/invoices")
def account_invoices_page():
    return FileResponse(FRONTEND_INDEX)


@app.get("/pay-later")
def pay_later():
    return FileResponse(FRONTEND_INDEX)


@app.get("/demo-console")
def demo_console_page():
    return FileResponse(FRONTEND_INDEX)


@app.get("/architecture")
def architecture():
    return FileResponse(FRONTEND_INDEX)


@app.get("/tiers")
def tiers():
    return FileResponse(FRONTEND_INDEX)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(FRONTEND_DIST / "favicon.ico")


app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")
# Silero VAD worklet / model / ONNX runtime for the hands-free call (copied there at build time).
if (FRONTEND_DIST / "vad").is_dir():
    app.mount("/vad", StaticFiles(directory=FRONTEND_DIST / "vad"), name="frontend-vad")

# Merchant-side routers require X-Merchant-Key when MERCHANT_API_KEY is set
# (app/merchant_auth.py; off by default). Routers that mix merchant routes
# with customer/public ones (promises, contacts) protect per route instead,
# and the storefront, webhook, OTP and self-serve routers stay open on purpose.
_MERCHANT = [Depends(require_merchant)]
app.include_router(merchant_auth_router, tags=["merchant-auth"])
app.include_router(webhook_router, tags=["webhook"])
app.include_router(outcomes_router, tags=["outcomes"], dependencies=_MERCHANT)
app.include_router(debug_router, tags=["debug"], dependencies=_MERCHANT)
app.include_router(storefront_router, tags=["storefront"])
app.include_router(merchant_router, tags=["merchant"], dependencies=_MERCHANT)
app.include_router(insights_router, tags=["insights"], dependencies=_MERCHANT)
app.include_router(promises_router, tags=["promises"])
app.include_router(receivables_router, tags=["receivables"], dependencies=_MERCHANT)
app.include_router(voice_router, tags=["voice"], dependencies=_MERCHANT)
app.include_router(contacts_router, tags=["contacts"])
app.include_router(mandate_router, tags=["mandate"], dependencies=_MERCHANT)
app.include_router(autopay_router, tags=["autopay"], dependencies=_MERCHANT)
app.include_router(payment_links_router, tags=["payment-links"], dependencies=_MERCHANT)
app.include_router(selfserve_router, tags=["self-serve"])
# Logins and the pages behind them: each route carries its own role check.
app.include_router(accounts_router, tags=["accounts"])
app.include_router(knowledge_router, tags=["knowledge"], dependencies=_MERCHANT)
app.include_router(memory_router, tags=["memory"])
# Demo Console API: 404s in production, merchant-key protected otherwise.
app.include_router(demo_router, tags=["demo"], dependencies=_MERCHANT)
