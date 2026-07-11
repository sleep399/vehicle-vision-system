from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str
    password: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None


class UserLogin(BaseModel):
    username: str
    password: str


class CodeLoginRequest(BaseModel):
    target: str
    code: str
    target_type: str = "email"


class SendCodeRequest(BaseModel):
    target: str
    target_type: str = "email"


class PlateResult(BaseModel):
    plate_number: str
    plate_color: str = "蓝牌"
    bbox: list[int]
    indices: list[int] = []
    confidence: float = 1.0
    source: Optional[str] = None


class LPRResponse(BaseModel):
    plates: list[PlateResult]
    plate_count: int
    annotated_image: str
    success: bool
    record_id: Optional[int] = None
    source: Optional[str] = None
    model_available: Optional[bool] = None


class GestureResponse(BaseModel):
    gesture: str
    gesture_cn: str
    confidence: float
    annotated_image: str
    keypoints: list
    success: bool
    record_id: Optional[int] = None
    action: Optional[str] = None


class VehicleStateResponse(BaseModel):
    volume: int
    temperature: int
    phone_status: str
    current_page: str
    is_awake: int


class AlertResponse(BaseModel):
    id: int
    level: str
    event_type: str
    title: str
    summary: str
    root_cause: Optional[str]
    suggestion: Optional[str]
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class LogResponse(BaseModel):
    id: int
    category: str
    level: str
    message: str
    detail_json: dict | None = None
    user_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True
