from datetime import datetime, timezone
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import apply_confirmation_gates


def _flow():
    now = datetime.now(timezone.utc).isoformat()
    return {
        "connected": True, "provider": "test fixture", "timestamp": now, "delta_percent": 12.0,
        "depth_connected": True, "depth_timestamp": now, "depth_imbalance_percent": 12.0,
        "depth_provider": "test fixture",
    }


def _candidate():
    return {"status": "CANDIDATE", "side": "BUY", "reason": "fixture", "context": {}}


def test_candidate_fails_closed_when_required_calendar_is_disconnected(monkeypatch):
    monkeypatch.setenv("ORDER_FLOW_REQUIRED", "true")
    monkeypatch.setenv("ORDER_BOOK_REQUIRED", "true")
    monkeypatch.setenv("ECONOMIC_CALENDAR_REQUIRED", "true")
    result = apply_confirmation_gates(_candidate(), _flow(), [], {"connected": False, "events": []})
    assert result["status"] == "NO TRADE"
    assert "calendar is required" in result["reason"].lower()


def test_candidate_fails_closed_when_calendar_is_stale(monkeypatch):
    monkeypatch.setenv("ORDER_FLOW_REQUIRED", "true")
    monkeypatch.setenv("ORDER_BOOK_REQUIRED", "true")
    monkeypatch.setenv("ECONOMIC_CALENDAR_REQUIRED", "true")
    old = datetime.fromtimestamp(1, timezone.utc).isoformat()
    result = apply_confirmation_gates(_candidate(), _flow(), [], {"connected": True, "updated_at": old, "events": []})
    assert result["status"] == "NO TRADE"
    assert "stale" in result["reason"].lower()


def test_candidate_fails_closed_for_high_impact_event_without_timestamp(monkeypatch):
    monkeypatch.setenv("ORDER_FLOW_REQUIRED", "true")
    monkeypatch.setenv("ORDER_BOOK_REQUIRED", "true")
    monkeypatch.setenv("ECONOMIC_CALENDAR_REQUIRED", "true")
    calendar = {"connected": True, "updated_at": datetime.now(timezone.utc).isoformat(), "events": []}
    result = apply_confirmation_gates(_candidate(), _flow(), [{"event": "CPI", "impact": "High", "time": None}], calendar)
    assert result["status"] == "NO TRADE"
    assert "no usable timestamp" in result["reason"].lower()


def test_candidate_passes_event_gate_only_with_recent_calendar_and_no_nearby_high_impact_events(monkeypatch):
    monkeypatch.setenv("ORDER_FLOW_REQUIRED", "true")
    monkeypatch.setenv("ORDER_BOOK_REQUIRED", "true")
    monkeypatch.setenv("ECONOMIC_CALENDAR_REQUIRED", "true")
    calendar = {"connected": True, "updated_at": datetime.now(timezone.utc).isoformat(), "events": []}
    result = apply_confirmation_gates(_candidate(), _flow(), [], calendar)
    assert result["status"] == "CANDIDATE"
