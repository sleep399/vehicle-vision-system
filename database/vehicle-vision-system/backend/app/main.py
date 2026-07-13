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
from app.database import init_db, check_db_connection
from app.models.user import User
from app.database import SessionLocal
from app.utils.auth import hash_password
from app.utils.privacy import protect_email
from app.routers import auth, lpr, police_gesture, owner_gesture, monitor, websocket, scenario
from app.services.alert_agent import alert_agent
from app.services.llm_service import llm_service
from app.services.lpr_service import lpr_service
from app.services.lpr_video_service import lpr_video_service
from app.services.police_gesture_service import police_gesture_service
from app.utils.logger import get_logger, write_log, write_system_log

main_logger = get_logger("main")


async def _startup_checks(db):
    write_system_log(db, "系统启动完成", detail={"version": "1.0.0"})
    db_ok = check_db_connection()
    alert_agent.record_db_connection(db_ok)
    if not db_ok:
        await alert_agent.check_and_alert(db, "db")
    if settings.alert_webhook_enabled and not settings.webhook_url:
        await alert_agent.handle_config_missing(db, "webhook_url", severity="warning")
    if settings.alert_email_enabled and not all((settings.smtp_host, settings.smtp_user, settings.alert_email_to)):
        await alert_agent.handle_config_missing(db, "smtp/email", severity="warning")
    if not settings.llm_configured:
        write_system_log(db, "LLM 未配置，告警摘要使用本地模板", level="WARN")
    else:
        status = await llm_service.test_connection()
        write_system_log(db, "LLM 连接正常" if status.get("ok") else "LLM 连接失败，使用模板降级", level="INFO" if status.get("ok") else "WARN", detail=status)

    image_model_ready = lpr_service.model_available()
    write_system_log(
        db,
        "车牌图片识别模型已就绪（RPNet）" if image_model_ready else "车牌图片识别模型未加载（RPNet）",
        level="INFO" if image_model_ready else "WARN",
        detail={"engine": "rpnet", "model": "fh02.pth", "ready": image_model_ready},
    )
    if not image_model_ready:
        await alert_agent.handle_model_load_failure(
            db, "fh02.pth", FileNotFoundError("RPNet 模型 fh02.pth 未就绪"),
        )

    video_status = lpr_video_service.model_status()
    write_system_log(
        db,
        "车牌视频识别模型已就绪（YOLO+LPRNet）" if video_status.get("model_available") else "车牌视频识别模型未加载（YOLO+LPRNet）",
        level="INFO" if video_status.get("model_available") else "WARN",
        detail=video_status,
    )
    if not video_status.get("model_available"):
        await alert_agent.handle_model_load_failure(
            db, "yolo_lprnet", FileNotFoundError(video_status.get("message") or "YOLO+LPRNet 权重未就绪"),
        )

    try:
        pose_info = police_gesture_service.pose_backend_info()
        write_system_log(db, "交警手势识别后端已配置", level="INFO", detail=pose_info)
    except Exception as exc:
        write_system_log(db, "交警手势识别后端检查失败", level="WARN", detail={"error": str(exc)})
        await alert_agent.handle_model_load_failure(db, "police_pose", exc)


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
                    hashed_password=hash_password("admin123"),
                    is_active=True,
                    **protect_email("admin@demo.com"),
                )
            )
            db.commit()
        await _startup_checks(db)
    finally:
        db.close()
    await alert_agent.start_patrol_loop(SessionLocal)
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
app.include_router(scenario.router)
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
    if response.status_code in {401, 403} and request.url.path.startswith("/api/"):
        db = SessionLocal()
        try:
            client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
            await alert_agent.handle_unauthorized_access(db, request.url.path, ip=client_ip, user_agent=request.headers.get("user-agent"))
            write_log(db, "user", f"未授权访问: {request.url.path}", level="WARN", detail={"ip": client_ip, "status": response.status_code})
        except Exception as exc:
            main_logger.warning("未授权访问检测失败: %s", exc)
        finally:
            db.close()
    return response
