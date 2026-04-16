"""Steno API — entry point."""

from dotenv import load_dotenv
from fastapi import FastAPI

from api.routers import transcriptions, search

load_dotenv()

app = FastAPI(title="Steno", docs_url="/docs")


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(transcriptions.router)
app.include_router(search.router)
