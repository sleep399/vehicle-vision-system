from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent

LLM_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openai": {"base": "https://api.openai.com/v1", "model": "gpt-3.5-turbo", "label": "OpenAI"},
    "qwen": {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-turbo", "label": "通义千问"},
    "deepseek": {"base": "https://api.deepseek.com/v1", "model": "deepseek-chat", "label": "DeepSeek"},
    "zhipu": {"base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "label": "智谱 GLM"},
}


class Settings(BaseSettings):
    app_name: str = "车载视觉感知与人机交互系统"
    secret_key: str = "dev-secret-key-change-in-production"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8001
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

    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_api_base: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-3.5-turbo"
    llm_timeout: float = 30.0
    llm_max_tokens: int = 800
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_timeout: float = 10.0
    alert_email_to: str = ""
    webhook_url: str = ""
    ccpd_data_path: str = "../CCPD-master"
    yolo_lprnet_path: str = "./yolo_lprnet_assets"
    ctpgr_data_path: str = "../ctpgr-pytorch-master"
    hagrid_data_path: str = "../hagrid-master"
    # 由 setup_security.py 为每台开发机单独生成，不提供可用于生产的默认值。
    aes_key: str = ""
    https_certfile: str = "certs/localhost-cert.pem"
    https_keyfile: str = "certs/localhost-key.pem"
    access_token_expire_minutes: int = 60 * 24
    gesture_hold_threshold: float = 0.8
    alert_failure_threshold: int = 5
    low_confidence_threshold: float = 0.4
    lpr_min_confidence: float = 0.5
    alert_window_seconds: int = 300
    alert_cooldown_seconds: int = 60
    alert_config_cooldown_seconds: int = 3600
    alert_token_warning_threshold: int = 80000
    alert_token_critical_threshold: int = 95000
    alert_token_limit: int = 100000
    alert_anomaly_rate_threshold: float = 0.3
    alert_sse_enabled: bool = True
    alert_webhook_enabled: bool = False
    alert_email_enabled: bool = False
    police_pose_backend: str = "yolo"
    police_yolo_pose_model: str = "yolo11s-pose.pt"
    police_gesture_model: str = "lstm_yolo11s.pt"
    police_pose_hold_frames: int = 5

    class Config:
        env_file = str(BASE_DIR / ".env")
        env_file_encoding = "utf-8"
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

    @property
    def effective_llm_base(self) -> str:
        if self.llm_api_base:
            return self.llm_api_base.rstrip("/")
        return LLM_PROVIDER_PRESETS.get(self.llm_provider.lower(), LLM_PROVIDER_PRESETS["openai"])["base"].rstrip("/")

    @property
    def effective_llm_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return LLM_PROVIDER_PRESETS.get(self.llm_provider.lower(), LLM_PROVIDER_PRESETS["openai"])["model"]

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key.strip())

    @property
    def llm_provider_label(self) -> str:
        preset = LLM_PROVIDER_PRESETS.get(self.llm_provider.lower())
        return preset["label"] if preset else "自定义"


settings = Settings()
