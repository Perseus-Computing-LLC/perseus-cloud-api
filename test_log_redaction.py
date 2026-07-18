from accounts import _redact_email


def test_redact_email_preserves_domain_without_local_part():
    assert _redact_email("member@example.com") == "m***@example.com"


def test_redact_email_rejects_malformed_value():
    assert _redact_email("not-an-email") == "invalid-email"
