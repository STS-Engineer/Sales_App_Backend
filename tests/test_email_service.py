import smtplib

from app.config import settings
from app.utils import emails


def _configure_smtp(
    monkeypatch,
    *,
    host: str = "smtp.example.com",
    port: int = 587,
    use_tls: bool = False,
    user: str = "mailer",
    password: str = "secret",
    from_email: str = "no-reply@example.com",
):
    monkeypatch.setattr(settings, "SMTP_HOST", host, raising=False)
    monkeypatch.setattr(settings, "SMTP_PORT", port, raising=False)
    monkeypatch.setattr(settings, "SMTP_USE_TLS", use_tls, raising=False)
    monkeypatch.setattr(settings, "SMTP_USER", user, raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", password, raising=False)
    monkeypatch.setattr(settings, "FROM_EMAIL", from_email, raising=False)


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []
    supports_starttls = True

    def __init__(self, host: str, port: int, timeout: int | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.actions: list[object] = []
        self.message = None
        self.from_addr = None
        self.to_addrs = None
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        self.actions.append("ehlo")

    def has_extn(self, name: str) -> bool:
        return name.upper() == "STARTTLS" and self.supports_starttls

    def starttls(self):
        self.actions.append("starttls")

    def login(self, user: str, password: str):
        self.actions.append(("login", user, password))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.actions.append(("send_message", from_addr, tuple(to_addrs or [])))
        self.message = msg
        self.from_addr = from_addr
        self.to_addrs = list(to_addrs or [])


class _FakeSMTPSSL(_FakeSMTP):
    pass


class _FailingSMTP(_FakeSMTP):
    def send_message(self, msg, from_addr=None, to_addrs=None):
        raise smtplib.SMTPException("boom")


def test_send_email_uses_starttls_login_cc_and_html(monkeypatch):
    _configure_smtp(monkeypatch, use_tls=True)
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    result = emails.send_email(
        "user@example.com",
        "Subject line",
        "Plain body",
        cc="cc@example.com",
        html_body="<p>HTML body</p>",
    )

    assert result is True
    smtp_client = _FakeSMTP.instances[-1]
    assert smtp_client.host == "smtp.example.com"
    assert smtp_client.port == 587
    assert smtp_client.actions[:4] == [
        "ehlo",
        "starttls",
        "ehlo",
        ("login", "mailer", "secret"),
    ]
    assert smtp_client.to_addrs == ["user@example.com", "cc@example.com"]
    assert smtp_client.message["From"] == "no-reply@example.com"
    assert smtp_client.message["To"] == "user@example.com"
    assert smtp_client.message["Cc"] == "cc@example.com"
    assert "Plain body" in smtp_client.message.get_body(preferencelist=("plain",)).get_content()
    assert "HTML body" in smtp_client.message.get_body(preferencelist=("html",)).get_content()


def test_send_email_skips_starttls_when_disabled(monkeypatch):
    _configure_smtp(monkeypatch, use_tls=False)
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    result = emails.send_email(
        "user@example.com",
        "No TLS subject",
        "Body",
    )

    assert result is True
    smtp_client = _FakeSMTP.instances[-1]
    assert smtp_client.actions == [
        "ehlo",
        ("login", "mailer", "secret"),
        ("send_message", "no-reply@example.com", ("user@example.com",)),
    ]


def test_send_email_uses_ssl_and_skips_login_when_credentials_missing(monkeypatch):
    _configure_smtp(monkeypatch, port=465, user="", password="")
    _FakeSMTPSSL.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)

    result = emails.send_email(
        ["first@example.com", "second@example.com"],
        "SSL subject",
        "Body",
    )

    assert result is True
    smtp_client = _FakeSMTPSSL.instances[-1]
    assert smtp_client.port == 465
    assert ("login", "mailer", "secret") not in smtp_client.actions
    assert all(action != ("login", "", "") for action in smtp_client.actions)
    assert smtp_client.to_addrs == ["first@example.com", "second@example.com"]


def test_send_email_returns_false_when_smtp_not_configured(monkeypatch):
    _configure_smtp(monkeypatch, host="", from_email="")

    result = emails.send_email(
        "user@example.com",
        "Missing config",
        "Body",
    )

    assert result is False


def test_send_email_logs_and_returns_false_on_failure(monkeypatch, caplog):
    _configure_smtp(monkeypatch)
    _FailingSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FailingSMTP)

    with caplog.at_level("ERROR"):
        result = emails.send_email(
            "user@example.com",
            "Failure subject",
            "Body",
        )

    assert result is False
    assert "SMTP Error while sending email" in caplog.text


def test_send_costing_reception_results_email_includes_rejection_reason(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_email(to, subject, body, cc=None, html_body=None):
        captured["to"] = to
        captured["subject"] = subject
        captured["body"] = body
        captured["cc"] = cc
        captured["html_body"] = html_body
        return True

    monkeypatch.setattr(emails, "send_email", _fake_send_email)

    result = emails.send_costing_reception_results_email(
        "creator@example.com",
        "validator@example.com",
        "reviewer@example.com",
        "26001-BRU-00",
        "http://localhost:5173/rfqs/new?id=rfq-123",
        is_approved=False,
        rejection_reason="Supplier quote is missing.",
    )

    assert result is True
    assert captured["to"] == "creator@example.com"
    assert captured["cc"] == "validator@example.com"
    assert captured["subject"] == "Costing Reception Rejected: 26001-BRU-00"
    assert "rejected" in str(captured["body"]).lower()
    assert "RFQ ID: 26001-BRU-00" in str(captured["body"])
    assert "rfq-123" not in str(captured["subject"])
    assert "RFQ ID: rfq-123" not in str(captured["body"])
    assert "Supplier quote is missing." in str(captured["body"])
    assert "RFQ ID:</strong> 26001-BRU-00" in str(captured["html_body"])
    assert "RFQ ID:</strong> rfq-123" not in str(captured["html_body"])
    assert "Supplier quote is missing." in str(captured["html_body"])


def test_send_revision_and_costing_message_emails_use_only_systematic_rfq_id(monkeypatch):
    captured_calls: list[dict[str, object]] = []

    def _fake_send_email(to, subject, body, cc=None, html_body=None):
        captured_calls.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "cc": cc,
                "html_body": html_body,
            }
        )
        return True

    monkeypatch.setattr(emails, "send_email", _fake_send_email)

    revision_result = emails.send_revision_request_email(
        "creator@example.com",
        "26001-BRU-00",
        "Please update the quotation date.",
        "http://localhost:5173/rfqs/new?id=rfq-123",
    )
    message_result = emails.send_costing_message_email(
        "pricing.owner@example.com",
        "26001-BRU-00",
        "sender@example.com",
        "Please confirm the pricing assumptions.",
        "http://localhost:5173/rfqs/new?id=rfq-123",
    )

    assert revision_result is True
    assert message_result is True
    assert len(captured_calls) == 2

    revision_email, message_email = captured_calls
    assert revision_email["subject"] == "Revision Requested: 26001-BRU-00"
    assert "RFQ ID: 26001-BRU-00" in str(revision_email["body"])
    assert "rfq-123" not in str(revision_email["subject"])
    assert "RFQ ID: rfq-123" not in str(revision_email["body"])
    assert "RFQ ID:</strong> 26001-BRU-00" in str(revision_email["html_body"])
    assert "RFQ ID:</strong> rfq-123" not in str(revision_email["html_body"])

    assert (
        message_email["subject"]
        == "New Costing Discussion Message: 26001-BRU-00"
    )
    assert "RFQ ID: 26001-BRU-00" in str(message_email["body"])
    assert "rfq-123" not in str(message_email["subject"])
    assert "RFQ ID: rfq-123" not in str(message_email["body"])
    assert "RFQ ID:</strong> 26001-BRU-00" in str(message_email["html_body"])
    assert "RFQ ID:</strong> rfq-123" not in str(message_email["html_body"])
