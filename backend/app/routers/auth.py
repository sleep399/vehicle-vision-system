import random
import uuid
from datetime import datetime, timedelta
from io import BytesIO
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.user import User, VerificationCode, WechatLoginSession
from app.schemas import Token, UserCreate, UserLogin, CodeLoginRequest, SendCodeRequest
from app.utils.auth import hash_password, verify_password, create_access_token, get_current_user, require_user
from app.utils.logger import write_log
from app.services.alert_agent import alert_agent

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", response_model=Token, summary="账号密码注册")
def register(data: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(400, "用户名已存在")
    user = User(
        username=data.username,
        email=data.email,
        phone=data.phone,
        hashed_password=hash_password(data.password),
    )
    db.add(user)
    db.commit()
    write_log(db, "user", f"用户注册: {data.username}")
    token = create_access_token({"sub": user.username})
    return Token(access_token=token)


@router.post("/login", response_model=Token, summary="账号密码登录")
def login(data: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()
    if not user or not user.is_active or not user.hashed_password or not verify_password(data.password, user.hashed_password):
        write_log(db, "user", f"登录失败: {data.username}", level="WARN")
        raise HTTPException(401, "用户名或密码错误")
    if not user.hashed_password.startswith("$2"):
        user.hashed_password = hash_password(data.password)
        db.commit()
    write_log(db, "user", f"用户登录: {data.username}", user_id=user.id)
    return Token(access_token=create_access_token({"sub": user.username}))


@router.post("/send-code", summary="发送邮箱/手机验证码")
def send_code(data: SendCodeRequest, db: Session = Depends(get_db)):
    if data.target_type not in {"email", "phone"} or not data.target.strip():
        raise HTTPException(400, "请提供有效的邮箱或手机号")
    code = f"{random.randint(100000, 999999)}"
    vc = VerificationCode(
        target=data.target,
        code=code,
        purpose="login",
        expires_at=datetime.utcnow() + timedelta(minutes=5),
    )
    db.add(vc)
    db.commit()
    write_log(db, "user", f"验证码已发送至 {data.target}", detail={"code": code, "type": data.target_type})
    # This project has no SMTP/SMS provider configuration. Returning the code
    # keeps the flow testable in demo mode and must be removed in production.
    return {"message": "验证码已发送（演示模式）", "code": code, "expires_in": 300}


@router.post("/login-code", response_model=Token, summary="验证码登录")
def login_with_code(data: CodeLoginRequest, db: Session = Depends(get_db)):
    vc = (
        db.query(VerificationCode)
        .filter(VerificationCode.target == data.target, VerificationCode.used == False, VerificationCode.expires_at > datetime.utcnow())
        .order_by(VerificationCode.id.desc())
        .first()
    )
    if not vc or vc.code != data.code:
        raise HTTPException(400, "验证码无效或已过期")
    vc.used = True
    field = User.email if data.target_type == "email" else User.phone
    user = db.query(User).filter(field == data.target).first()
    if not user:
        username = data.target.split("@")[0] if "@" in data.target else data.target
        user = User(username=username, email=data.target if data.target_type == "email" else None, phone=data.target if data.target_type == "phone" else None)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not user.is_active:
        raise HTTPException(403, "用户已被禁用")
    db.commit()
    write_log(db, "user", f"验证码登录: {data.target}", user_id=user.id)
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
        user = User(username="wechat_demo_user", email="wechat-demo@local")
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
    return {"id": user.id, "username": user.username, "email": user.email, "phone": user.phone}
