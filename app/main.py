from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.router import api_router

app = FastAPI(
    title="Asset Management System (AMS) API",
    description="Industrial Asset Management System — Equipment Register API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=False,                  # sign-in uses a bearer token, not cookies
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Documents are NOT served from a public folder: they live in the database and are given only to signed-in people
# (GET /api/v1/equipment/documents/{id}, and the report files).

app.include_router(api_router)


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "AMS API", "version": "1.0.0"}
