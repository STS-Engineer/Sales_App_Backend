import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jwt

from app.config import settings
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ── Outlook SMTP relay settings ──────────────────────────────────────
SMTP_SERVER = "avocarbon-com.mail.protection.outlook.com"
SMTP_PORT = 25
SMTP_FROM = "administration.STS@avocarbon.com"


def create_access_token(email: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": email, "role": role, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def _get_owner_emails(db: AsyncSession) -> list[str]:
    owner_result = await db.execute(
        select(User.email).where(
            User.role == UserRole.OWNER,
            User.is_approved.is_(True),
        )
    )
    return list(owner_result.scalars().all())


def _send_new_signup_email(owner_email: str, new_user_email: str, full_name: str | None) -> None:
    """Send an SMTP notification to the owner about a new signup."""
    display_name = full_name or new_user_email
    frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:5173")
    approval_link = f"{frontend_url}/users/validation"

    msg = EmailMessage()
    msg["Subject"] = f"New Account Pending Approval: {display_name}"
    msg["From"] = SMTP_FROM
    msg["To"] = owner_email

    text_body = f"""Hello,

A new user has registered on the AVO Carbon RFQ Portal and is awaiting your approval.

Name: {display_name}
Email: {new_user_email}

Please log in to the admin panel to review and approve this account:
{approval_link}

Best regards,
AVO Carbon RFQ System
"""
    msg.set_content(text_body)

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333333; line-height: 1.6; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; border: 1px solid #e0e0e0;">
          <h2 style="color: #1a365d; margin-top: 0;">New Account Pending Approval</h2>
          <p>Hello,</p>
          <p>A new user has registered on the <strong>AVO Carbon RFQ Portal</strong> and is awaiting your approval.</p>

          <table style="margin: 20px 0; border-collapse: collapse; width: 100%;">
            <tr>
              <td style="padding: 8px 12px; font-weight: bold; color: #666;">Name</td>
              <td style="padding: 8px 12px;">{display_name}</td>
            </tr>
            <tr style="background-color: #f9f9f9;">
              <td style="padding: 8px 12px; font-weight: bold; color: #666;">Email</td>
              <td style="padding: 8px 12px;">{new_user_email}</td>
            </tr>
          </table>

          <div style="margin: 30px 0; text-align: center;">
            <a href="{approval_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Review &amp; Approve Account
            </a>
          </div>

          <hr style="border: none; border-top: 1px solid #eeeeee; margin: 30px 0;">
          <p style="font-size: 12px; color: #999999; margin-bottom: 0;">
            Best regards,<br>
            <strong>AVO Carbon RFQ System</strong>
          </p>
        </div>
      </body>
    </html>
    """
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.send_message(msg)
    except Exception as e:
        # Log but don't crash — the user account is already created
        print(f"SMTP Error (new signup notification): {e}")


async def _create_pending_user(body: RegisterRequest, db: AsyncSession) -> dict[str, str]:
    # Check for duplicate email
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered.")

    user = User(
        email=body.email,
        full_name=body.full_name,
        role=UserRole.COMMERCIAL,
        is_approved=False,
    )
    user.set_password(body.password)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    owner_emails = await _get_owner_emails(db)
    for owner_email in owner_emails:
        _send_new_signup_email(owner_email, body.email, body.full_name)

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

    # Check approval status instead of PENDING role
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

    token = create_access_token(email, role)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    """Returns the currently authenticated user's profile."""
    return current_user
