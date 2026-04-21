import logging
import smtplib
from email.message import EmailMessage

from app.config import settings

logger = logging.getLogger(__name__)
SMTP_TIMEOUT_SECONDS = 20


def _normalize_systematic_rfq_id(value: str | None) -> str:
    return str(value or "").strip()


def _rfq_id_subject_suffix(systematic_rfq_id: str | None) -> str:
    normalized = _normalize_systematic_rfq_id(systematic_rfq_id)
    return f": {normalized}" if normalized else ""


def _rfq_id_text_block(systematic_rfq_id: str | None) -> str:
    normalized = _normalize_systematic_rfq_id(systematic_rfq_id)
    return f"RFQ ID: {normalized}\n" if normalized else ""


def _rfq_id_html_item(systematic_rfq_id: str | None) -> str:
    normalized = _normalize_systematic_rfq_id(systematic_rfq_id)
    if not normalized:
        return ""
    return f"<li><strong>RFQ ID:</strong> {normalized}</li>"


def _normalize_email_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    else:
        candidates = list(value)

    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        email = str(item or "").strip()
        if not email:
            continue
        lowered = email.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(email)
    return normalized


def _build_base_html(title: str, body_html: str) -> str:
    return f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #333333; line-height: 1.6; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 8px; border: 1px solid #e0e0e0;">
          <h2 style="color: #1a365d; margin-top: 0;">{title}</h2>
          {body_html}
          <hr style="border: none; border-top: 1px solid #eeeeee; margin: 30px 0;">
          <p style="font-size: 12px; color: #999999; margin-bottom: 0;">
            Best regards,<br>
            <strong>AVO Carbon RFQ System</strong>
          </p>
        </div>
      </body>
    </html>
    """


def _login_if_configured(server: smtplib.SMTP) -> None:
    if settings.smtp_user and settings.smtp_password:
        server.login(settings.smtp_user, settings.smtp_password)


def send_email(
    to: str | list[str] | tuple[str, ...],
    subject: str,
    body: str,
    cc: str | list[str] | tuple[str, ...] | None = None,
    html_body: str | None = None,
) -> bool:
    recipients = _normalize_email_list(to)
    cc_recipients = _normalize_email_list(cc)
    sender = settings.from_email
    smtp_host = settings.smtp_host

    if not recipients:
        logger.warning("Email send skipped because no recipient was provided for subject '%s'.", subject)
        return False
    if not sender or not smtp_host:
        logger.warning(
            "Email send skipped for subject '%s' because SMTP is not configured.",
            subject,
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    if cc_recipients:
        msg["Cc"] = ", ".join(cc_recipients)
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    all_recipients = recipients + cc_recipients

    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(
                smtp_host,
                settings.smtp_port,
                timeout=SMTP_TIMEOUT_SECONDS,
            ) as server:
                _login_if_configured(server)
                server.send_message(msg, from_addr=sender, to_addrs=all_recipients)
        else:
            with smtplib.SMTP(
                smtp_host,
                settings.smtp_port,
                timeout=SMTP_TIMEOUT_SECONDS,
            ) as server:
                server.ehlo()
                if settings.smtp_use_tls and server.has_extn("STARTTLS"):
                    server.starttls()
                    server.ehlo()
                _login_if_configured(server)
                server.send_message(msg, from_addr=sender, to_addrs=all_recipients)
        return True
    except Exception:
        logger.exception(
            "SMTP Error while sending email '%s' to %s",
            subject,
            ", ".join(all_recipients),
        )
        return False


def send_new_signup_email(
    owner_email: str,
    new_user_email: str,
    full_name: str | None,
    approval_link: str,
) -> bool:
    display_name = full_name or new_user_email
    subject = f"New Account Pending Approval: {display_name}"
    text_body = f"""Hello,

A new user has registered on the AVO Carbon RFQ Portal and is awaiting your approval.

Name: {display_name}
Email: {new_user_email}

Please log in to the admin panel to review and approve this account:
{approval_link}
"""
    html_body = _build_base_html(
        "New Account Pending Approval",
        f"""
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
        """,
    )
    return send_email(owner_email, subject, text_body, html_body=html_body)


def send_approval_email(
    user_email: str,
    assigned_role: str,
    login_link: str,
) -> bool:
    approval_message = (
        "Hello, your account for the AVO Carbon RFQ Portal has been approved. "
        f"You have been assigned the role of {assigned_role}. You may now log in."
    )
    text_body = f"""{approval_message}

You can now log in here:
{login_link}
"""
    html_body = _build_base_html(
        "Account Approved",
        f"""
          <p>{approval_message}</p>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{login_link}" style="background-color: #16a34a; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Log In to RFQ Portal
            </a>
          </div>
        """,
    )
    return send_email(
        user_email,
        "Your AVO Carbon RFQ Account Has Been Approved",
        text_body,
        html_body=html_body,
    )


def send_validation_email(
    zone_manager_email: str,
    systematic_rfq_id: str,
    acronym: str,
    rfq_link: str,
    validator_role: str = "Validator",
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = (
        f"Action Required: {validator_role} Review"
        f"{_rfq_id_subject_suffix(systematic_rfq_id)}"
    )
    text_body = f"""Hello,

A new RFQ for the {acronym} product line has been submitted.
{rfq_id_line}It requires your validation as the {validator_role} in order to proceed to the Costing phase.
Please log into the AVO Carbon RFQ Portal to review the details:
{rfq_link}
"""
    html_body = _build_base_html(
        f"{validator_role} Review Required",
        f"""
          <p>Hello,</p>
          <p>A new RFQ for the <strong>{acronym}</strong> product line has been submitted. It requires your validation as the <strong>{validator_role}</strong> in order to proceed to the Costing phase.</p>
          <ul>
            {rfq_id_html}
          </ul>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Review RFQ
            </a>
          </div>
          <p style="font-size: 14px; color: #666666;">
            If the button above does not work, copy and paste this link into your browser:<br>
            <a href="{rfq_link}" style="color: #2563eb; word-break: break-all;">{rfq_link}</a>
          </p>
        """,
    )
    return send_email(
        zone_manager_email,
        subject,
        text_body,
        html_body=html_body,
    )


def send_revision_request_email(
    sales_rep_email: str,
    systematic_rfq_id: str,
    comment: str,
    rfq_link: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = f"Revision Requested{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

The validator has requested updates for this RFQ.

{rfq_id_line}Requested changes:
{comment}

You can review the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "Revision Requested",
        f"""
          <p>Hello,</p>
          <p>The validator has requested updates for this RFQ.</p>
          <ul>
            {rfq_id_html}
          </ul>
          <p><strong>Requested changes:</strong></p>
          <div style="white-space: pre-wrap; background-color: #f9fafb; border: 1px solid #e5e7eb; border-radius: 6px; padding: 12px;">{comment}</div>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Review RFQ
            </a>
          </div>
        """,
    )
    return send_email(sales_rep_email, subject, text_body, html_body=html_body)


def send_costing_entry_email(
    recipient_email: str,
    product_line: str,
    product_code: str,
    systematic_rfq_id: str,
    rfq_link: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = f"New RFQ Awaiting Costing Reception{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

A new RFQ is awaiting Costing reception.

{rfq_id_line}Product line: {product_line} ({product_code})

Open the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "New RFQ Awaiting Costing Reception",
        f"""
          <p>Hello,</p>
          <p>A new RFQ is awaiting Costing reception.</p>
          <ul>
            {rfq_id_html}
            <li><strong>Product line:</strong> {product_line} ({product_code})</li>
          </ul>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Open RFQ
            </a>
          </div>
        """,
    )
    return send_email(recipient_email, subject, text_body, html_body=html_body)


def send_costing_reception_results_email(
    to_email: str,
    cc_email: str | None,
    review_user_email: str,
    systematic_rfq_id: str,
    rfq_link: str,
    is_approved: bool,
    rejection_reason: str | None = None,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    if is_approved:
        subject = f"Costing Reception Approved{_rfq_id_subject_suffix(systematic_rfq_id)}"
        text_body = f"""Hello,

The Costing reception review has been approved for this RFQ.

{rfq_id_line}Reviewed by: {review_user_email}

Open the RFQ here:
{rfq_link}
"""
        html_body = _build_base_html(
            "Costing Reception Approved",
            f"""
              <p>Hello,</p>
              <p>The Costing reception review has been approved for this RFQ.</p>
              <ul>
                {rfq_id_html}
                <li><strong>Reviewed by:</strong> {review_user_email}</li>
              </ul>
              <div style="margin: 30px 0; text-align: center;">
                <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
                  Open RFQ
                </a>
              </div>
            """,
        )
    else:
        normalized_reason = str(rejection_reason or "-").strip() or "-"
        subject = f"Costing Reception Rejected{_rfq_id_subject_suffix(systematic_rfq_id)}"
        text_body = f"""Hello,

The Costing reception review has been rejected for this RFQ.

{rfq_id_line}Reviewed by: {review_user_email}
Rejection reason: {normalized_reason}

Open the RFQ here:
{rfq_link}
"""
        html_body = _build_base_html(
            "Costing Reception Rejected",
            f"""
              <p>Hello,</p>
              <p>The Costing reception review has been rejected for this RFQ.</p>
              <ul>
                {rfq_id_html}
                <li><strong>Reviewed by:</strong> {review_user_email}</li>
                <li><strong>Rejection reason:</strong> {normalized_reason}</li>
              </ul>
              <div style="margin: 30px 0; text-align: center;">
                <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
                  Open RFQ
                </a>
              </div>
            """,
        )
    return send_email(to_email, subject, text_body, cc=cc_email, html_body=html_body)


def send_costing_handoff_email(
    recipient_email: str,
    product_line: str,
    product_code: str,
    systematic_rfq_id: str,
    rfq_link: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = f"Begin Feasibility And BOM{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

Please begin the Feasibility and BOM work for this RFQ.

{rfq_id_line}Product line: {product_line} ({product_code})

Please download the templates and open the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "Begin Feasibility And BOM",
        f"""
          <p>Hello,</p>
          <p>Please begin the <strong>Feasibility and BOM</strong> work for this RFQ.</p>
          <ul>
            {rfq_id_html}
            <li><strong>Product line:</strong> {product_line} ({product_code})</li>
          </ul>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Open RFQ And Download Templates
            </a>
          </div>
        """,
    )
    return send_email(recipient_email, subject, text_body, html_body=html_body)


def send_bom_ready_email(
    costing_agent_email: str,
    systematic_rfq_id: str,
    rfq_link: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = f"BOM Data Ready For Pricing{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

The BOM data is ready for this RFQ. Please complete the final pricing.

{rfq_id_line}Open the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "BOM Data Ready For Pricing",
        f"""
          <p>Hello,</p>
          <p>The BOM data is ready for this RFQ. Please complete the final pricing.</p>
          <ul>
            {rfq_id_html}
          </ul>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Open RFQ
            </a>
          </div>
        """,
    )
    return send_email(costing_agent_email, subject, text_body, html_body=html_body)


def send_pricing_ready_email(
    plm_email: str,
    systematic_rfq_id: str,
    rfq_link: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = f"Pricing Ready For Validation{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

Pricing is complete for this RFQ. Please validate it.

{rfq_id_line}Open the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "Pricing Ready For Validation",
        f"""
          <p>Hello,</p>
          <p>Pricing is complete for this RFQ. Please validate it.</p>
          <ul>
            {rfq_id_html}
          </ul>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Open RFQ
            </a>
          </div>
        """,
    )
    return send_email(plm_email, subject, text_body, html_body=html_body)


def send_costing_approved_email(
    kam_email: str,
    systematic_rfq_id: str,
    rfq_link: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = f"Costing Approved{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

Costing is approved for this RFQ. You can now start offer preparation.

{rfq_id_line}Open the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "Costing Approved",
        f"""
          <p>Hello,</p>
          <p>Costing is approved for this RFQ. You can now start offer preparation.</p>
          <ul>
            {rfq_id_html}
          </ul>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Open RFQ
            </a>
          </div>
        """,
    )
    return send_email(kam_email, subject, text_body, html_body=html_body)


def send_costing_rejected_email(
    costing_agent_email: str,
    kam_email: str,
    systematic_rfq_id: str,
    rfq_link: str,
    rejection_reason: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    normalized_reason = str(rejection_reason or "-").strip() or "-"
    subject = f"Costing Rejected{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

Costing was rejected for this RFQ.

{rfq_id_line}Rejection reason: {normalized_reason}

Open the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "Costing Rejected",
        f"""
          <p>Hello,</p>
          <p>Costing was rejected for this RFQ.</p>
          <ul>
            {rfq_id_html}
            <li><strong>Rejection reason:</strong> {normalized_reason}</li>
          </ul>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Open RFQ
            </a>
          </div>
        """,
    )

    to_recipients = _normalize_email_list(costing_agent_email)
    cc_recipients = _normalize_email_list(kam_email)

    if not to_recipients and cc_recipients:
        to_recipients = cc_recipients
        cc_recipients = []
    elif to_recipients and cc_recipients:
        primary_lookup = {recipient.casefold() for recipient in to_recipients}
        cc_recipients = [
            recipient for recipient in cc_recipients if recipient.casefold() not in primary_lookup
        ]

    return send_email(
        to_recipients,
        subject,
        text_body,
        cc=cc_recipients or None,
        html_body=html_body,
    )


def send_costing_message_email(
    recipient_email: str,
    systematic_rfq_id: str,
    sender_email: str,
    message: str,
    rfq_link: str,
) -> bool:
    rfq_id_line = _rfq_id_text_block(systematic_rfq_id)
    rfq_id_html = _rfq_id_html_item(systematic_rfq_id)
    subject = f"New Costing Discussion Message{_rfq_id_subject_suffix(systematic_rfq_id)}"
    text_body = f"""Hello,

{sender_email} has sent you a message in the Costing discussion for this RFQ.

{rfq_id_line}Message:
{message}

Open the RFQ here:
{rfq_link}
"""
    html_body = _build_base_html(
        "New Costing Discussion Message",
        f"""
          <p>Hello,</p>
          <p><strong>{sender_email}</strong> has sent you a message in the Costing discussion for this RFQ.</p>
          <ul>
            {rfq_id_html}
          </ul>
          <p><strong>Message:</strong></p>
          <div style="white-space: pre-wrap; background-color: #f9fafb; border: 1px solid #e5e7eb; border-radius: 6px; padding: 12px;">{message}</div>
          <div style="margin: 30px 0; text-align: center;">
            <a href="{rfq_link}" style="background-color: #2563eb; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">
              Open RFQ
            </a>
          </div>
        """,
    )
    return send_email(recipient_email, subject, text_body, html_body=html_body)
