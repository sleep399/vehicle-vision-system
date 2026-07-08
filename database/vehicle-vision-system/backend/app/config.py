from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    app_name: str = "Vehicle Vision System"
    secret_key: str = "dev-secret-key-change-in-production"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = ""
    database_echo: bool = False
    access_token_expire_minutes: int = 60 * 24
    low_confidence_threshold: float = 0.4
    police_pose_backend: str = "ctpgr"
    police_yolo_pose_model: str = "yolov8n-pose.pt"
    police_yolo_keypoint_conf: float = 0.25

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    @property
    def upload_dir(self) -> Path:
        path = self.base_dir / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def data_dir(self) -> Path:
        path = self.base_dir / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
