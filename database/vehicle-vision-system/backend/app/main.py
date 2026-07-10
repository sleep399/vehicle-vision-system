from app.utils.quiet_logs import configure_quiet_logs

configure_quiet_logs()

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from app.config import settings
from app.database import init_db
from app.models.user import User
from app.database import SessionLocal
from app.utils.auth import hash_password
from app.routers import auth, lpr, police_gesture, owner_gesture, monitor, websocket


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        try:
            admin = db.query(User).filter(User.username == "admin").first()
        except Exception:
            admin = None
        if not admin:
            db.execute(
                User.__table__.insert().values(
                    username="admin",
                    email="admin@demo.com",
                    hashed_password=hash_password("admin123"),
                    is_active=True,
                )
            )
            db.commit()
    finally:
        db.close()
    yield


app = FastAPI(
    title=settings.app_name,
    description="车载摄像头视觉感知与人机交互系统 - 车牌识别、交警手势、车主手势控车、告警智能体",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(lpr.router)
app.include_router(police_gesture.router)
app.include_router(owner_gesture.router)
app.include_router(monitor.router)
app.include_router(websocket.router)

static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
uploads_dir = settings.upload_dir
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")


@app.get("/hls/{path_name}/index.m3u8", include_in_schema=False)
async def hls_playlist(path_name: str):
    hls_file = settings.hls_dir / path_name / "index.m3u8"
    if hls_file.exists():
        return FileResponse(str(hls_file))
    return {"detail": "HLS playlist not found"}


@app.get("/", include_in_schema=False)
async def index():
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "车载视觉感知系统 API", "docs": "/api/docs"}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    if response.status_code == 401 and request.url.path.startswith("/api/") and "auth" not in request.url.path:
        pass
    return response
