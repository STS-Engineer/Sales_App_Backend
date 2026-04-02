import logging
import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware.auth import require_role
from app.models.user import User, UserRole
from app.schemas.auth import ApproveUserRequest, UserOut
from app.schemas.user import RoleUpdateRequest, UserOut as UserOutFull

router = APIRouter(prefix="/api/owner", tags=["owner"])
logger = logging.getLogger(__name__)

# ── Outlook SMTP relay settings ──────────────────────────────────────
SMTP_SERVER = "avocarbon-com.mail.protection.outlook.com"
SMTP_PORT = 25
SMTP_FROM = "administration.STS@avocarbon.com"


def _send_approval_email(user_email: str, _user_name: str | None, assigned_role: str) -> None:
    """Send an SMTP confirmation email to the newly approved user."""
    frontend_url = settings.frontend_url
    login_link = frontend_url
    approval_message = (
        "Hello, your account for the AVO Carbon RFQ Portal has been approved. "
        f"You have been assigned the role of {assigned_role}. You may now log in."
    )

    msg = EmailMessage()
    msg["Subject"] = "Your AVO Carbon RFQ Account Has Been Approved"
    msg["From"] = SMTP_FROM
    msg["To"] = user_email

    text_body = f"""{approval_message}

You can now log in here:
{login_link}

Best regards,
AVO Carbon RFQ System
"""
    msg.set_content(text_body)

    html_body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333333; line-height: 1.6; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; border: 1px solid #e0e0e0;">
          <h2 style="color: #1a365d; margin-top: 0;">Account Approved ✓</h2>
          <p>{approval_message}</p>

          <div style="margin: 30px 0; text-align: center;">
            <a href="{login_link}" style="background-color: #16a34a; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Log In to RFQ Portal
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
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
            server.ehlo()
            server.sendmail(SMTP_FROM, [user_email], msg.as_string())
        logger.info("Approval email sent to %s with role %s", user_email, assigned_role)
    except Exception as e:
        logger.exception("SMTP Error (approval notification) for %s: %s", user_email, e)


def _format_role_label(role: UserRole) -> str:
    return role.value.replace("_", " ").title()


async def _apply_user_role_update(
    user: User,
    role: UserRole,
    db: AsyncSession,
    *,
    region: str | None = None,
) -> User:
    if user.role == UserRole.OWNER:
        raise HTTPException(status_code=400, detail="Owner role cannot be edited here.")

    user.role = role
    user.is_approved = True
    if region is not None:
        user.region = region

    await db.commit()
    await db.refresh(user)
    _send_approval_email(user.email, user.full_name, _format_role_label(role))
    return user


@router.get("/users", response_model=list[UserOutFull])
async def list_all_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """
    Returns all registered non-owner users in the system.
    Only accessible by users with the OWNER role.
    """
    result = await db.execute(
        select(User)
        .where(User.role != UserRole.OWNER)
        .order_by(User.created_at.desc())
    )
    return result.scalars().all()


@router.post("/users/{user_id}/approve", response_model=UserOut)
async def approve_user(
    user_id: str,
    body: ApproveUserRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """
    Owner approves a pending user and assigns their role.
    Sets is_approved=True and sends a confirmation email.
    """
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if user.is_approved:
        raise HTTPException(status_code=400, detail="User is already approved.")
    return await _apply_user_role_update(user, body.role, db, region=body.region)


@router.put("/users/{user_id}/role", response_model=UserOutFull)
async def update_user_role(
    user_id: str,
    body: RoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role(UserRole.OWNER)),
):
    """
    Update an existing user's role. Owner accounts are intentionally excluded.
    """
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return await _apply_user_role_update(user, body.role, db)
