"""PMS adapters (F08) against recorded synthetic fixtures via httpx2.MockTransport.

Cases per adapter: availability, restriction violation, 401 with token refresh, 429 with
Retry-After, timeout, malformed body, credit budget (Beds24), write gates (F10) and the
Mews UTC time-unit normalisation (F07 rule 8).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest

from concierge.availability.apaleo import ApaleoAdapter
from concierge.availability.beds24 import Beds24Adapter
from concierge.availability.mews import MewsAdapter, local_day, utc_midnight
from concierge.availability.models import (
    AuthError,
    InquiryRequest,
    MalformedResponse,
    NotSupported,
    Occupancy,
    RateLimited,
    SourceUnavailable,
    Stay,
)
from concierge.clock import FakeClock

pytestmark = pytest.mark.feature("F08")

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
STAY = Stay(date(2026, 10, 3), date(2026, 10, 5))
OCC = Occupancy(2, 0)
Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    def __init__(self, route: Callable[[Recorder, httpx2.Request], httpx2.Response]) -> None:
        self.route = route
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.route(self, request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)


# ----------------------------------------------------------------- beds24
def _beds24_calendar(min_stay: int = 1, num_avail: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "roomId": 101,
            "calendar": [
                {
                    "from": "2026-10-03",
                    "to": "2026-10-04",
                    "numAvail": num_avail,
                    "price1": 120.0,
                    "minStay": min_stay,
                },
            ],
        },
        {
            "roomId": 102,
            "calendar": [
                {
                    "from": "2026-10-03",
                    "to": "2026-10-04",
                    "numAvail": 0,
                    "price1": 95.5,
                    "minStay": 1,
                }
            ],
        },
    ]


def beds24_routes(state: dict[str, Any]) -> Callable[[Recorder, httpx2.Request], httpx2.Response]:
    def route(rec: Recorder, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/authentication/token"):
            state["tokens"] = state.get("tokens", 0) + 1
            if request.headers.get("refreshToken") != "refresh-1":
                return httpx2.Response(401, json={"success": False})
            return httpx2.Response(200, json={"token": f"tok{state['tokens']}", "expiresIn": 86400})
        if request.headers.get("token") not in {"tok1", "tok2", "tok3"}:
            return httpx2.Response(401, json={"success": False, "error": "unauthorized"})
        headers = {
            "X-FiveMinCreditLimit-Remaining": str(state.get("credits", 90)),
            "X-RequestCost": "1",
        }
        if state.get("fail_next"):
            status, body = state.pop("fail_next")
            if status == "timeout":
                raise httpx2.ReadTimeout("slow")
            return httpx2.Response(
                status, content=body, headers={**headers, **state.pop("fail_headers", {})}
            )
        if path.endswith("/properties"):
            return httpx2.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": "P1",
                            "roomTypes": [
                                {"id": 101, "name": "Doppelzimmer", "qty": 10, "maxPeople": 2},
                                {"id": 102, "name": "Einzelzimmer", "qty": 6, "maxPeople": 1},
                            ],
                        }
                    ],
                },
                headers=headers,
            )
        if path.endswith("/inventory/rooms/calendar"):
            assert (
                request.url.params["includeNumAvail"] == "true"
                and request.url.params["propertyId"] == "P1"
            )
            return httpx2.Response(
                200,
                json={"success": True, "data": state.get("calendar", _beds24_calendar())},
                headers=headers,
            )
        if path.endswith("/bookings") and request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "id": 5001,
                            "firstName": "Anna",
                            "lastName": "Muster",
                            "arrival": "2026-10-03",
                            "departure": "2026-10-05",
                            "roomId": 101,
                            "status": state.get("booking_status", "confirmed"),
                            "numAdult": 2,
                            "numChild": 0,
                            "email": "a@example.invalid",
                            "phone": "+49 151 1234567",
                            "lang": "de",
                        }
                    ],
                },
                headers=headers,
            )
        if path.endswith("/bookings") and request.method == "POST":
            state["posted"] = json.loads(request.content)
            return httpx2.Response(
                200, json=[{"success": True, "new": {"id": 7001}}], headers=headers
            )
        if path.endswith("/authentication/details"):
            return httpx2.Response(
                200,
                json={"success": True, "token": {"scopes": ["read:inventory"]}},
                headers=headers,
            )
        return httpx2.Response(404, json={"success": False})

    return route


def beds24(state: dict[str, Any], clock: FakeClock, **kw: Any) -> tuple[Beds24Adapter, Recorder]:
    rec = Recorder(beds24_routes(state))
    adapter = Beds24Adapter(
        property_id="P1",
        refresh_token="refresh-1",
        clock=clock,
        tax_inclusive=True,
        transport=rec.transport(),
        **kw,
    )
    return adapter, rec


async def test_beds24_availability_prices_and_room_types() -> None:
    state: dict[str, Any] = {}
    adapter, rec = beds24(state, FakeClock(NOW))
    answer = await adapter.quote(STAY, OCC, [])
    assert answer.source == "live_pms" and answer.status == "available"
    dz = next(q for q in answer.quotes if q.room_type == "101")
    assert (
        dz.per_night is not None
        and dz.per_night.cents == 12000
        and dz.total is not None
        and dz.total.cents == 24000
    )
    assert (
        dz.room_name == "Doppelzimmer"
        and dz.units_left == 3
        and dz.tax_inclusive is True
        and dz.binding is False
    )
    ez = next(q for q in answer.quotes if q.room_type == "102")
    assert (
        ez.units_left == 0
        and not ez.available
        and ez.per_night is not None
        and ez.per_night.cents == 9550
    )
    assert rec.paths()[0].endswith("/authentication/token") and state["tokens"] == 1
    await adapter.quote(STAY, OCC, ["101"])
    assert state["tokens"] == 1  # token reused within 24 h


async def test_beds24_restriction_violation_and_occupancy() -> None:
    state: dict[str, Any] = {"calendar": _beds24_calendar(min_stay=3)}
    adapter, _ = beds24(state, FakeClock(NOW))
    answer = await adapter.quote(STAY, Occupancy(3, 0), ["101"])
    assert answer.status == "restricted"
    kinds = {r.kind: r for r in answer.quotes[0].restrictions}
    assert kinds["min_stay"].value == "3" and kinds["min_stay"].alternative == "2026-10-06"
    assert kinds["max_occupancy"].value == "2" and kinds["max_occupancy"].alternative == "2"


async def test_beds24_token_refresh_on_401_and_auth_error() -> None:
    state: dict[str, Any] = {}
    adapter, rec = beds24(state, FakeClock(NOW))
    await adapter.list_room_types()
    adapter.token_state.token = "stale"
    answer = await adapter.quote(STAY, OCC, [])
    assert answer.status == "available" and state["tokens"] == 2
    assert [p for p in rec.paths() if p.endswith("/authentication/token")] == [
        "/v2/authentication/token"
    ] * 2
    bad, _ = beds24(state, FakeClock(NOW))
    bad._refresh_token = "wrong"
    with pytest.raises(AuthError):
        await bad.list_room_types()


async def test_beds24_429_timeout_malformed_and_credit_budget() -> None:
    state: dict[str, Any] = {"fail_next": (429, b"{}"), "fail_headers": {"Retry-After": "30"}}
    adapter, _ = beds24(state, FakeClock(NOW))
    with pytest.raises(RateLimited) as limited:
        await adapter.list_room_types()
    assert limited.value.retry_after_s == 30.0
    state["fail_next"] = ("timeout", b"")
    with pytest.raises(SourceUnavailable):
        await adapter.list_room_types()
    state["fail_next"] = (200, b"not json")
    with pytest.raises(MalformedResponse):
        await adapter.list_room_types()
    state["fail_next"] = (200, json.dumps({"success": True, "nodata": []}).encode())
    with pytest.raises(MalformedResponse):
        await adapter.list_room_types()
    state["fail_next"] = (500, b"{}")
    with pytest.raises(SourceUnavailable):
        await adapter.list_room_types()
    # credit budget: the vendor reports 20 credits left, the adapter stops until the window resets
    clock = FakeClock(NOW)
    state = {"credits": 20}
    adapter, rec = beds24(state, clock)
    await adapter.list_room_types()
    calls = len(rec.requests)
    with pytest.raises(RateLimited):
        await adapter.quote(STAY, OCC, [])
    assert len(rec.requests) == calls
    clock.advance(timedelta(minutes=6))
    state["credits"] = 90
    assert (await adapter.quote(STAY, OCC, [])).status == "available"


async def test_beds24_reservations_and_inquiry_write_gate() -> None:
    state: dict[str, Any] = {}
    adapter, _ = beds24(state, FakeClock(NOW))
    record = await adapter.get_reservation("5001")
    assert (
        record is not None and record.confirmation_number == "5001" and record.surname == "Muster"
    )
    assert record.arrival == date(2026, 10, 3) and record.adults == 2 and record.source == "beds24"
    assert await adapter.get_reservation("9999") is None
    state["booking_status"] = "inquiry"
    inquiry = await adapter.get_reservation("5001")
    assert inquiry is not None and inquiry.confirmation_number == ""
    arrivals = await adapter.arrivals(date(2026, 10, 1), date(2026, 10, 7))
    assert len(arrivals) == 1
    request = InquiryRequest(
        STAY,
        OCC,
        "101",
        "Anna",
        "Muster",
        "a@example.invalid",
        "+49151",
        "de",
        "Zimmer mit Blick",
        "k" * 40,
    )
    with pytest.raises(NotSupported):
        await adapter.create_inquiry(request)
    assert "posted" not in state
    writer, _ = beds24(state, FakeClock(NOW), allow_write=True)
    assert await writer.create_inquiry(request) == "7001"
    assert state["posted"][0]["status"] == "inquiry" and state["posted"][0]["roomId"] == "101"
    report = await writer.doctor()
    assert report.ok


# ----------------------------------------------------------------- apaleo
OFFER = {
    "arrival": "2026-10-03T15:00:00+02:00",
    "departure": "2026-10-05T11:00:00+02:00",
    "unitGroup": {"id": "HB-DBL", "code": "DBL", "name": "Double", "maxPersons": 2},
    "ratePlan": {"id": "HB-FLEX", "code": "FLEX", "name": "Flexible"},
    "availableUnits": 4,
    "totalGrossAmount": {"amount": 250.0, "currency": "EUR"},
    "cityTax": {"totalGrossAmount": {"amount": 6.0, "currency": "EUR"}},
    "cancellationFee": {
        "name": "Free until 18:00",
        "description": "Free cancellation until 18:00 the day before arrival",
    },
    "fees": [{"name": "Breakfast", "totalAmount": {"amount": 30.0}}],
    "timeSlices": [
        {
            "from": "2026-10-03T15:00:00+02:00",
            "to": "2026-10-04T11:00:00+02:00",
            "totalGrossAmount": {"amount": 120.0},
        },
        {
            "from": "2026-10-04T15:00:00+02:00",
            "to": "2026-10-05T11:00:00+02:00",
            "totalGrossAmount": {"amount": 130.0},
        },
    ],
}


def apaleo_routes(state: dict[str, Any]) -> Callable[[Recorder, httpx2.Request], httpx2.Response]:
    def route(rec: Recorder, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.url.host == "identity.apaleo.com":
            state["tokens"] = state.get("tokens", 0) + 1
            assert request.headers["authorization"].startswith("Basic ")
            return httpx2.Response(
                200,
                json={
                    "access_token": f"at{state['tokens']}",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
        if request.headers.get("authorization") not in {"Bearer at1", "Bearer at2"}:
            return httpx2.Response(401, json={"message": "unauthorized"})
        if state.get("fail_next"):
            status, body = state.pop("fail_next")
            if status == "timeout":
                raise httpx2.ConnectTimeout("slow")
            return httpx2.Response(status, content=body, headers=state.pop("fail_headers", {}))
        if path == "/booking/v1/offers":
            assert request.url.params["propertyId"] == "HB" and request.url.params["adults"] == "2"
            return httpx2.Response(200, json={"offers": state.get("offers", [OFFER])})
        if path == "/availability/v1/unit-groups":
            return httpx2.Response(
                200,
                json={
                    "timeSlices": [
                        {
                            "from": "2026-10-03",
                            "to": "2026-10-04",
                            "unitGroups": [
                                {
                                    "unitGroup": {"code": "DBL", "name": "Double"},
                                    "availableCount": 2,
                                    "sellableCount": state.get("sellable", 0),
                                }
                            ],
                        }
                    ]
                },
            )
        if path.startswith("/booking/v1/reservations/"):
            if path.endswith("/NOPE"):
                return httpx2.Response(404, json={"message": "not found"})
            return httpx2.Response(
                200,
                json={
                    "id": "XPGMSXGF-1",
                    "status": "Confirmed",
                    "arrival": "2026-10-03T15:00:00+02:00",
                    "departure": "2026-10-05T11:00:00+02:00",
                    "adults": 2,
                    "childrenAges": [4],
                    "unitGroup": {"code": "DBL"},
                    "primaryGuest": {
                        "firstName": "Anna",
                        "lastName": "Muster",
                        "email": "a@example.invalid",
                    },
                },
            )
        if path == "/booking/v1/reservations":
            assert request.url.params["dateFilter"] in {"Arrival", "Departure"}
            return httpx2.Response(
                200,
                json={
                    "reservations": [
                        {
                            "id": "XPGMSXGF-1",
                            "status": "Confirmed",
                            "arrival": "2026-10-03T15:00:00+02:00",
                            "departure": "2026-10-05T11:00:00+02:00",
                            "adults": 2,
                            "unitGroup": {"code": "DBL"},
                            "primaryGuest": {"lastName": "Muster"},
                        }
                    ],
                    "count": 1,
                },
            )
        if path == "/inventory/v1/properties/HB":
            return httpx2.Response(200, json={"id": "HB", "name": "Hotel Beispiel"})
        return httpx2.Response(404, json={})

    return route


def apaleo(state: dict[str, Any], clock: FakeClock) -> tuple[ApaleoAdapter, Recorder]:
    rec = Recorder(apaleo_routes(state))
    adapter = ApaleoAdapter(
        property_id="HB",
        client_id="cid",
        client_secret="csecret",
        clock=clock,
        transport=rec.transport(),
    )
    return adapter, rec


async def test_apaleo_offers_with_fees_city_tax_and_cancellation_as_untrusted_text() -> None:
    state: dict[str, Any] = {}
    adapter, rec = apaleo(state, FakeClock(NOW))
    answer = await adapter.quote(STAY, OCC, [])
    assert answer.status == "available" and answer.source == "live_pms"
    q = answer.quotes[0]
    assert q.room_type == "DBL" and q.total is not None and q.total.cents == 25000
    assert (
        q.per_night is not None
        and q.per_night.cents == 12000
        and q.tax_inclusive is True
        and q.units_left == 4
    )
    assert (
        q.extras["city_tax"] == "6 EUR"
        and "Free cancellation" in q.extras["cancellation"]
        and q.extras["fees"] == "Breakfast"
    )
    assert rec.requests[0].url.host == "identity.apaleo.com" and state["tokens"] == 1
    with_child = await adapter.quote(STAY, Occupancy(2, 1), [])
    assert with_child.quotes[0].extras["children"].startswith("child prices")


async def test_apaleo_no_offers_means_unavailable_or_restricted() -> None:
    state: dict[str, Any] = {"offers": [], "sellable": 0}
    adapter, _ = apaleo(state, FakeClock(NOW))
    answer = await adapter.quote(STAY, OCC, [])
    assert answer.status == "unavailable" and answer.quotes[0].units_left == 0
    state["sellable"] = 2
    restricted = await adapter.quote(STAY, OCC, [])
    assert (
        restricted.status == "restricted" and restricted.quotes[0].restrictions[0].kind == "closed"
    )


async def test_apaleo_token_refresh_429_timeout_malformed_and_reservations() -> None:
    state: dict[str, Any] = {}
    adapter, _ = apaleo(state, FakeClock(NOW))
    await adapter.quote(STAY, OCC, [])
    adapter.token_state.token = "expired"
    assert (await adapter.quote(STAY, OCC, [])).status == "available" and state["tokens"] == 2
    state["fail_next"] = (429, b"{}")
    state["fail_headers"] = {"Retry-After": "5"}
    with pytest.raises(RateLimited) as limited:
        await adapter.quote(STAY, OCC, [])
    assert limited.value.retry_after_s == 5.0
    state["fail_next"] = ("timeout", b"")
    with pytest.raises(SourceUnavailable):
        await adapter.quote(STAY, OCC, [])
    state["fail_next"] = (200, b'{"offers": "nope"}')
    with pytest.raises(MalformedResponse):
        await adapter.quote(STAY, OCC, [])
    record = await adapter.get_reservation("XPGMSXGF-1")
    assert (
        record is not None and record.confirmation_number == "XPGMSXGF-1" and record.children == 1
    )
    assert record.arrival == date(2026, 10, 3) and record.surname == "Muster"
    assert await adapter.get_reservation("NOPE") is None
    assert len(await adapter.arrivals(date(2026, 10, 1), date(2026, 10, 7))) == 1
    assert (await adapter.doctor()).ok


async def test_apaleo_never_writes() -> None:
    adapter, rec = apaleo({}, FakeClock(NOW))
    request = InquiryRequest(
        STAY, OCC, "DBL", "Anna", "Muster", "a@example.invalid", "", "de", "wish", "k" * 40
    )
    with pytest.raises(NotSupported):
        await adapter.create_inquiry(request)
    assert rec.requests == []


# ------------------------------------------------------------------- mews
def mews_routes(state: dict[str, Any]) -> Callable[[Recorder, httpx2.Request], httpx2.Response]:
    def route(rec: Recorder, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        body = json.loads(request.content)
        assert body["Client"].startswith("WITALCO")
        if state.get("fail_next"):
            status, content = state.pop("fail_next")
            if status == "timeout":
                raise httpx2.ReadTimeout("slow")
            return httpx2.Response(status, content=content, headers=state.pop("fail_headers", {}))
        if path.endswith("/distributor/v1/hotels/get"):
            return httpx2.Response(
                200,
                json={
                    "RoomCategories": [
                        {
                            "Id": "cat-dbl",
                            "Name": {"en-US": "Double", "de-DE": "Doppelzimmer"},
                            "NormalBedCount": 2,
                            "ExtraBedCount": 1,
                            "SpaceCount": 8,
                        }
                    ],
                    "TimeZoneIdentifier": "Europe/Berlin",
                },
            )
        if path.endswith("/distributor/v1/hotels/getAvailability"):
            state["availability_body"] = body
            return httpx2.Response(
                200,
                json={
                    "RoomCategoryAvailabilities": [
                        {
                            "RoomCategoryId": "cat-dbl",
                            "AvailableRoomCount": state.get("count", 2),
                            "RoomOccupancyAvailabilities": [
                                {
                                    "AdultCount": 2,
                                    "ChildCount": 0,
                                    "Pricing": [
                                        {
                                            "RateId": "rate-1",
                                            "Price": {
                                                "Total": {"EUR": 240.0},
                                                "AverageAmountPerTimeUnit": {"EUR": 120.0},
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            )
        if path.endswith("/connector/v1/reservations/getAll"):
            assert body["ClientToken"] == "ct" and body["AccessToken"] == "at"
            state["getall_body"] = body
            return httpx2.Response(
                200,
                json={
                    "Reservations": [
                        {
                            "Id": "r1",
                            "Number": "1234",
                            "State": "Confirmed",
                            "StartUtc": "2026-10-02T22:00:00Z",
                            "EndUtc": "2026-10-04T22:00:00Z",
                            "AdultCount": 2,
                            "ChildCount": 0,
                            "RequestedCategoryId": "cat-dbl",
                            "CustomerId": "c1",
                        }
                    ],
                    "Customers": [
                        {
                            "Id": "c1",
                            "FirstName": "Anna",
                            "LastName": "Muster",
                            "Email": "a@example.invalid",
                            "LanguageCode": "de-DE",
                        }
                    ],
                },
            )
        if path.endswith("/connector/v1/customers/add"):
            return httpx2.Response(200, json={"Id": "c9"})
        if path.endswith("/connector/v1/reservations/add"):
            state["added"] = body
            return httpx2.Response(
                200, json={"Reservations": [{"Reservation": {"Id": "r9", "Number": "9001"}}]}
            )
        return httpx2.Response(404, json={})

    return route


def mews(state: dict[str, Any], clock: FakeClock, **kw: Any) -> tuple[MewsAdapter, Recorder]:
    rec = Recorder(mews_routes(state))
    adapter = MewsAdapter(
        hotel_id="h1",
        configuration_id="cfg1",
        clock=clock,
        tz="Europe/Berlin",
        transport=rec.transport(),
        **kw,
    )
    return adapter, rec


def test_rule8_mews_time_units_are_utc_midnight_of_the_local_day() -> None:
    assert utc_midnight(date(2026, 10, 3), "Europe/Berlin") == "2026-10-02T22:00:00Z"
    assert utc_midnight(date(2026, 12, 20), "Europe/Berlin") == "2026-12-19T23:00:00Z"
    assert local_day("2026-10-02T22:00:00Z", "Europe/Berlin") == date(2026, 10, 3)
    assert local_day("2026-12-19T23:00:00Z", "Europe/Berlin") == date(2026, 12, 20)


async def test_mews_distributor_availability_and_names() -> None:
    state: dict[str, Any] = {}
    adapter, _ = mews(state, FakeClock(NOW))
    answer = await adapter.quote(STAY, OCC, [])
    assert (
        state["availability_body"]["StartUtc"] == "2026-10-02T22:00:00Z"
        and state["availability_body"]["EndUtc"] == "2026-10-04T22:00:00Z"
    )
    assert answer.status == "available"
    q = answer.quotes[0]
    assert (
        q.room_name == "Doppelzimmer"
        and q.per_night is not None
        and q.per_night.cents == 12000
        and q.total is not None
        and q.total.cents == 24000
    )
    assert q.units_left == 2 and q.max_occupancy == 3 and q.tax_inclusive is True
    state["count"] = 0
    unavailable = await adapter.quote(STAY, OCC, [])
    assert unavailable.status == "unavailable"
    restricted = await adapter.quote(STAY, Occupancy(4, 0), [])
    assert restricted.quotes[0].restrictions[0].kind == "max_occupancy"
    report = await adapter.doctor()
    assert report.ok and any("connector" in w for w in report.warnings)


async def test_mews_connector_reservations_write_gate_and_errors() -> None:
    state: dict[str, Any] = {}
    keyless, _ = mews(state, FakeClock(NOW))
    with pytest.raises(NotSupported):
        await keyless.get_reservation("1234")
    adapter, _ = mews(state, FakeClock(NOW), client_token="ct", access_token="at")
    record = await adapter.get_reservation("1234")
    assert (
        record is not None
        and record.arrival == date(2026, 10, 3)
        and record.departure == date(2026, 10, 5)
    )
    assert (
        record.confirmation_number == "1234" and record.surname == "Muster" and record.lang == "de"
    )
    arrivals = await adapter.arrivals(date(2026, 10, 1), date(2026, 10, 7))
    assert (
        len(arrivals) == 1
        and state["getall_body"]["ScheduledStartUtc"]["StartUtc"] == "2026-09-30T22:00:00Z"
    )
    request = InquiryRequest(
        STAY, OCC, "cat-dbl", "Anna", "Muster", "a@example.invalid", "", "de", "wish", "k" * 40
    )
    with pytest.raises(NotSupported):
        await adapter.create_inquiry(request)
    writer, _ = mews(state, FakeClock(NOW), client_token="ct", access_token="at", allow_write=True)
    assert await writer.create_inquiry(request) == "9001"
    assert state["added"]["Reservations"][0]["State"] == "Optional"
    state["fail_next"] = (401, b"{}")
    with pytest.raises(AuthError):
        await adapter.quote(STAY, OCC, [])
    state["fail_next"] = (429, b"{}")
    with pytest.raises(RateLimited):
        await adapter.quote(STAY, OCC, [])
    state["fail_next"] = (200, b'{"RoomCategoryAvailabilities": {}}')
    with pytest.raises(MalformedResponse):
        await adapter.quote(STAY, OCC, [])
    state["fail_next"] = ("timeout", b"")
    with pytest.raises(SourceUnavailable):
        await adapter.quote(STAY, OCC, [])
