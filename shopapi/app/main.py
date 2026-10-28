"""ShopAPI: a small e-commerce service that we deliberately break and then diagnose."""
import asyncio
import logging
import random
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import chaos, db
from .logging_config import setup_logging, trace_id_var
from .metrics import DEPENDENCY_ERRORS, LATENCY, ORDER_CACHE_BYTES, REQUESTS

setup_logging()
log = logging.getLogger("shopapi")
app = FastAPI(title="ShopAPI")

PAYMENT_TIMEOUT_MS = 5000
LEAK_WARN_STEP = 50 * 1024 * 1024  # warn every 50 MB of cache growth
_last_leak_warning = 0


class PaymentGatewayError(Exception):
    pass


@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path in ("/metrics", "/health") or request.url.path.startswith("/chaos"):
        return await call_next(request)

    trace_id_var.set(uuid.uuid4().hex[:16])
    start = time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        response, status = JSONResponse({"detail": "internal error"}, status_code=500), 500

    elapsed = time.perf_counter() - start
    route_obj = request.scope.get("route")
    route = getattr(route_obj, "path", "unmatched")
    REQUESTS.labels(request.method, route, str(status)).inc()
    LATENCY.labels(request.method, route).observe(elapsed)
    log.info(
        "%s %s %d %.0fms", request.method, route, status, elapsed * 1000,
        extra={"fields": {"method": request.method, "route": route, "status": status, "duration_ms": round(elapsed * 1000)}},
    )
    return response


@app.exception_handler(PaymentGatewayError)
async def payment_error(request: Request, exc: PaymentGatewayError):
    log.error("Payment failed: %s", exc, exc_info=exc, extra={"fields": {"dependency": "payment-gateway"}})
    return JSONResponse({"detail": "payment provider unavailable"}, status_code=502)


# --- Business endpoints ---------------------------------------------------------

@app.get("/products")
async def list_products(limit: int = 20):
    await db.query("products", "select")
    chaos.burn_cpu()  # stands in for "recommendation scoring"
    return list(db.PRODUCTS.values())[:limit]


@app.get("/products/{product_id}")
async def get_product(product_id: int):
    await db.query("products", "select")
    if product_id not in db.PRODUCTS:
        raise HTTPException(404, "product not found")
    return db.PRODUCTS[product_id]


class OrderIn(BaseModel):
    product_id: int
    quantity: int = 1


async def charge_payment(amount: float) -> None:
    await asyncio.sleep(random.uniform(0.02, 0.08))
    if random.random() < chaos.state.error_rate:
        DEPENDENCY_ERRORS.labels("payment-gateway").inc()
        raise PaymentGatewayError(f"Timeout contacting payment-gateway after {PAYMENT_TIMEOUT_MS}ms (amount={amount})")


@app.post("/orders", status_code=201)
async def create_order(order: OrderIn):
    global _last_leak_warning
    product = db.PRODUCTS.get(order.product_id)
    if product is None:
        raise HTTPException(404, "product not found")

    await db.query("products", "select")
    total = round(product["price"] * order.quantity, 2)
    await charge_payment(total)
    await db.query("orders", "insert")

    order_id = len(db.ORDERS) + 1
    db.ORDERS[order_id] = {"id": order_id, **order.model_dump(), "total": total}

    chaos.leak()
    cache = chaos.leaked_bytes()
    ORDER_CACHE_BYTES.set(cache)
    if cache - _last_leak_warning >= LEAK_WARN_STEP:
        _last_leak_warning = cache
        log.warning("Order cache size is %d MB and growing", cache // (1024 * 1024),
                    extra={"fields": {"cache_bytes": cache}})
    return db.ORDERS[order_id]


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    await db.query("orders", "select")
    if order_id not in db.ORDERS:
        raise HTTPException(404, "order not found")
    return db.ORDERS[order_id]


# --- Ops endpoints --------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/chaos")
async def get_chaos():
    return chaos.state


@app.post("/chaos")
async def set_chaos(changes: dict):
    try:
        return chaos.update(**changes)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.delete("/chaos")
async def reset_chaos():
    global _last_leak_warning
    _last_leak_warning = 0
    ORDER_CACHE_BYTES.set(0)
    return chaos.reset()
