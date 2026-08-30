from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.profiles import router as profiles_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="linkedin-profile-api")
app.include_router(profiles_router)


@app.get("/")
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
