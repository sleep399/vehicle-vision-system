import secrets
import hmac
import uuid
from datetime import datetime, timedelta
from io import BytesIO
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.user import User, VerificationCode, WechatLoginSession
from app.schemas import Token, UserCreate, UserLogin, CodeLoginRequest, SendCodeRequest
from app.services.auth_email import EmailDeliveryError, send_verification_email
from app.utils.auth import hash_password, verify_password, create_access_token, require_user
from app.utils.crypto import hash_verification_code
from app.utils.logger import write_log
from app.utils.privacy import email_lookup, normalize_email, protect_email, user_email, user_phone

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", response_model=Token, summary="账号密码注册")
def register(data: UserCreate, db: Session = Depends(get_db)):
    email = normalize_email(str(data.email))
    lookup = email_lookup(email)
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(400, "用户名已存在")
    if db.query(User).filter(User.email_lookup == lookup).first():
        raise HTTPException(400, "该邮箱已注册")
    vc = _get_valid_code(db, email, "register", data.verification_code)
    user = User(
        username=data.username,
        hashed_password=hash_password(data.password),
        **protect_email(email),
    )
    vc.used = True
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "用户名或邮箱已被占用，请更换后重试") from exc
    write_log(db, "user", f"用户注册: {data.username}", user_id=user.id)
    token = create_access_token({"sub": user.username})
    return Token(access_token=token)


@router.post("/login", response_model=Token, summary="账号密码登录")
def login(data: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user or not user.is_active or not user.hashed_password or not verify_password(data.password, user.hashed_password):
        write_log(
            db,
            "user",
            f"登录失败: {data.username}",
            level="WARN",
            user_id=user.id if user else None,
        )
        raise HTTPException(401, "用户名或密码错误")
    if not user.hashed_password.startswith("$2"):
        user.hashed_password = hash_password(data.password)
        db.commit()
    write_log(db, "user", f"用户登录: {data.username}", user_id=user.id)
    return Token(access_token=create_access_token({"sub": user.username}))


@router.post("/send-code", summary="发送邮箱验证码")
def send_code(data: SendCodeRequest, db: Session = Depends(get_db)):
    email = normalize_email(str(data.email))
    lookup = email_lookup(email)
    user = db.query(User).filter(User.email_lookup == lookup).first()
    if data.purpose == "login":
        if not user:
            raise HTTPException(404, "该邮箱尚未注册")
        if not user.is_active:
            raise HTTPException(403, "用户已被禁用")
    elif user:
        raise HTTPException(400, "该邮箱已注册")

    latest = (
        db.query(VerificationCode)
        .filter(
            VerificationCode.target_lookup == lookup,
            VerificationCode.purpose == data.purpose,
            VerificationCode.used == False,
        )
        .order_by(VerificationCode.id.desc())
        .first()
    )
    now = datetime.utcnow()
    if latest and latest.expires_at > now + timedelta(minutes=4):
        raise HTTPException(429, "验证码发送过于频繁，请稍后再试")

    code = f"{secrets.randbelow(900000) + 100000}"
    try:
        send_verification_email(email, code, data.purpose)
    except EmailDeliveryError as exc:
        raise HTTPException(503, str(exc)) from exc

    db.query(VerificationCode).filter(
        VerificationCode.target_lookup == lookup,
        VerificationCode.purpose == data.purpose,
        VerificationCode.used == False,
    ).update({VerificationCode.used: True}, synchronize_session=False)
    vc = VerificationCode(
        target=lookup,
        code="hashed",
        target_lookup=lookup,
        code_hash=hash_verification_code(email, data.purpose, code),
        purpose=data.purpose,
        expires_at=now + timedelta(minutes=5),
    )
    db.add(vc)
    db.commit()
    write_log(
        db,
        "user",
        f"{data.purpose} 验证码已发送",
        user_id=user.id if user else None,
    )
    return {"message": "验证码已发送，请查收邮件", "expires_in": 300}


def _get_valid_code(db: Session, email: str, purpose: str, code: str) -> VerificationCode:
    lookup = email_lookup(email)
    vc = (
        db.query(VerificationCode)
        .filter(
            VerificationCode.target_lookup == lookup,
            VerificationCode.purpose == purpose,
            VerificationCode.used == False,
            VerificationCode.expires_at > datetime.utcnow(),
        )
        .order_by(VerificationCode.id.desc())
        .first()
    )
    expected_hash = hash_verification_code(email, purpose, code)
    if not vc or not vc.code_hash or not hmac.compare_digest(vc.code_hash, expected_hash):
        raise HTTPException(400, "验证码无效或已过期")
    return vc


@router.post("/login-code", response_model=Token, summary="验证码登录")
def login_with_code(data: CodeLoginRequest, db: Session = Depends(get_db)):
    email = normalize_email(str(data.email))
    user = db.query(User).filter(User.email_lookup == email_lookup(email)).first()
    if not user:
        raise HTTPException(404, "该邮箱尚未注册")
    if not user.is_active:
        raise HTTPException(403, "用户已被禁用")
    vc = _get_valid_code(db, email, "login", data.code)
    vc.used = True
    db.commit()
    write_log(db, "user", "验证码登录成功", user_id=user.id)
    return Token(access_token=create_access_token({"sub": user.username}))


@router.post("/wechat/qrcode", summary="获取微信扫码登录会话")
def wechat_qrcode(db: Session = Depends(get_db)):
    session_id = uuid.uuid4().hex
    session = WechatLoginSession(session_id=session_id, status="pending")
    db.add(session)
    db.commit()
    qrcode_url = f"/api/auth/wechat/qrcode/{session_id}"
    write_log(db, "user", "创建微信扫码登录会话", detail={"session_id": session_id, "qrcode_url": qrcode_url})
    return {"session_id": session_id, "qrcode_url": qrcode_url, "poll_url": f"/api/auth/wechat/poll/{session_id}"}


@router.get("/wechat/qrcode/{session_id}", summary="获取微信扫码登录二维码")
def wechat_qrcode_image(session_id: str, request: Request, db: Session = Depends(get_db)):
    session = db.query(WechatLoginSession).filter(WechatLoginSession.session_id == session_id).first()
    if not session:
        raise HTTPException(404, "会话不存在")
    confirm_url = str(request.url_for("wechat_confirm_page", session_id=session_id))
    image = qrcode.make(confirm_url)
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return StreamingResponse(output, media_type="image/png")


@router.get("/wechat/confirm/{session_id}", response_class=HTMLResponse, name="wechat_confirm_page", include_in_schema=False)
def wechat_confirm_page(session_id: str, db: Session = Depends(get_db)):
    session = db.query(WechatLoginSession).filter(WechatLoginSession.session_id == session_id).first()
    if not session:
        raise HTTPException(404, "会话不存在")
    return HTMLResponse(f"""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>微信扫码登录</title>
    <body><h2>微信扫码登录（演示模式）</h2><p>确认后，浏览器中的登录会话将完成。</p>
    <button onclick=\"fetch('/api/auth/wechat/confirm/{session_id}',{{method:'POST'}}).then(()=>document.body.innerHTML='<h2>已确认，请返回电脑端</h2>')\">确认登录</button></body></html>""")


@router.post("/wechat/confirm/{session_id}", summary="确认微信扫码登录（演示）")
def wechat_confirm(session_id: str, db: Session = Depends(get_db)):
    session = db.query(WechatLoginSession).filter(WechatLoginSession.session_id == session_id).first()
    if not session:
        raise HTTPException(404, "会话不存在")
    if session.status == "confirmed":
        return {"status": "confirmed"}
    user = db.query(User).filter(User.username == "wechat_demo_user").first()
    if not user:
        user = User(username="wechat_demo_user", **protect_email("wechat-demo@local"))
        db.add(user)
        db.flush()
    session.status = "confirmed"
    session.user_id = user.id
    db.commit()
    write_log(db, "user", "微信扫码登录确认（演示）", user_id=user.id)
    return {"status": "confirmed"}


@router.get("/wechat/poll/{session_id}", summary="轮询微信扫码状态")
def wechat_poll(session_id: str, db: Session = Depends(get_db)):
    session = db.query(WechatLoginSession).filter(WechatLoginSession.session_id == session_id).first()
    if not session:
        raise HTTPException(404, "会话不存在")
    if session.status == "confirmed" and session.user_id:
        user = db.get(User, session.user_id)
        token = create_access_token({"sub": user.username})
        return {"status": "confirmed", "access_token": token}
    return {"status": session.status}


@router.post("/logout", summary="退出登录")
def logout(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    write_log(
        db, "user", f"用户退出: {user.username}",
        level="INFO",
        detail={"ip": client_ip},
        user_id=user.id,
    )
    return {"message": "已退出登录"}


@router.get("/me", summary="当前用户信息")
def me(user: User = Depends(require_user)):
    return {
        "id": user.id,
        "username": user.username,
        "email": user_email(user),
        "phone": user_phone(user),
    }
