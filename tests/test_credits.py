from datetime import UTC, datetime

from nflnews.bot import credits_text

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
SPEND = {"today": 0.05, "7 days": 0.4, "this month": 1.2}


def test_credits_message_shows_both_balances():
    x = {
        "total_balance": 39.94,
        "free_balance": 19.94,
        "free_grants": [{"amount": 19.94, "expires_at": "2027-01-26T23:16:13.000Z"}],
        "prepaid_balance": 20.0,
    }
    usage = {"project_usage": "1234", "cap_reset_day": 28}
    balance = {"amount": 10.0, "set_at": "2026-10-01T00:00:00+00:00"}
    text = credits_text(NOW, x, usage, SPEND, balance, 8.8)
    assert "**$39.94 left**" in text and "running low" not in text
    assert "$19.94 free credit, expires <t:1801005373:D>" in text and "$20.00 prepaid" in text
    assert "1,234 posts read this cycle" in text
    assert "**~$8.80 left**" in text
    assert "today $0.05 · 7 days $0.40 · this month $1.20" in text


def test_credits_message_warns_when_low_and_explains_missing_data():
    text = credits_text(
        NOW, {"total_balance": 3.0, "prepaid_balance": 3.0}, None, SPEND, None, None
    )
    assert "**$3.00 left** ⚠️ running low" in text
    assert "/newsbot openai-balance" in text
    assert "Couldn't load" in credits_text(NOW, None, None, SPEND, None, None)
