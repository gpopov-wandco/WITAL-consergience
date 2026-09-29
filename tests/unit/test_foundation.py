"""Settings, clock, ids and logging."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from concierge import clock as clockmod
from concierge.clock import FakeClock, format_date, in_range, local_to_utc, parse_range
from concierge.ids import new_id, ulid
from concierge.log import configure_logging, get_logger, redact
from concierge.settings import Settings, SettingsError

pytestmark = pytest.mark.feature("F28")


class TestSettings:
    def test_defaults_are_offline(self, clean_env: None) -> None:
        s = Settings(_env_file=None)
        assert s.gc_env == "development"
        assert s.gc_llm_provider == "none"
        assert s.database_url().startswith("sqlite+aiosqlite:///")
        assert s.public_base_url() == "http://127.0.0.1:8000"

    def test_master_keys_errors_name_the_variable(self, clean_env: None) -> None:
        s = Settings(_env_file=None)
        with pytest.raises(SettingsError, match="GC_MASTER_KEYS"):
            s.master_keys()
        bad = Settings(_env_file=None, gc_master_keys=json.dumps({"k1": "c2hvcnQ="}))
        with pytest.raises(SettingsError, match="32 bytes"):
            bad.master_keys()
        not_json = Settings(_env_file=None, gc_master_keys="{nope")
        with pytest.raises(SettingsError, match="JSON"):
            not_json.master_keys()

    def test_active_key_resolution(self, settings: Settings) -> None:
        assert settings.active_key_id() == "k1"
        several = Settings(
            _env_file=None,
            gc_master_keys=json.dumps(
                {
                    "a": settings.gc_backup_key.get_secret_value(),
                    "b": settings.gc_backup_key.get_secret_value(),
                }
            ),
        )
        with pytest.raises(SettingsError, match="GC_ACTIVE_KEY_ID"):
            several.active_key_id()

    def test_production_requires_public_url(self, clean_env: None) -> None:
        s = Settings(_env_file=None, gc_env="production")
        with pytest.raises(SettingsError, match="GC_PUBLIC_BASE_URL"):
            s.public_base_url()

    def test_graph_version_validated(self, clean_env: None) -> None:
        with pytest.raises(ValueError, match="GC_WA_GRAPH_VERSION"):
            Settings(_env_file=None, gc_wa_graph_version="26")

    def test_secret_never_in_repr(self, settings: Settings) -> None:
        assert "s" * 48 not in repr(settings)
        assert settings.gc_backup_key.get_secret_value() not in str(settings)


class TestClock:
    def test_fake_clock_advances(self) -> None:
        c = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        c.advance(timedelta(hours=1))
        assert c.now() == datetime(2026, 1, 1, 1, tzinfo=UTC)

    def test_naive_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            FakeClock(datetime(2026, 1, 1))

    @pytest.mark.parametrize(
        ("day", "at", "expected_utc_hour"),
        [
            (date(2026, 10, 24), time(10, 0), 8),  # CEST, UTC+2
            (date(2026, 10, 25), time(10, 0), 9),  # DST ends 03:00 -> 02:00, UTC+1 afterwards
            (date(2027, 3, 27), time(10, 0), 9),  # CET
            (date(2027, 3, 28), time(10, 0), 8),  # DST starts, UTC+2
        ],
    )
    def test_dst_boundaries(self, day: date, at: time, expected_utc_hour: int) -> None:
        assert local_to_utc(day, at, "Europe/Berlin").hour == expected_utc_hour

    def test_quiet_hours_cross_midnight(self) -> None:
        window = parse_range("21:00-08:00")
        assert in_range(time(23, 0), window)
        assert in_range(time(7, 59), window)
        assert not in_range(time(8, 0), window)
        assert not in_range(time(12, 0), window)

    def test_unknown_zone(self) -> None:
        with pytest.raises(ValueError, match="Europe/Berlin"):
            clockmod.zone("Mars/Olympus")

    def test_date_formats(self) -> None:
        d = date(2026, 12, 3)
        assert format_date(d, "de") == "03.12.2026"
        assert format_date(d, "it") == "03/12/2026"
        assert format_date(d, "en") == "03 Dec 2026"


class TestIds:
    def test_ulid_sorts_by_time(self) -> None:
        early = ulid(1_000)
        late = ulid(2_000)
        assert early < late
        assert len(early) == 26

    def test_prefix(self) -> None:
        assert new_id("conv").startswith("conv_")
        with pytest.raises(ValueError, match="alphanumeric"):
            new_id("bad prefix")


class TestLog:
    def test_redacts_pii_keys_and_suffixes(self) -> None:
        out = redact(
            {
                "phone": "+49",
                "nested": {"wa_access_token": "x", "ok": 1},
                "items": [{"email": "a@b"}],
            }
        )
        assert out == {
            "phone": "[redacted]",
            "nested": {"wa_access_token": "[redacted]", "ok": 1},
            "items": [{"email": "[redacted]"}],
        }

    def test_json_lines_without_pii(self) -> None:
        stream = io.StringIO()
        configure_logging("INFO", stream=stream)
        get_logger("t").info("inbound", tenant="ten_1", text="Hallo Welt", phone="+4915112345")
        line = json.loads(stream.getvalue().strip().splitlines()[-1])
        assert line["event"] == "inbound"
        assert line["tenant"] == "ten_1"
        assert line["text"] == "[redacted]"
        assert "+4915112345" not in stream.getvalue()
