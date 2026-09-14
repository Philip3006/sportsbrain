"""Offline provider-validation scenarios for future Top-5 activation.

The framework deliberately models provider behavior without importing a
provider client.  It is suitable for deterministic failure injection and
planning evidence; it is not a live authority selector or a network runner.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from src.football.production_contracts import ProductionContractError
from src.football.top5_adapters import TOP5_LEAGUE_ADAPTERS


class ProviderOutcome(str, Enum):
    """Provider responses that can be injected into an offline scenario."""

    BULK_SUCCESS = "bulk_success"
    HTTP_403 = "http_403"
    HTTP_429 = "http_429"
    TIMEOUT = "timeout"
    EMPTY_RESPONSE = "empty_response"
    MALFORMED_RESPONSE = "malformed_response"
    UNSUPPORTED_MARKET = "unsupported_market"
    PARTIAL_RESPONSE = "partial_response"
    STALE_ODDS = "stale_odds"
    MISSING_FIXTURE = "missing_fixture"
    WRONG_LEAGUE = "wrong_league"
    RESULT_DELAY = "result_delay"

ValidationOutcome = ProviderOutcome


@dataclass(frozen=True)
class ProviderAuthority:
    """Independent fixture, odds, and results authority choices.

    ``None`` is the intentional default.  A configured string is only a
    proposal for later validation; it does not register or contact anything.
    """

    fixture_authority: str | None = None
    odds_authority: str | None = None
    result_authority: str | None = None

    @property
    def fixture(self) -> str | None:
        return self.fixture_authority

    @property
    def odds(self) -> str | None:
        return self.odds_authority

    @property
    def results(self) -> str | None:
        return self.result_authority

    @property
    def is_complete(self) -> bool:
        return all(value is not None for value in (
            self.fixture_authority,
            self.odds_authority,
            self.result_authority,
        ))

    def validate(self) -> None:
        for name, value in (
            ("fixture_authority", self.fixture_authority),
            ("odds_authority", self.odds_authority),
            ("result_authority", self.result_authority),
        ):
            if value is not None and not value.strip():
                raise ProductionContractError(f"{name} must be nonblank when configured")

    def as_payload(self) -> dict[str, str | None]:
        self.validate()
        return {
            "fixture_authority": self.fixture_authority,
            "odds_authority": self.odds_authority,
            "result_authority": self.result_authority,
        }


@dataclass(frozen=True)
class ProviderValidationResponse:
    """One static response for a bulk odds or result validation attempt."""

    outcome: ProviderOutcome
    fixture_keys: tuple[str, ...] = ()
    latency_ms: int = 0
    status_code: int | None = None
    result_delay_seconds: int = 0

    def validate(self, requested_fixture_keys: Iterable[str] = ()) -> None:
        outcome = ProviderOutcome(self.outcome)
        requested = set(requested_fixture_keys)
        if len(set(self.fixture_keys)) != len(self.fixture_keys):
            raise ProductionContractError("provider response contains duplicate fixtures")
        if requested and not set(self.fixture_keys).issubset(requested):
            raise ProductionContractError("provider response contains an unknown fixture")
        if self.latency_ms < 0 or self.result_delay_seconds < 0:
            raise ProductionContractError("provider response timing must be non-negative")
        if self.status_code is not None and not 100 <= self.status_code <= 599:
            raise ProductionContractError("provider response status code is invalid")
        expected_status = {
            ProviderOutcome.HTTP_403: 403,
            ProviderOutcome.HTTP_429: 429,
        }.get(outcome)
        if expected_status is not None and self.status_code not in (None, expected_status):
            raise ProductionContractError("provider response status does not match outcome")


@dataclass(frozen=True)
class ProviderValidationScenario:
    """A complete, static validation scenario for one league."""

    league_code: str
    sport_key: str
    authority: ProviderAuthority = field(default_factory=ProviderAuthority)
    markets: tuple[str, ...] = ("h2h", "totals", "spreads")
    regions: tuple[str, ...] = ("eu",)
    max_retries: int = 0
    timeout_seconds: int = 10
    fallback_enabled: bool = True
    responses: tuple[ProviderValidationResponse, ...] = ()
    bulk_path: str = "/sports/{sport}/odds"
    event_fallback_path: str = "/sports/{sport}/events/{event_id}/odds"
    result_path: str = "/sports/{sport}/scores"

    def validate(self) -> None:
        if not self.league_code.strip() or not self.sport_key.strip():
            raise ProductionContractError("provider scenario requires league and sport identity")
        self.authority.validate()
        if not self.markets or not self.regions:
            raise ProductionContractError("provider scenario requires markets and regions")
        if any(not value.strip() for value in (*self.markets, *self.regions)):
            raise ProductionContractError("provider scenario contains a blank market or region")
        if self.max_retries < 0 or self.timeout_seconds <= 0:
            raise ProductionContractError("provider retry and timeout values are invalid")
        for path_name, path in (
            ("bulk_path", self.bulk_path),
            ("event_fallback_path", self.event_fallback_path),
            ("result_path", self.result_path),
        ):
            if not path.strip() or not path.startswith("/"):
                raise ProductionContractError(f"provider {path_name} must be an absolute route template")
        for response in self.responses:
            response.validate()


@dataclass(frozen=True)
class ProviderValidationReport:
    """Read-only metrics from one provider scenario."""

    league_code: str
    fixture_count: int
    covered_fixture_count: int
    fixture_authority: str | None
    odds_authority: str | None
    result_authority: str | None
    bulk_request_count: int
    fallback_event_request_count: int
    fixture_authority_request_count: int
    result_request_count: int
    retry_count: int
    total_provider_latency_ms: int
    timeout_count: int
    forbidden_count: int
    rate_limit_count: int
    empty_response_count: int
    malformed_response_count: int
    unsupported_market_count: int
    partial_response_count: int
    stale_odds_rejection_count: int
    missing_fixture_count: int
    wrong_league_count: int
    result_delay_count: int
    activation_allowed: bool = False
    fail_closed: bool = True

    @property
    def odds_request_count(self) -> int:
        return self.bulk_request_count + self.fallback_event_request_count

    @property
    def coverage(self) -> float:
        return self.covered_fixture_count / self.fixture_count if self.fixture_count else 1.0

    @property
    def provider_failure_count(self) -> int:
        return sum((
            self.timeout_count,
            self.forbidden_count,
            self.rate_limit_count,
            self.empty_response_count,
            self.malformed_response_count,
            self.unsupported_market_count,
            self.partial_response_count,
        ))

    @property
    def live_authority_configured(self) -> bool:
        return all(value is not None for value in (
            self.fixture_authority,
            self.odds_authority,
            self.result_authority,
        ))

    def validate(self) -> None:
        counts = (
            self.fixture_count,
            self.covered_fixture_count,
            self.bulk_request_count,
            self.fallback_event_request_count,
            self.fixture_authority_request_count,
            self.result_request_count,
            self.retry_count,
            self.total_provider_latency_ms,
            self.timeout_count,
            self.forbidden_count,
            self.rate_limit_count,
            self.empty_response_count,
            self.malformed_response_count,
            self.unsupported_market_count,
            self.partial_response_count,
            self.stale_odds_rejection_count,
            self.missing_fixture_count,
            self.wrong_league_count,
            self.result_delay_count,
        )
        if any(count < 0 for count in counts):
            raise ProductionContractError("provider validation counts must be non-negative")
        if self.covered_fixture_count > self.fixture_count:
            raise ProductionContractError("provider coverage cannot exceed fixtures")
        if self.activation_allowed:
            raise ProductionContractError("provider simulation cannot authorize activation")

    def as_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "league": self.league_code,
            "fixtures": self.fixture_count,
            "covered_fixtures": self.covered_fixture_count,
            "coverage": self.coverage,
            "fixture_authority": self.fixture_authority,
            "odds_authority": self.odds_authority,
            "result_authority": self.result_authority,
            "live_authority_configured": self.live_authority_configured,
            "bulk_odds_requests": self.bulk_request_count,
            "fallback_event_requests": self.fallback_event_request_count,
            "odds_requests": self.odds_request_count,
            "fixture_authority_requests": self.fixture_authority_request_count,
            "result_requests": self.result_request_count,
            "retries": self.retry_count,
            "provider_latency_ms": self.total_provider_latency_ms,
            "timeouts": self.timeout_count,
            "http_403": self.forbidden_count,
            "http_429": self.rate_limit_count,
            "empty_responses": self.empty_response_count,
            "malformed_responses": self.malformed_response_count,
            "unsupported_markets": self.unsupported_market_count,
            "partial_responses": self.partial_response_count,
            "stale_odds_rejections": self.stale_odds_rejection_count,
            "missing_fixtures": self.missing_fixture_count,
            "wrong_league": self.wrong_league_count,
            "result_delays": self.result_delay_count,
            "activation_allowed": False,
            "fail_closed": self.fail_closed,
        }


def simulate_provider_validation(
    scenario: ProviderValidationScenario,
    fixture_keys: Sequence[str],
    *,
    responses: Sequence[ProviderValidationResponse] | None = None,
    result_delayed: bool = False,
) -> ProviderValidationReport:
    """Run a deterministic provider scenario with no network behavior."""

    scenario.validate()
    keys = tuple(fixture_keys)
    if len(set(keys)) != len(keys) or any(not key.strip() for key in keys):
        raise ProductionContractError("provider validation fixtures must be unique and nonblank")
    supplied = tuple(responses if responses is not None else scenario.responses)
    if not supplied:
        supplied = (ProviderValidationResponse(ProviderOutcome.BULK_SUCCESS, keys),)
    for response in supplied:
        response.validate(keys)

    covered: set[str] = set()
    bulk_requests = 0
    fallback_requests = 0
    retries = 0
    latency = 0
    counts = {outcome: 0 for outcome in ProviderOutcome}
    pending = set(keys)
    max_attempts = scenario.max_retries + 1
    for attempt in range(max_attempts):
        response = supplied[min(attempt, len(supplied) - 1)]
        outcome = ProviderOutcome(response.outcome)
        bulk_requests += 1
        latency += response.latency_ms
        counts[outcome] += 1
        if outcome in (ProviderOutcome.BULK_SUCCESS, ProviderOutcome.PARTIAL_RESPONSE):
            covered.update(response.fixture_keys)
            pending.difference_update(response.fixture_keys)
        if outcome is ProviderOutcome.RESULT_DELAY:
            result_delayed = True
        if outcome in (ProviderOutcome.BULK_SUCCESS, ProviderOutcome.PARTIAL_RESPONSE):
            break
        if attempt < max_attempts - 1:
            retries += 1

    fallback_outcomes = {
        ProviderOutcome.HTTP_403,
        ProviderOutcome.HTTP_429,
        ProviderOutcome.TIMEOUT,
        ProviderOutcome.EMPTY_RESPONSE,
        ProviderOutcome.MALFORMED_RESPONSE,
        ProviderOutcome.UNSUPPORTED_MARKET,
        ProviderOutcome.STALE_ODDS,
        ProviderOutcome.MISSING_FIXTURE,
        ProviderOutcome.WRONG_LEAGUE,
        ProviderOutcome.PARTIAL_RESPONSE,
    }
    last_outcome = ProviderOutcome(supplied[min(max_attempts - 1, len(supplied) - 1)].outcome)
    if scenario.fallback_enabled and last_outcome in fallback_outcomes:
        fallback_requests = len(pending)
        covered.update(())

    failure_count = sum(
        counts[outcome]
        for outcome in fallback_outcomes | {ProviderOutcome.PARTIAL_RESPONSE}
    )
    authority_missing = not scenario.authority.is_complete
    report = ProviderValidationReport(
        league_code=scenario.league_code,
        fixture_count=len(keys),
        covered_fixture_count=len(covered.intersection(keys)),
        fixture_authority=scenario.authority.fixture_authority,
        odds_authority=scenario.authority.odds_authority,
        result_authority=scenario.authority.result_authority,
        bulk_request_count=bulk_requests,
        fallback_event_request_count=fallback_requests,
        fixture_authority_request_count=1 if keys else 0,
        result_request_count=1 if keys else 0,
        retry_count=retries,
        total_provider_latency_ms=latency,
        timeout_count=counts[ProviderOutcome.TIMEOUT],
        forbidden_count=counts[ProviderOutcome.HTTP_403],
        rate_limit_count=counts[ProviderOutcome.HTTP_429],
        empty_response_count=counts[ProviderOutcome.EMPTY_RESPONSE],
        malformed_response_count=counts[ProviderOutcome.MALFORMED_RESPONSE],
        unsupported_market_count=counts[ProviderOutcome.UNSUPPORTED_MARKET],
        partial_response_count=counts[ProviderOutcome.PARTIAL_RESPONSE],
        stale_odds_rejection_count=counts[ProviderOutcome.STALE_ODDS],
        missing_fixture_count=counts[ProviderOutcome.MISSING_FIXTURE] + len(pending),
        wrong_league_count=counts[ProviderOutcome.WRONG_LEAGUE],
        result_delay_count=1 if result_delayed else counts[ProviderOutcome.RESULT_DELAY],
        activation_allowed=False,
        fail_closed=bool(failure_count or pending or authority_missing or result_delayed),
    )
    report.validate()
    return report


@dataclass(frozen=True)
class ProviderValidationFramework:
    """Five-league scenario collection with live authority left unset."""

    scenarios: Mapping[str, ProviderValidationScenario]

    @classmethod
    def for_top5(cls) -> ProviderValidationFramework:
        scenarios = {
            code: ProviderValidationScenario(
                league_code=code,
                sport_key=adapter.config.provider_sport_key,
            )
            for code, adapter in TOP5_LEAGUE_ADAPTERS.items()
        }
        return cls(MappingProxyType(scenarios))

    def validate(self) -> None:
        expected = set(TOP5_LEAGUE_ADAPTERS)
        if set(self.scenarios) != expected:
            raise ProductionContractError("provider validation framework must cover all five Top-5 leagues")
        for code, scenario in self.scenarios.items():
            if code != scenario.league_code:
                raise ProductionContractError("provider scenario key differs from league identity")
            scenario.validate()

    def run(
        self,
        fixtures_by_league: Mapping[str, Sequence[str]],
        responses_by_league: Mapping[str, Sequence[ProviderValidationResponse]] | None = None,
    ) -> Mapping[str, ProviderValidationReport]:
        self.validate()
        unknown = set(fixtures_by_league) - set(self.scenarios)
        if unknown:
            raise ProductionContractError(f"provider validation has unknown leagues: {sorted(unknown)}")
        responses_by_league = responses_by_league or {}
        reports = {
            code: simulate_provider_validation(
                self.scenarios[code],
                fixtures_by_league.get(code, ()),
                responses=responses_by_league.get(code),
            )
            for code in self.scenarios
        }
        return MappingProxyType(reports)


def provider_failure_matrix() -> tuple[ProviderOutcome, ...]:
    """Canonical outcomes used by failure-injection tests and runbooks."""

    return (
        ProviderOutcome.TIMEOUT,
        ProviderOutcome.HTTP_403,
        ProviderOutcome.HTTP_429,
        ProviderOutcome.EMPTY_RESPONSE,
        ProviderOutcome.MALFORMED_RESPONSE,
        ProviderOutcome.UNSUPPORTED_MARKET,
        ProviderOutcome.PARTIAL_RESPONSE,
        ProviderOutcome.STALE_ODDS,
        ProviderOutcome.MISSING_FIXTURE,
        ProviderOutcome.WRONG_LEAGUE,
        ProviderOutcome.RESULT_DELAY,
    )
