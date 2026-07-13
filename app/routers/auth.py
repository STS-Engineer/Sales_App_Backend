import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import quote

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User, UserRole
from app.schemas.auth import (
    AccessTokenResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RefreshTokenRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserOut,
)
from app.security import (
    build_password_reset_fingerprint,
    create_access_token,
    create_password_reset_token,
    create_refresh_token,
    decode_token,
)
from app.utils import emails
from app.utils.time import local_now
from app.utils.user_agent import parse_os_from_user_agent

router = APIRouter(prefix="/api/auth", tags=["auth"])
PASSWORD_RESET_REQUEST_MESSAGE = (
    "If an account exists for that email, a password reset link has been sent."
)
PASSWORD_RESET_INVALID_MESSAGE = (
    "This password reset link is invalid or has expired."
)
PASSWORD_RESET_SUCCESS_MESSAGE = (
    "Your password has been reset. You can now sign in with your new password."
)


async def _get_owner_emails(db: AsyncSession) -> list[str]:
    owner_result = await db.execute(
        select(User.email).where(
            User.role == UserRole.OWNER,
            User.is_approved.is_(True),
        )
    )
    return list(owner_result.scalars().all())


async def _create_pending_user(body: RegisterRequest, db: AsyncSession) -> dict[str, str]:
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered.")

    normalized_full_name = body.full_name.strip()
    if not normalized_full_name:
        raise HTTPException(status_code=400, detail="Full name is required.")

    user = User(
        email=body.email,
        full_name=normalized_full_name,
        role=UserRole.COMMERCIAL,
        is_approved=False,
    )
    user.set_password(body.password)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    owner_emails = await _get_owner_emails(db)
    approval_link = f"{settings.frontend_url}/users/validation"
    for owner_email in owner_emails:
        emails.send_new_signup_email(
            owner_email,
            body.email,
            normalized_full_name,
            approval_link,
        )

    return {
        "message": "Signup successful. Your account is pending approval by the system Owner."
    }


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    normalized_email = str(email or "").strip()
    if not normalized_email:
        return None
    result = await db.execute(select(User).where(User.email == normalized_email))
    return result.scalar_one_or_none()


def _password_reset_http_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=PASSWORD_RESET_INVALID_MESSAGE,
    )


async def _resolve_password_reset_user(
    db: AsyncSession,
    token: str,
) -> User:
    invalid_reset_exception = _password_reset_http_exception()
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        raise invalid_reset_exception from exc

    if payload.get("token_type") != "password_reset":
        raise invalid_reset_exception

    email = str(payload.get("sub") or "").strip()
    fingerprint = str(payload.get("pwd") or "").strip()
    if not email or not fingerprint:
        raise invalid_reset_exception

    user = await _get_user_by_email(db, email)
    if user is None:
        raise invalid_reset_exception

    if build_password_reset_fingerprint(user.password_hash) != fingerprint:
        raise invalid_reset_exception

    return user


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    return await _create_pending_user(body, db)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    return await _create_pending_user(body, db)


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_by_email(db, body.email)
    if user is not None:
        token = create_password_reset_token(user.email, user.password_hash)
        reset_link = (
            f"{settings.frontend_url}/reset-password"
            f"?token={quote(token, safe='')}"
        )
        emails.send_password_reset_email(
            user.email,
            user.full_name,
            reset_link,
            settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES,
        )

    return MessageResponse(message=PASSWORD_RESET_REQUEST_MESSAGE)


@router.get("/reset-password/validate", response_model=MessageResponse)
async def validate_reset_password_token(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    await _resolve_password_reset_user(db, token)
    return MessageResponse(message="Password reset token is valid.")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _resolve_password_reset_user(db, body.token)
    new_password = str(body.password or "")
    if user.check_password(new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current password.",
        )

    user.set_password(new_password)
    await db.commit()

    return MessageResponse(message=PASSWORD_RESET_SUCCESS_MESSAGE)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_by_email(db, body.email)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if not user.check_password(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if not user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account pending approval. Please contact the system Owner.",
        )

    email = user.email
    role = user.role.value

    if user.needs_password_rehash():
        user.set_password(body.password)

    user.last_login = local_now()
    user.operating_system = parse_os_from_user_agent(request.headers.get("user-agent"))
    await db.commit()

    access_token = create_access_token(email, role)
    refresh_token = create_refresh_token(email, role)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
    )


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh_token(body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid refresh token.",
    )

    try:
        payload = decode_token(body.refresh_token)
    except jwt.PyJWTError as exc:
        raise credentials_exception from exc

    if payload.get("token_type") != "refresh":
        raise credentials_exception

    email = str(payload.get("sub") or "").strip()
    if not email:
        raise credentials_exception

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_approved:
        raise credentials_exception

    access_token = create_access_token(user.email, user.role.value)
    return AccessTokenResponse(access_token=access_token)


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    all_roles = sorted(
        current_user.__dict__.get("_all_roles", {current_user.role.value})
    )
    return UserOut(
        user_id=current_user.user_id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role.value,
        roles=all_roles,
        is_approved=current_user.is_approved,
        region=current_user.region,
    )
