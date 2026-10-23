from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_render_blueprint_boots_without_optional_paid_provider_credentials():
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    env_example = (ROOT / "backend" / ".env.example").read_text(encoding="utf-8")
    docs = (ROOT / "DEPLOYMENT_CHECKLIST.md").read_text(encoding="utf-8")
    for key in (
        "TWELVEDATA_API_KEY", "FRED_API_KEY", "TRADING_ECONOMICS_API_KEY",
        "OPENAI_API_KEY", "DATABENTO_API_KEY", "BROKER_TICK_SIZE",
        "BROKER_TICK_VALUE_ACCOUNT_CCY", "BROKER_VOLUME_MIN", "BROKER_VOLUME_MAX",
        "BROKER_VOLUME_STEP", "TRADINGVIEW_WEBHOOK_TOKEN",
    ):
        assert key in env_example or key in text, f"missing environment contract for {key}"
    assert "Add these as environment variables in the Render dashboard" in docs
    assert "value: twelvedata" in text
    assert 'key: POLL_SECONDS\n        value: "3"' in text
    assert 'key: MAX_QUOTE_AGE_SECONDS\n        value: "90"' in text
    assert "gpt-4.1-mini" in text
    assert "gpt-6.1-sol" not in text
    assert "sync: false" not in text, "optional secret prompts must not block first deployment"


def test_deployment_defaults_keep_real_flow_and_book_confirmation_required():
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert 'key: ORDER_FLOW_REQUIRED\n        value: "true"' in text
    assert 'key: ORDER_BOOK_REQUIRED\n        value: "true"' in text
    assert 'key: ECONOMIC_CALENDAR_REQUIRED\n        value: "true"' in text
