from fastapi import FastAPI

from src.config import settings

app = FastAPI(title="affine-ingest", version=settings.version)


@app.get("/health")
async def health() -> dict:
    """Liveness + minimal observability.

    queue_depth and worker_alive are hardcoded in Phase 1; wired up in Phase 6
    once the worker loop and DB layer exist.
    """
    return {
        "ok": True,
        "queue_depth": 0,
        "worker_alive": False,
        "version": settings.version,
    }
