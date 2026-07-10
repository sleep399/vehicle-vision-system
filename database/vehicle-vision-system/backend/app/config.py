from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    app_name: str = "车载视觉感知与人机交互系统"
    secret_key: str = "dev-secret-key-change-in-production"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = ""
    database_echo: bool = False
    odbc_driver: str = "ODBC Driver 17 for SQL Server"
    log_level: str = "INFO"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = self.data_dir / "app.db"
        return f"sqlite:///{db_path.as_posix()}"

    llm_api_key: str = ""
    llm_api_base: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-3.5-turbo"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_to: str = ""
    webhook_url: str = ""
    ccpd_data_path: str = "../CCPD-master"
    yolo_lprnet_path: str = "./yolo_lprnet_assets"
    ctpgr_data_path: str = "../ctpgr-pytorch-master"
    hagrid_data_path: str = "../hagrid-master"
    aes_key: str = "0123456789abcdef0123456789abcdef"
    access_token_expire_minutes: int = 60 * 24
    gesture_hold_threshold: float = 0.8
    alert_failure_threshold: int = 5
    low_confidence_threshold: float = 0.4

    class Config:
        env_file = str(BASE_DIR / ".env")
        extra = "ignore"

    @property
    def base_dir(self) -> Path:
        return BASE_DIR

    @property
    def upload_dir(self) -> Path:
        p = self.base_dir / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_dir(self) -> Path:
        p = self.base_dir / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def hls_dir(self) -> Path:
        p = self.base_dir / "hls"
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
