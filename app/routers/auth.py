import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User, UserRole
from app.schemas.auth import (
    AccessTokenResponse,
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.security import create_access_token, create_refresh_token, decode_token
from app.utils import emails

router = APIRouter(prefix="/api/auth", tags=["auth"])


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


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    return await _create_pending_user(body, db)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    return await _create_pending_user(body, db)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

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
    return current_user
