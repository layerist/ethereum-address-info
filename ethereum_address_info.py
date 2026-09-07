#!/usr/bin/env python3
"""
Production-oriented multichain Etherscan API V2 client (v4).

Highlights
----------
- Etherscan API V2 with configurable chain ID and base URL.
- Strict configuration and argument validation.
- Global thread-safe token-bucket rate limiter.
- Global rate-limit cooldown after HTTP/API throttling.
- Thread-local requests.Session objects with connection pooling.
- Explicit retry policy with Retry-After, exponential backoff and jitter.
- Circuit breaker with CLOSED / OPEN / HALF_OPEN semantics.
- Circuit-breaker generations prevent stale in-flight requests from
  incorrectly closing a newly opened circuit.
- Ordered, bounded parallel pagination.
- Safe executor shutdown and prompt cancellation.
- Serial streaming mode.
- Native / internal / ERC-20 / ERC-721 / ERC-1155 iterators.
- Native and ERC-20 balance helpers.
- Atomic JSONL output.
- Environment variables + CLI overrides.
- Graceful SIGINT/SIGTERM handling.
- Deterministic exit codes.
- Optional runtime statistics.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, getcontext
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import FrameType
from typing import Any, Iterator, Mapping, Optional, Sequence, TextIO, TypedDict, cast
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests import Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import JSONDecodeError, RequestException


load_dotenv()
getcontext().prec = 80

LOGGER = logging.getLogger("etherscan")

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

TRANSIENT_HTTP_STATUSES = frozenset({
    408,
    425,
    429,
    500,
    502,
    503,
    504,
})

RETRYABLE_API_MARKERS = (
    "rate limit",
    "rate-limit",
    "max rate limit",
    "maximum rate limit",
    "server too busy",
    "query timeout",
    "timeout occurred",
    "temporarily unavailable",
    "service unavailable",
    "please try again",
    "busy",
    "throttle",
)

EMPTY_RESULT_MARKERS = (
    "no transactions found",
    "no records found",
)

ACCOUNT_ACTIONS = frozenset({
    "txlist",
    "txlistinternal",
    "tokentx",
    "tokennfttx",
    "token1155tx",
})

TOKEN_ACTIONS = frozenset({
    "tokentx",
    "tokennfttx",
    "token1155tx",
})


class EtherscanError(RuntimeError):
    """Base client error."""


class EtherscanValidationError(EtherscanError):
    """Invalid configuration or method argument."""


class EtherscanRequestError(EtherscanError):
    """HTTP/network request failed after all retries."""


class EtherscanResponseError(EtherscanError):
    """Etherscan returned an invalid or non-retryable response."""


class EtherscanCircuitOpenError(EtherscanError):
    """Requests are temporarily blocked by the circuit breaker."""


class EtherscanResponse(TypedDict, total=False):
    status: str
    message: str
    result: Any


@dataclass(slots=True)
class ClientStats:
    requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    retries: int = 0
    rate_limits: int = 0
    empty_results: int = 0
    records: int = 0
    pages: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "retries": self.retries,
            "rate_limits": self.rate_limits,
            "empty_results": self.empty_results,
            "pages": self.pages,
            "records": self.records,
        }


@dataclass(slots=True)
class AtomicOutput:
    """Write to a sibling temporary file and atomically replace target."""

    final_path: Path
    temp_path: Path
    stream: TextIO
    _finished: bool = False

    def commit(self) -> None:
        if self._finished:
            return

        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()

        os.replace(self.temp_path, self.final_path)
        self._finished = True

    def abort(self) -> None:
        if self._finished:
            return

        try:
            self.stream.close()
        finally:
            try:
                self.temp_path.unlink()
            except FileNotFoundError:
                pass

        self._finished = True


class TokenBucketRateLimiter:
    """
    Thread-safe global token bucket.

    All workers share one limiter.

    defer_for() is intentionally global:
    if Etherscan tells us to slow down, every worker slows down.
    """

    def __init__(
        self,
        rate: float,
        capacity: Optional[float] = None,
    ) -> None:
        if not rate > 0:
            raise EtherscanValidationError(
                "rate_limit_per_sec must be > 0"
            )

        self._rate = float(rate)
        self._capacity = float(
            capacity if capacity is not None else max(1.0, rate)
        )

        if self._capacity < 1.0:
            raise EtherscanValidationError(
                "rate_limit_burst must be >= 1"
            )

        self._tokens = self._capacity
        self._updated_at = time.monotonic()
        self._blocked_until = 0.0

        self._lock = threading.Lock()

    def defer_for(self, delay: float) -> None:
        """
        Globally pause requests.

        Tokens are drained so that a 429 followed by cooldown does not
        immediately produce a burst.
        """
        if delay <= 0:
            return

        with self._lock:
            now = time.monotonic()

            self._blocked_until = max(
                self._blocked_until,
                now + delay,
            )

            # Drain accumulated burst capacity.
            self._tokens = 0.0
            self._updated_at = now

    def acquire(self, stop_event: threading.Event) -> None:
        while True:
            if stop_event.is_set():
                raise KeyboardInterrupt

            with self._lock:
                now = time.monotonic()

                if now < self._blocked_until:
                    delay = self._blocked_until - now
                else:
                    elapsed = max(
                        0.0,
                        now - self._updated_at,
                    )

                    self._updated_at = now

                    self._tokens = min(
                        self._capacity,
                        self._tokens + elapsed * self._rate,
                    )

                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return

                    delay = (1.0 - self._tokens) / self._rate

            if stop_event.wait(delay):
                raise KeyboardInterrupt


@dataclass(frozen=True, slots=True)
class CircuitLease:
    generation: int
    probe: bool


class CircuitBreaker:
    """
    CLOSED -> OPEN -> HALF_OPEN -> CLOSED.

    A generation is attached to every logical request.

    This prevents an old in-flight request from doing:

        record_success()

    after another worker has already opened a newer circuit.
    """

    def __init__(
        self,
        failure_threshold: int,
        recovery_timeout: float,
    ) -> None:
        if failure_threshold < 1:
            raise EtherscanValidationError(
                "circuit_breaker_failures must be >= 1"
            )

        if recovery_timeout <= 0:
            raise EtherscanValidationError(
                "circuit_breaker_recovery_sec must be > 0"
            )

        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout

        self._failures = 0
        self._opened_at: Optional[float] = None
        self._probe_owner: Optional[int] = None
        self._generation = 0

        self._lock = threading.Lock()

    def before_operation(self) -> CircuitLease:
        owner = threading.get_ident()

        with self._lock:
            if self._opened_at is None:
                return CircuitLease(
                    generation=self._generation,
                    probe=False,
                )

            elapsed = time.monotonic() - self._opened_at

            if elapsed < self._recovery_timeout:
                remaining = self._recovery_timeout - elapsed

                raise EtherscanCircuitOpenError(
                    f"Circuit breaker is open; retry in "
                    f"{remaining:.1f}s"
                )

            # HALF_OPEN.
            if self._probe_owner is None:
                self._probe_owner = owner

                return CircuitLease(
                    generation=self._generation,
                    probe=True,
                )

            if self._probe_owner == owner:
                return CircuitLease(
                    generation=self._generation,
                    probe=True,
                )

            raise EtherscanCircuitOpenError(
                "Circuit breaker half-open probe in progress"
            )

    def record_success(self, lease: CircuitLease) -> None:
        with self._lock:
            if lease.generation != self._generation:
                return

            self._failures = 0
            self._opened_at = None
            self._probe_owner = None
            self._generation += 1

    def record_failure(self, lease: CircuitLease) -> None:
        with self._lock:
            if lease.generation != self._generation:
                return

            self._probe_owner = None
            self._failures += 1

            if self._failures >= self._failure_threshold:
                self._opened_at = time.monotonic()
                self._generation += 1

    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "CLOSED"

            elapsed = time.monotonic() - self._opened_at

            if elapsed >= self._recovery_timeout:
                return "HALF_OPEN"

            return "OPEN"


@dataclass(frozen=True, slots=True)
class EtherscanConfig:
    api_key: str
    address: str

    chain_id: int = 1

    base_url: str = (
        "https://api.etherscan.io/v2/api"
    )

    connect_timeout: float = 5.0
    read_timeout: float = 30.0

    retries: int = 5
    backoff_factor: float = 0.5
    max_backoff: float = 30.0

    rate_limit_per_sec: float = 3.0
    rate_limit_burst: float = 1.0

    max_workers: int = 3
    pagination_window: int = 0
    page_size: int = 1000

    circuit_breaker_failures: int = 8
    circuit_breaker_recovery_sec: float = 30.0

    user_agent: str = "etherscan-v2-client/4.0"

    proxies: Optional[Mapping[str, str]] = None
    trust_env: bool = True

    def validate(self) -> None:
        if not self.api_key.strip():
            raise EtherscanValidationError(
                "ETHERSCAN_API_KEY is missing"
            )

        validate_address(self.address)

        if self.chain_id <= 0:
            raise EtherscanValidationError(
                "chain_id must be > 0"
            )

        parsed = urlparse(self.base_url)

        if parsed.scheme not in {"http", "https"}:
            raise EtherscanValidationError(
                "base_url must use HTTP or HTTPS"
            )

        if not parsed.netloc:
            raise EtherscanValidationError(
                "base_url must contain a hostname"
            )

        if self.connect_timeout <= 0:
            raise EtherscanValidationError(
                "connect_timeout must be > 0"
            )

        if self.read_timeout <= 0:
            raise EtherscanValidationError(
                "read_timeout must be > 0"
            )

        if self.retries < 1:
            raise EtherscanValidationError(
                "retries must be >= 1"
            )

        if self.backoff_factor < 0:
            raise EtherscanValidationError(
                "backoff_factor must be >= 0"
            )

        if self.max_backoff <= 0:
            raise EtherscanValidationError(
                "max_backoff must be > 0"
            )

        if self.max_workers < 1:
            raise EtherscanValidationError(
                "max_workers must be >= 1"
            )

        if self.pagination_window < 0:
            raise EtherscanValidationError(
                "pagination_window must be >= 0"
            )

        if not 1 <= self.page_size <= 1000:
            raise EtherscanValidationError(
                "page_size must be between 1 and 1000"
            )

        if self.circuit_breaker_failures < 1:
            raise EtherscanValidationError(
                "circuit_breaker_failures must be >= 1"
            )

        if self.circuit_breaker_recovery_sec <= 0:
            raise EtherscanValidationError(
                "circuit_breaker_recovery_sec must be > 0"
            )


class EtherscanClient:
    def __init__(self, config: EtherscanConfig) -> None:
        config.validate()

        self.config = config

        self._stop_event = threading.Event()

        self._rate_limiter = TokenBucketRateLimiter(
            config.rate_limit_per_sec,
            config.rate_limit_burst,
        )

        self._circuit_breaker = CircuitBreaker(
            config.circuit_breaker_failures,
            config.circuit_breaker_recovery_sec,
        )

        self._thread_local = threading.local()

        self._sessions: list[Session] = []
        self._sessions_lock = threading.Lock()

        self._state_lock = threading.Lock()

        self._closed = False

        self._stats = ClientStats()
        self._stats_lock = threading.Lock()

    def __enter__(self) -> "EtherscanClient":
        return self

    def __exit__(
        self,
        exc_type: Any,
        exc: Any,
        tb: Any,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return

            self._closed = True
            self._stop_event.set()

        with self._sessions_lock:
            sessions = self._sessions
            self._sessions = []

        for session in sessions:
            try:
                session.close()
            except Exception:
                LOGGER.debug(
                    "Failed to close requests.Session",
                    exc_info=True,
                )

    def stop(self) -> None:
        self._stop_event.set()

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            snapshot = self._stats.snapshot()

        snapshot["circuit"] = self._circuit_breaker.state()

        return snapshot

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise EtherscanError(
                    "Client is closed"
                )

    def _inc_stat(
        self,
        name: str,
        amount: int = 1,
    ) -> None:
        with self._stats_lock:
            setattr(
                self._stats,
                name,
                getattr(self._stats, name) + amount,
            )

    def _session(self) -> Session:
        self._ensure_open()

        session = getattr(
            self._thread_local,
            "session",
            None,
        )

        if session is not None:
            return cast(Session, session)

        session = requests.Session()

        session.trust_env = self.config.trust_env

        pool_size = max(
            1,
            self.config.max_workers,
        )

        adapter = HTTPAdapter(
            max_retries=0,
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            pool_block=True,
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "User-Agent": self.config.user_agent,
        })

        if self.config.proxies:
            session.proxies.update(
                dict(self.config.proxies)
            )

        self._thread_local.session = session

        with self._sessions_lock:
            self._sessions.append(session)

        return session

    def _request(
        self,
        params: Mapping[str, Any],
    ) -> Any:
        self._ensure_open()

        lease = self._circuit_breaker.before_operation()

        query = {
            "apikey": self.config.api_key,
            "chainid": str(self.config.chain_id),
            **{
                key: str(value)
                for key, value in params.items()
                if value is not None
            },
        }

        safe_query = {
            **query,
            "apikey": "***",
        }

        last_error: Optional[BaseException] = None

        for attempt in range(self.config.retries):
            if self._stop_event.is_set():
                raise KeyboardInterrupt

            self._inc_stat("requests")

            self._rate_limiter.acquire(
                self._stop_event
            )

            response: Optional[Response] = None

            try:
                response = self._session().get(
                    self.config.base_url,
                    params=query,
                    timeout=(
                        self.config.connect_timeout,
                        self.config.read_timeout,
                    ),
                )

                if response.status_code == 429:
                    self._inc_stat("rate_limits")

                if response.status_code in TRANSIENT_HTTP_STATUSES:
                    raise EtherscanRequestError(
                        f"Transient HTTP "
                        f"{response.status_code}: "
                        f"{response.text[:300]!r}"
                    )

                response.raise_for_status()

                payload = self._decode_response(
                    response
                )

                retryable, result = self._parse_payload(
                    payload
                )

                if retryable:
                    raise EtherscanRequestError(
                        "Retryable Etherscan response: "
                        f"{payload.get('result')!r}"
                    )

                self._inc_stat(
                    "successful_requests"
                )

                self._circuit_breaker.record_success(
                    lease
                )

                return result

            except EtherscanResponseError:
                # A valid permanent API error does not imply
                # upstream infrastructure failure.
                self._circuit_breaker.record_success(
                    lease
                )
                raise

            except (
                RequestException,
                JSONDecodeError,
                EtherscanRequestError,
            ) as exc:
                last_error = exc

                if attempt + 1 >= self.config.retries:
                    break

                delay = self._retry_delay(
                    attempt,
                    response,
                )

                is_rate_limited = (
                    response is not None
                    and response.status_code == 429
                ) or self._looks_like_rate_limit(exc)

                if is_rate_limited:
                    self._inc_stat("rate_limits")
                    self._rate_limiter.defer_for(
                        delay
                    )

                self._inc_stat("retries")

                self._log_retry(
                    safe_query,
                    attempt,
                    delay,
                    exc,
                )

                self._wait(delay)

            finally:
                if response is not None:
                    response.close()

        self._inc_stat("failed_requests")

        self._circuit_breaker.record_failure(
            lease
        )

        raise EtherscanRequestError(
            f"Request failed after "
            f"{self.config.retries} attempts: "
            f"{last_error}"
        ) from last_error

    @staticmethod
    def _decode_response(
        response: Response,
    ) -> EtherscanResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise EtherscanResponseError(
                "Etherscan returned invalid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise EtherscanResponseError(
                "Expected JSON object, got "
                f"{type(payload).__name__}"
            )

        return cast(
            EtherscanResponse,
            payload,
        )

    def _parse_payload(
        self,
        payload: EtherscanResponse,
    ) -> tuple[bool, Any]:
        status = str(
            payload.get("status", "")
        )

        message = str(
            payload.get("message", "")
        )

        result = payload.get("result")

        if status == "1":
            return False, result

        combined = (
            f"{message} {result}"
        ).strip().lower()

        if any(
            marker in combined
            for marker in RETRYABLE_API_MARKERS
        ):
            return True, None

        if any(
            marker in combined
            for marker in EMPTY_RESULT_MARKERS
        ):
            self._inc_stat("empty_results")
            return False, []

        raise EtherscanResponseError(
            "Etherscan error: "
            f"status={status!r}, "
            f"message={message!r}, "
            f"result={result!r}"
        )

    @staticmethod
    def _looks_like_rate_limit(
        error: BaseException,
    ) -> bool:
        text = str(error).lower()

        return (
            "rate limit" in text
            or "rate-limit" in text
            or "too many requests" in text
            or "throttl" in text
        )

    def _retry_delay(
        self,
        attempt: int,
        response: Optional[Response] = None,
    ) -> float:
        retry_after = self._parse_retry_after(
            response
        )

        if retry_after is not None:
            jitter = min(
                retry_after * 0.10,
                2.0,
            )

            return min(
                retry_after
                + random.uniform(0.0, jitter),
                self.config.max_backoff,
            )

        base = min(
            self.config.backoff_factor
            * (2 ** attempt),
            self.config.max_backoff,
        )

        jitter = min(
            max(0.05, base * 0.25),
            2.0,
        )

        return min(
            base + random.uniform(
                0.0,
                jitter,
            ),
            self.config.max_backoff,
        )

    @staticmethod
    def _parse_retry_after(
        response: Optional[Response],
    ) -> Optional[float]:
        if response is None:
            return None

        value = response.headers.get(
            "Retry-After"
        )

        if not value:
            return None

        try:
            return max(
                0.0,
                float(value.strip()),
            )
        except ValueError:
            pass

        try:
            target = parsedate_to_datetime(
                value
            )

            return max(
                0.0,
                target.timestamp() - time.time(),
            )
        except (
            TypeError,
            ValueError,
            OverflowError,
        ):
            return None

    def _wait(self, delay: float) -> None:
        if self._stop_event.wait(delay):
            raise KeyboardInterrupt

    def _log_retry(
        self,
        query: Mapping[str, str],
        attempt: int,
        delay: float,
        error: BaseException,
    ) -> None:
        LOGGER.warning(
            "Request %d/%d failed for %s/%s: "
            "%s; retrying in %.2fs",
            attempt + 1,
            self.config.retries,
            query.get("module"),
            query.get("action"),
            error,
            delay,
        )

    @staticmethod
    def units_to_decimal(
        value: str | int,
        decimals: int = 18,
    ) -> Decimal:
        if not 0 <= decimals <= 255:
            raise EtherscanValidationError(
                "decimals must be between 0 and 255"
            )

        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise EtherscanResponseError(
                f"Invalid numeric result: {value!r}"
            ) from exc

        return numeric / (
            Decimal(10) ** decimals
        )

    def get_balance(
        self,
        address: Optional[str] = None,
    ) -> Decimal:
        target = normalize_address(
            address or self.config.address
        )

        value = self._request({
            "module": "account",
            "action": "balance",
            "address": target,
            "tag": "latest",
        })

        return self.units_to_decimal(value)

    def get_token_balance(
        self,
        contract_address: str,
        decimals: int = 18,
        address: Optional[str] = None,
    ) -> Decimal:
        contract = normalize_address(
            contract_address,
            field="contract_address",
        )

        target = normalize_address(
            address or self.config.address
        )

        value = self._request({
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract,
            "address": target,
            "tag": "latest",
        })

        return self.units_to_decimal(
            value,
            decimals,
        )

    def get_account_page(
        self,
        action: str,
        page: int,
        *,
        offset: Optional[int] = None,
        start_block: int = 0,
        end_block: int = 99_999_999,
        sort: str = "asc",
        address: Optional[str] = None,
        contract_address: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if action not in ACCOUNT_ACTIONS:
            raise EtherscanValidationError(
                f"Unsupported account action: {action!r}"
            )

        page_size = (
            offset
            if offset is not None
            else self.config.page_size
        )

        validate_page_arguments(
            page,
            page_size,
            start_block,
            end_block,
            sort,
        )

        target = normalize_address(
            address or self.config.address
        )

        if (
            contract_address
            and action not in TOKEN_ACTIONS
        ):
            raise EtherscanValidationError(
                f"contract_address filter is not "
                f"supported for action {action!r}"
            )

        contract = (
            normalize_address(
                contract_address,
                field="contract_address",
            )
            if contract_address
            else None
        )

        result = self._request({
            "module": "account",
            "action": action,
            "address": target,
            "contractaddress": contract,
            "startblock": start_block,
            "endblock": end_block,
            "page": page,
            "offset": page_size,
            "sort": sort,
        })

        if not isinstance(result, list):
            raise EtherscanResponseError(
                f"{action} result is not a list"
            )

        if not all(
            isinstance(item, dict)
            for item in result
        ):
            raise EtherscanResponseError(
                f"{action} result contains "
                "non-object items"
            )

        return cast(
            list[dict[str, Any]],
            result,
        )

    def iter_account_records(
        self,
        action: str = "txlist",
        *,
        page_size: Optional[int] = None,
        max_pages: Optional[int] = None,
        start_block: int = 0,
        end_block: int = 99_999_999,
        sort: str = "asc",
        parallel: bool = True,
        address: Optional[str] = None,
        contract_address: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        size = (
            page_size
            if page_size is not None
            else self.config.page_size
        )

        validate_page_arguments(
            1,
            size,
            start_block,
            end_block,
            sort,
        )

        if max_pages is not None and max_pages < 1:
            raise EtherscanValidationError(
                "max_pages must be >= 1 or None"
            )

        if action not in ACCOUNT_ACTIONS:
            raise EtherscanValidationError(
                f"Unsupported account action: {action!r}"
            )

        kwargs = {
            "action": action,
            "page_size": size,
            "max_pages": max_pages,
            "start_block": start_block,
            "end_block": end_block,
            "sort": sort,
            "address": address,
            "contract_address": contract_address,
        }

        if (
            parallel
            and self.config.max_workers > 1
        ):
            yield from self._iter_pages_parallel(
                **kwargs
            )
        else:
            yield from self._iter_pages_serial(
                **kwargs
            )

    def iter_transactions(
        self,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        yield from self.iter_account_records(
            "txlist",
            **kwargs,
        )

    def iter_internal_transactions(
        self,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        yield from self.iter_account_records(
            "txlistinternal",
            **kwargs,
        )

    def iter_erc20_transfers(
        self,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        yield from self.iter_account_records(
            "tokentx",
            **kwargs,
        )

    def iter_erc721_transfers(
        self,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        yield from self.iter_account_records(
            "tokennfttx",
            **kwargs,
        )

    def iter_erc1155_transfers(
        self,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        yield from self.iter_account_records(
            "token1155tx",
            **kwargs,
        )

    def _iter_pages_serial(
        self,
        *,
        action: str,
        page_size: int,
        max_pages: Optional[int],
        start_block: int,
        end_block: int,
        sort: str,
        address: Optional[str],
        contract_address: Optional[str],
    ) -> Iterator[dict[str, Any]]:
        page = 1

        while (
            max_pages is None
            or page <= max_pages
        ):
            records = self.get_account_page(
                action,
                page,
                offset=page_size,
                start_block=start_block,
                end_block=end_block,
                sort=sort,
                address=address,
                contract_address=contract_address,
            )

            self._inc_stat("pages")
            self._inc_stat(
                "records",
                len(records),
            )

            LOGGER.info(
                "Fetched %s page %d (%d records)",
                action,
                page,
                len(records),
            )

            yield from records

            if len(records) < page_size:
                return

            page += 1

    def _iter_pages_parallel(
        self,
        *,
        action: str,
        page_size: int,
        max_pages: Optional[int],
        start_block: int,
        end_block: int,
        sort: str,
        address: Optional[str],
        contract_address: Optional[str],
    ) -> Iterator[dict[str, Any]]:
        """
        Ordered bounded parallel pagination.

        Important:
        Etherscan does not expose a reliable total-page count for
        these endpoints, so pages after the terminal page are
        necessarily speculative.

        The speculative window is deliberately bounded.
        """

        workers = self.config.max_workers

        if self.config.pagination_window:
            window = min(
                max(
                    1,
                    self.config.pagination_window,
                ),
                workers,
            )
        else:
            window = workers

        next_submit = 1
        next_emit = 1

        terminal_page: Optional[int] = None

        completed: dict[
            int,
            list[dict[str, Any]],
        ] = {}

        in_flight: dict[
            Future[list[dict[str, Any]]],
            int,
        ] = {}

        executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="etherscan",
        )

        def can_submit(page: int) -> bool:
            if self._stop_event.is_set():
                return False

            if (
                max_pages is not None
                and page > max_pages
            ):
                return False

            if (
                terminal_page is not None
                and page > terminal_page
            ):
                return False

            return page < next_emit + window

        def submit(page: int) -> None:
            future = executor.submit(
                self.get_account_page,
                action,
                page,
                offset=page_size,
                start_block=start_block,
                end_block=end_block,
                sort=sort,
                address=address,
                contract_address=contract_address,
            )

            in_flight[future] = page

        try:
            while (
                len(in_flight) < workers
                and can_submit(next_submit)
            ):
                submit(next_submit)
                next_submit += 1

            while in_flight:
                if self._stop_event.is_set():
                    raise KeyboardInterrupt

                done, _ = wait(
                    in_flight,
                    timeout=0.25,
                    return_when=FIRST_COMPLETED,
                )

                if not done:
                    continue

                for future in done:
                    page = in_flight.pop(future)

                    # future.result() deliberately propagates
                    # worker exceptions to the caller.
                    records = future.result()

                    completed[page] = records

                    self._inc_stat("pages")
                    self._inc_stat(
                        "records",
                        len(records),
                    )

                    LOGGER.info(
                        "Fetched %s page %d (%d records)",
                        action,
                        page,
                        len(records),
                    )

                    if len(records) < page_size:
                        if terminal_page is None:
                            terminal_page = page
                        else:
                            terminal_page = min(
                                terminal_page,
                                page,
                            )

                while next_emit in completed:
                    records = completed.pop(
                        next_emit
                    )

                    yield from records

                    if len(records) < page_size:
                        return

                    next_emit += 1

                while (
                    len(in_flight) < workers
                    and can_submit(next_submit)
                ):
                    submit(next_submit)
                    next_submit += 1

        finally:
            # Cancel tasks that haven't started yet.
            for future in in_flight:
                future.cancel()

            # IMPORTANT:
            # wait=True prevents worker threads from continuing to use
            # Session objects after EtherscanClient.__exit__ closes them.
            executor.shutdown(
                wait=True,
                cancel_futures=True,
            )


def validate_address(
    value: str,
    *,
    field: str = "address",
) -> None:
    if (
        not isinstance(value, str)
        or not ADDRESS_RE.fullmatch(value.strip())
    ):
        raise EtherscanValidationError(
            f"Invalid {field}: {value!r}"
        )


def normalize_address(
    value: str,
    *,
    field: str = "address",
) -> str:
    validate_address(
        value,
        field=field,
    )

    return value.strip().lower()


def validate_page_arguments(
    page: int,
    offset: int,
    start_block: int,
    end_block: int,
    sort: str,
) -> None:
    if page < 1:
        raise EtherscanValidationError(
            "page must be >= 1"
        )

    if not 1 <= offset <= 1000:
        raise EtherscanValidationError(
            "offset/page_size must be "
            "between 1 and 1000"
        )

    if start_block < 0:
        raise EtherscanValidationError(
            "start_block must be >= 0"
        )

    if end_block < start_block:
        raise EtherscanValidationError(
            "end_block must be >= start_block"
        )

    if sort not in {"asc", "desc"}:
        raise EtherscanValidationError(
            "sort must be 'asc' or 'desc'"
        )


def env_int(
    name: str,
    default: int,
) -> int:
    raw = os.getenv(name)

    if raw in (None, ""):
        return default

    try:
        return int(raw)
    except ValueError as exc:
        raise EtherscanValidationError(
            f"{name} must be an integer"
        ) from exc


def env_float(
    name: str,
    default: float,
) -> float:
    raw = os.getenv(name)

    if raw in (None, ""):
        return default

    try:
        return float(raw)
    except ValueError as exc:
        raise EtherscanValidationError(
            f"{name} must be a number"
        ) from exc


def env_bool(
    name: str,
    default: bool,
) -> bool:
    raw = os.getenv(name)

    if raw in (None, ""):
        return default

    value = raw.strip().lower()

    if value in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True

    if value in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False

    raise EtherscanValidationError(
        f"{name} must be one of: "
        "true/false, 1/0, yes/no, on/off"
    )


def build_proxy_mapping() -> Optional[dict[str, str]]:
    explicit = os.getenv(
        "ETHERSCAN_PROXY"
    )

    if not explicit:
        return None

    parsed = urlparse(explicit)

    if parsed.scheme not in {
        "http",
        "https",
        "socks5",
        "socks5h",
    }:
        raise EtherscanValidationError(
            "ETHERSCAN_PROXY must use "
            "http/https/socks5/socks5h"
        )

    if not parsed.netloc:
        raise EtherscanValidationError(
            "ETHERSCAN_PROXY is invalid"
        )

    return {
        "http": explicit,
        "https": explicit,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reliable Etherscan API V2 client"
        )
    )

    parser.add_argument(
        "--api-key",
        default=os.getenv(
            "ETHERSCAN_API_KEY",
            "",
        ),
    )

    parser.add_argument(
        "--address",
        default=os.getenv(
            "ETHEREUM_ADDRESS",
            "",
        ),
    )

    parser.add_argument(
        "--chain-id",
        type=int,
        default=env_int(
            "ETHERSCAN_CHAIN_ID",
            1,
        ),
    )

    parser.add_argument(
        "--base-url",
        default=os.getenv(
            "ETHERSCAN_BASE_URL",
            "https://api.etherscan.io/v2/api",
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=env_int(
            "ETHERSCAN_WORKERS",
            3,
        ),
    )

    parser.add_argument(
        "--pagination-window",
        type=int,
        default=env_int(
            "ETHERSCAN_PAGINATION_WINDOW",
            0,
        ),
        help=(
            "Maximum ordered page look-ahead; "
            "0 means max_workers"
        ),
    )

    parser.add_argument(
        "--rate",
        type=float,
        default=env_float(
            "ETHERSCAN_RATE",
            3.0,
        ),
    )

    parser.add_argument(
        "--burst",
        type=float,
        default=env_float(
            "ETHERSCAN_BURST",
            1.0,
        ),
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=env_int(
            "ETHERSCAN_RETRIES",
            5,
        ),
    )

    parser.add_argument(
        "--backoff-factor",
        type=float,
        default=env_float(
            "ETHERSCAN_BACKOFF_FACTOR",
            0.5,
        ),
    )

    parser.add_argument(
        "--max-backoff",
        type=float,
        default=env_float(
            "ETHERSCAN_MAX_BACKOFF",
            30.0,
        ),
    )

    parser.add_argument(
        "--circuit-failures",
        type=int,
        default=env_int(
            "ETHERSCAN_CIRCUIT_FAILURES",
            8,
        ),
    )

    parser.add_argument(
        "--circuit-recovery",
        type=float,
        default=env_float(
            "ETHERSCAN_CIRCUIT_RECOVERY_SEC",
            30.0,
        ),
    )

    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=env_float(
            "ETHERSCAN_CONNECT_TIMEOUT",
            5.0,
        ),
    )

    parser.add_argument(
        "--read-timeout",
        type=float,
        default=env_float(
            "ETHERSCAN_READ_TIMEOUT",
            30.0,
        ),
    )

    parser.add_argument(
        "--page-size",
        type=int,
        default=env_int(
            "ETHERSCAN_PAGE_SIZE",
            1000,
        ),
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--serial",
        action="store_true",
        help="Disable parallel pagination",
    )

    parser.add_argument(
        "--start-block",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--end-block",
        type=int,
        default=99_999_999,
    )

    parser.add_argument(
        "--sort",
        choices=("asc", "desc"),
        default="asc",
    )

    parser.add_argument(
        "--action",
        choices=sorted(ACCOUNT_ACTIONS),
        default="txlist",
    )

    parser.add_argument(
        "--contract-address",
        help=(
            "Optional contract filter "
            "for token actions"
        ),
    )

    parser.add_argument(
        "--token-contract",
        default=os.getenv(
            "TOKEN_CONTRACT_ADDRESS"
        ),
    )

    parser.add_argument(
        "--token-decimals",
        type=int,
        default=env_int(
            "TOKEN_DECIMALS",
            18,
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        help="Write records as UTF-8 JSONL",
    )

    parser.add_argument(
        "--print-first",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--skip-balance",
        action="store_true",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print client statistics on exit",
    )

    parser.add_argument(
        "--log-level",
        default=os.getenv(
            "LOG_LEVEL",
            "INFO",
        ),
    )

    parser.add_argument(
        "--no-trust-env",
        action="store_true",
        default=not env_bool(
            "ETHERSCAN_TRUST_ENV",
            True,
        ),
        help=(
            "Ignore standard HTTP_PROXY/"
            "HTTPS_PROXY environment variables"
        ),
    )

    return parser


def configure_logging(
    level: str,
) -> None:
    numeric_level = getattr(
        logging,
        level.upper(),
        None,
    )

    if not isinstance(
        numeric_level,
        int,
    ):
        raise EtherscanValidationError(
            f"Invalid log level: {level!r}"
        )

    logging.basicConfig(
        level=numeric_level,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(threadName)s | "
            "%(message)s"
        ),
    )


def install_signal_handlers(
    client: EtherscanClient,
) -> None:
    def handle_signal(
        signum: int,
        _frame: Optional[FrameType],
    ) -> None:
        LOGGER.warning(
            "Received signal %s; stopping",
            signum,
        )

        client.stop()

    signal.signal(
        signal.SIGINT,
        handle_signal,
    )

    if hasattr(signal, "SIGTERM"):
        signal.signal(
            signal.SIGTERM,
            handle_signal,
        )


def open_output(
    path: Optional[Path],
) -> Optional[AtomicOutput]:
    if path is None:
        return None

    final_path = (
        path.expanduser()
        .resolve()
    )

    final_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_path = final_path.with_name(
        f".{final_path.name}."
        f"{os.getpid()}."
        f"{time.monotonic_ns()}.part"
    )

    stream = temp_path.open(
        "x",
        encoding="utf-8",
        newline="\n",
    )

    return AtomicOutput(
        final_path=final_path,
        temp_path=temp_path,
        stream=stream,
    )


def main(
    argv: Optional[Sequence[str]] = None,
) -> int:
    client: Optional[EtherscanClient] = None

    try:
        args = build_parser().parse_args(
            argv
        )

        configure_logging(
            args.log_level
        )

        if args.print_first < 0:
            raise EtherscanValidationError(
                "print-first must be >= 0"
            )

        if args.max_pages is not None and args.max_pages < 1:
            raise EtherscanValidationError(
                "max-pages must be >= 1"
            )

        if not 0 <= args.token_decimals <= 255:
            raise EtherscanValidationError(
                "token-decimals must be "
                "between 0 and 255"
            )

        if (
            args.contract_address
            and args.action not in TOKEN_ACTIONS
        ):
            raise EtherscanValidationError(
                "--contract-address can only be "
                "used with token actions"
            )

        config = EtherscanConfig(
            api_key=args.api_key,
            address=args.address,
            chain_id=args.chain_id,
            base_url=args.base_url,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            retries=args.retries,
            backoff_factor=args.backoff_factor,
            max_backoff=args.max_backoff,
            rate_limit_per_sec=args.rate,
            rate_limit_burst=args.burst,
            max_workers=args.workers,
            pagination_window=args.pagination_window,
            page_size=args.page_size,
            circuit_breaker_failures=(
                args.circuit_failures
            ),
            circuit_breaker_recovery_sec=(
                args.circuit_recovery
            ),
            proxies=build_proxy_mapping(),
            trust_env=not args.no_trust_env,
        )

        client = EtherscanClient(config)

        with client:
            install_signal_handlers(
                client
            )

            if not args.skip_balance:
                balance = client.get_balance()

                LOGGER.info(
                    "Native balance: %s",
                    balance,
                )

            output_handle = open_output(
                args.output
            )

            try:
                record_count = 0

                records = client.iter_account_records(
                    args.action,
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                    start_block=args.start_block,
                    end_block=args.end_block,
                    sort=args.sort,
                    parallel=not args.serial,
                    contract_address=(
                        args.contract_address
                    ),
                )

                for record_count, record in enumerate(
                    records,
                    start=1,
                ):
                    if output_handle is not None:
                        output_handle.stream.write(
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                separators=(
                                    ",",
                                    ":",
                                ),
                            )
                        )
                        output_handle.stream.write(
                            "\n"
                        )

                    if (
                        record_count
                        <= args.print_first
                    ):
                        print(
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                indent=2,
                            )
                        )

                if output_handle is not None:
                    output_handle.commit()

            except BaseException:
                if output_handle is not None:
                    output_handle.abort()

                raise

            LOGGER.info(
                "Records fetched: %d",
                record_count,
            )

            if args.output:
                LOGGER.info(
                    "JSONL saved to: %s",
                    args.output.expanduser().resolve(),
                )

            if args.token_contract:
                token_balance = (
                    client.get_token_balance(
                        args.token_contract,
                        decimals=args.token_decimals,
                    )
                )

                LOGGER.info(
                    "Token balance: %s",
                    token_balance,
                )

            if args.stats:
                LOGGER.info(
                    "Statistics: %s",
                    json.dumps(
                        client.stats(),
                        sort_keys=True,
                    ),
                )

        return 0

    except KeyboardInterrupt:
        LOGGER.warning("Interrupted")
        return 130

    except EtherscanValidationError as exc:
        LOGGER.error(
            "Configuration error: %s",
            exc,
        )
        return 2

    except EtherscanCircuitOpenError as exc:
        LOGGER.error(
            "Circuit breaker: %s",
            exc,
        )
        return 1

    except EtherscanError as exc:
        LOGGER.error(
            "%s",
            exc,
        )
        return 1

    except (
        OSError,
        ValueError,
    ) as exc:
        LOGGER.error(
            "Startup/output error: %s",
            exc,
        )
        return 2

    except Exception:
        LOGGER.exception(
            "Unexpected error"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
