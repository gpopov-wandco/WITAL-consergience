"""Per-tenant configuration schema (Appendix C), validated with pydantic.

Implements: F21, C2, C5, C10

The profile is the single place for every hotel-specific value that is not a
knowledge-base fact: contacts, staffed hours, handoff rules, availability chain,
journey timing, voice, widget, limits and pricing references. It is stored as JSON
in ``tenant.profile`` and exported unchanged in client packs; secrets are never part
of it.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from concierge.clock import in_range, parse_hhmm, parse_range, to_local

LANGS: tuple[str, ...] = ("de", "en", "ru", "uk", "tr", "it")
Lang = Literal["de", "en", "ru", "uk", "tr", "it"]
DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_KEY = re.compile(r"^pk_[a-z0-9]{4,40}$")
BOOKING_URL_FIELDS = frozenset({"arrival", "departure", "adults", "children", "lang", "room"})


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Contacts(Strict):
    reception_phone: str = ""
    email: str = ""
    privacy_url: str = ""
    imprint_url: str = ""
    website: str = ""
    phone_region: str = Field(default="DE", pattern=r"^[A-Z]{2}$")
    """ISO 3166 region used to read local phone numbers (0151... becomes +49151...)."""


class SlaConfig(Strict):
    p1_min: int = Field(default=10, ge=1, le=1440)
    p2_min: int = Field(default=120, ge=1, le=10080)
    p3: Literal["next_business_morning"] = "next_business_morning"
    p0_repeat_min: int = Field(default=2, ge=1, le=60)


class HandoffContact(Strict):
    name: str
    role: str = "front desk"
    whatsapp: str | None = None
    email: str | None = None
    on_call: bool = False


class HandoffConfig(Strict):
    sla: SlaConfig = SlaConfig()
    contacts: list[HandoffContact] = Field(default_factory=list)
    response_window_text: dict[str, str] = Field(default_factory=dict)


class WhatsAppConfig(Strict):
    waba_id: str = ""
    phone_number_id: str = ""
    display_phone_number: str = ""


class Beds24Config(Strict):
    property_id: str = ""


class ApaleoConfig(Strict):
    property_id: str = ""


class MewsConfig(Strict):
    hotel_id: str = ""
    configuration_id: str = ""
    demo: bool = False


class AvailabilityConfig(Strict):
    chain: list[
        Literal["beds24", "apaleo", "mews", "smoobu", "ibelsa", "ical", "manual", "none"]
    ] = Field(default_factory=lambda: ["manual", "none"])
    stale_after_hours: int = Field(default=36, ge=1, le=720)
    booking_url: str = ""
    allow_pms_write: bool = False
    high_demand_dates: list[date] = Field(default_factory=list)
    ical_units: dict[str, int] = Field(default_factory=dict)
    """Room type code to number of physical units; feed URLs are secrets ical_url_<code>_<n>."""
    ical_max_age_min: int = Field(default=15, ge=1, le=1440)
    rate_limits: dict[str, str] = Field(default_factory=dict)
    """Own request budget per source (limits notation), below the vendor's limit."""
    beds24: Beds24Config = Beds24Config()
    apaleo: ApaleoConfig = ApaleoConfig()
    mews: MewsConfig = MewsConfig()
    tax_inclusive: bool | None = None
    currency: str = "EUR"

    @field_validator("booking_url")
    @classmethod
    def _booking_url(cls, value: str) -> str:
        if not value:
            return value
        if not value.startswith(("https://", "http://")):
            raise ValueError("booking_url must start with https:// or http://")
        fields = set(re.findall(r"\{([a-z_]+)\}", value))
        unknown = fields - BOOKING_URL_FIELDS
        if unknown:
            raise ValueError(f"booking_url has unknown placeholders: {sorted(unknown)}")
        return value

    @model_validator(mode="after")
    def _chain_ends(self) -> AvailabilityConfig:
        if not self.chain:
            raise ValueError("availability.chain must not be empty")
        if len(set(self.chain)) != len(self.chain):
            raise ValueError("availability.chain has duplicates")
        return self


class TimedStep(Strict):
    enabled: bool = True
    at: str = "10:00"

    @field_validator("at")
    @classmethod
    def _at(cls, value: str) -> str:
        parse_hhmm(value)
        return value


class FollowupStep(Strict):
    enabled: bool = True
    within_min: int = Field(default=15, ge=1, le=1440)


class StayCheckStep(TimedStep):
    at: str = "18:00"
    min_nights: int = Field(default=2, ge=1, le=30)


class CheckoutStep(TimedStep):
    at: str = "08:00"

    @field_validator("at")
    @classmethod
    def _not_before_seven(cls, value: str) -> str:
        parsed = parse_hhmm(value)
        if parsed < time(7, 0):
            raise ValueError("checkout reminder must not be sent before 07:00")
        return value


class ReviewStep(TimedStep):
    enabled: bool = False
    at: str = "11:00"
    days_after: int = Field(default=1, ge=1, le=14)
    review_url: str = ""


class JourneyCaps(Strict):
    before: int = Field(default=3, ge=0, le=10)
    during: int = Field(default=1, ge=0, le=10)
    after: int = Field(default=2, ge=0, le=10)


class JourneysConfig(Strict):
    quiet_hours: str = "21:00-08:00"
    booking_followup: FollowupStep = FollowupStep()
    t_minus_7: bool = True
    t_minus_7_at: str = "10:00"
    t_minus_2: TimedStep = TimedStep(at="10:00")
    t_0: TimedStep = TimedStep(at="10:00")
    stay_check: StayCheckStep = StayCheckStep()
    checkout: CheckoutStep = CheckoutStep()
    review_request: ReviewStep = ReviewStep()
    caps: JourneyCaps = JourneyCaps()
    email_fallback: bool = True

    @field_validator("quiet_hours")
    @classmethod
    def _quiet(cls, value: str) -> str:
        parse_range(value)
        return value

    @field_validator("t_minus_7_at")
    @classmethod
    def _t7(cls, value: str) -> str:
        parse_hhmm(value)
        return value

    def quiet_window(self) -> tuple[time, time]:
        return parse_range(self.quiet_hours)

    def is_quiet(self, local_time: time) -> bool:
        return in_range(local_time, self.quiet_window())


class RegistrationConfig(Strict):
    enabled: bool = False
    retention_days: int = Field(default=380, ge=365, le=455)
    psp: Literal["stripe", "fake"] = "stripe"
    preauth_cents: int = Field(default=100, ge=0, le=10000)


class VoiceConfig(Strict):
    provider: Literal["retell", "twilio_cr", "openai_compat", "none"] = "none"
    number: str = ""
    eu_only: bool = False
    us_transfer_acknowledged: bool = False
    included_minutes: int = Field(default=300, ge=0, le=100000)
    overage_eur_per_min: float = Field(default=0.19, ge=0, le=10)
    transcript_retention_days: int = Field(default=30, ge=1, le=365)
    filler_ms: int = Field(default=900, ge=100, le=5000)
    rounding: Literal["second", "minute"] = "second"
    voice_id: str = ""
    agent_id: str = ""

    @model_validator(mode="after")
    def _eu_gate(self) -> VoiceConfig:
        if self.provider == "retell" and self.eu_only and not self.us_transfer_acknowledged:
            raise ValueError(
                "voice.eu_only is true: retell processes voice data in the US; set "
                "voice.us_transfer_acknowledged to true or choose another provider"
            )
        return self


class WidgetConfig(Strict):
    public_key: str = "pk_demo"
    origins: list[str] = Field(default_factory=list)
    primary_color: str = "#C22113"
    position: Literal["right", "left"] = "right"
    logo_text: str = ""
    greeting: dict[str, str] = Field(default_factory=dict)
    language_menu: bool = True

    @field_validator("public_key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _KEY.match(value):
            raise ValueError("widget.public_key must look like pk_xxxx (lowercase letters, digits)")
        return value

    @field_validator("primary_color")
    @classmethod
    def _color(cls, value: str) -> str:
        if not _HEX.match(value):
            raise ValueError("widget.primary_color must be a #rrggbb colour")
        return value

    @field_validator("origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$", origin):
                raise ValueError(f"widget.origins entry is not an origin: {origin!r}")
        return value


class LimitsConfig(Strict):
    daily_llm_budget_usd: float = Field(default=5.0, ge=0, le=10000)
    messages_per_guest_per_hour: int = Field(default=20, ge=1, le=1000)
    widget_messages_per_session_per_hour: int = Field(default=30, ge=1, le=1000)
    widget_requests_per_ip_per_minute: int = Field(default=30, ge=1, le=10000)
    max_model_calls_per_message: int = Field(default=5, ge=1, le=10)
    max_tool_runs_per_message: int = Field(default=6, ge=1, le=12)
    reply_max_chars: int = Field(default=350, ge=100, le=700)
    reply_hard_cap_chars: int = Field(default=700, ge=200, le=1000)


class PricingConfig(Strict):
    price_table: str = "default_de"
    model_prices: str = "default"
    usd_eur: str = "default"


class LegalConfig(Strict):
    status: Literal["draft_unreviewed", "approved"] = "draft_unreviewed"


class RetentionConfig(Strict):
    chat_days: int = Field(default=30, ge=1, le=3650)
    booking_days_after_departure: int = Field(default=30, ge=1, le=3650)
    consent_years: int = Field(default=3, ge=1, le=10)
    metering_months: int = Field(default=24, ge=1, le=120)
    voice_transcript_days: int = Field(default=30, ge=1, le=365)
    webhook_days: int = Field(default=14, ge=1, le=365)


class BrandingConfig(Strict):
    logo_text: str = ""
    accent_color: str = "#C22113"
    tone: dict[str, str] = Field(default_factory=dict)


class TenantProfile(Strict):
    """The tenant profile (Appendix C)."""

    slug: str
    name: str
    timezone: str = "Europe/Berlin"
    status: Literal["demo", "pilot", "active", "paused"] = "demo"
    ai_mode: Literal["full", "faq_only"] = "full"
    languages: list[Lang] = Field(default_factory=lambda: ["de", "en"])
    default_language: Lang = "de"
    machine_translation_allowed: bool = False
    contacts: Contacts = Contacts()
    staffed_hours: dict[str, str] = Field(default_factory=lambda: {"mon-sun": "07:00-22:00"})
    handoff: HandoffConfig = HandoffConfig()
    whatsapp: WhatsAppConfig = WhatsAppConfig()
    availability: AvailabilityConfig = AvailabilityConfig()
    journeys: JourneysConfig = JourneysConfig()
    registration: RegistrationConfig = RegistrationConfig()
    voice: VoiceConfig = VoiceConfig()
    widget: WidgetConfig = WidgetConfig()
    limits: LimitsConfig = LimitsConfig()
    pricing: PricingConfig = PricingConfig()
    legal: LegalConfig = LegalConfig()
    retention: RetentionConfig = RetentionConfig()
    branding: BrandingConfig = BrandingConfig()
    fixture: bool = False
    needs_input: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _SLUG.match(value):
            raise ValueError("slug must be lowercase letters, digits and hyphens (2 to 63 chars)")
        return value

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        from concierge.clock import zone

        zone(value)
        return value

    @field_validator("languages")
    @classmethod
    def _languages(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("languages has duplicates")
        for mandatory in ("de", "en"):
            if mandatory not in value:
                raise ValueError(f"languages must include {mandatory!r}")
        return value

    @field_validator("staffed_hours")
    @classmethod
    def _staffed(cls, value: dict[str, str]) -> dict[str, str]:
        for key, window in value.items():
            _expand_days(key)
            if window.lower() != "closed":
                parse_range(window)
        return value

    @model_validator(mode="after")
    def _default_language_supported(self) -> TenantProfile:
        if self.default_language not in self.languages:
            raise ValueError("default_language must be one of languages")
        return self

    # ------------------------------------------------------------- helpers
    def is_staffed(self, instant: datetime) -> bool:
        local = to_local(instant, self.timezone)
        day = DAYS[local.weekday()]
        for key, window in self.staffed_hours.items():
            if day in _expand_days(key):
                if window.lower() == "closed":
                    return False
                return in_range(local.time(), parse_range(window))
        return False

    def next_staffed_start(self, instant: datetime) -> datetime:
        """The next instant at which staffed hours begin (at most 8 days ahead)."""
        from datetime import timedelta

        from concierge.clock import local_to_utc

        local = to_local(instant, self.timezone)
        for offset in range(0, 9):
            day = local.date() + timedelta(days=offset)
            name = DAYS[day.weekday()]
            for key, window in self.staffed_hours.items():
                if name in _expand_days(key) and window.lower() != "closed":
                    start, _ = parse_range(window)
                    candidate = local_to_utc(day, start, self.timezone)
                    if candidate > instant:
                        return candidate
        return instant + timedelta(days=1)

    def supports(self, lang: str) -> bool:
        return lang in self.languages

    def public_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _expand_days(key: str) -> list[str]:
    key = key.lower()
    if "-" in key:
        start, end = key.split("-", 1)
        if start not in DAYS or end not in DAYS:
            raise ValueError(f"staffed_hours key {key!r} must use mon..sun")
        i, j = DAYS.index(start), DAYS.index(end)
        return list(DAYS[i : j + 1]) if i <= j else list(DAYS[i:]) + list(DAYS[: j + 1])
    if key not in DAYS:
        raise ValueError(f"staffed_hours key {key!r} must use mon..sun")
    return [key]


def profile_from_dict(data: dict[str, object]) -> TenantProfile:
    return TenantProfile.model_validate(data)


def validation_errors(data: dict[str, object]) -> list[str]:
    """Human-readable errors with field paths, for ``concierge pack validate``."""
    from pydantic import ValidationError

    try:
        TenantProfile.model_validate(data)
    except ValidationError as exc:
        return [
            ".".join(str(part) for part in err["loc"]) + ": " + err["msg"] for err in exc.errors()
        ]
    return []
