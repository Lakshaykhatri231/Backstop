import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.database import Base, engine
from app.webhook import router as webhook_router
from app.outcomes import router as outcomes_router
from app.debug import router as debug_router
from app.dropoff import run_poller
from app.storefront import router as storefront_router, run_cart_idle_sweep
from app.merchant import router as merchant_router
from app.insights_router import router as insights_router
from app.maintenance import start_sweep_thread

app = FastAPI(title="Revenue Recovery Agent", version="0.2.0")

# The React (Vite) SPA lives in frontend/, built to frontend/dist/. FastAPI
# serves that build directly - same origin as the API, so no CORS is ever
# needed. `npm run build` in frontend/ before starting uvicorn (see README).
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    poller_thread = threading.Thread(target=run_poller, daemon=True, name="dropoff-poller")
    poller_thread.start()
    cart_sweep_thread = threading.Thread(target=run_cart_idle_sweep, daemon=True, name="cart-idle-sweep")
    cart_sweep_thread.start()
    # Keeps time-based tier components (recency, rolling behaviour window)
    # from going stale on customers who simply stop shopping, and resolves
    # abandoned CREATED orders. See app/maintenance.py.
    start_sweep_thread()


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
app.include_router(webhook_router, tags=["webhook"])
app.include_router(outcomes_router, tags=["outcomes"])
app.include_router(debug_router, tags=["debug"])
app.include_router(storefront_router, tags=["storefront"])
app.include_router(merchant_router, tags=["merchant"])
app.include_router(insights_router, tags=["insights"])
