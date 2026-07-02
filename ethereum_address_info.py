#!/usr/bin/env python3
"""
Reliable multichain Etherscan API V2 client.

Highlights
----------
- Etherscan API V2 with configurable chain ID
- Global thread-safe token-bucket rate limiter
- Thread-local HTTP sessions and connection pooling
- One explicit retry layer with Retry-After support
- Exponential backoff with jitter
- Circuit breaker for repeated transient failures
- Bounded ordered parallel pagination (no 1000-page request storm)
- Serial streaming mode
- Native and ERC-20 balance helpers
- Environment variables plus CLI overrides
- Graceful shutdown and strict configuration validation
"""

from __future__ import annotations

import argparse
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
from types import FrameType
from typing import Any, Iterator, Mapping, Optional, Sequence, TypedDict, cast

import requests
from dotenv import load_dotenv
from requests import Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import JSONDecodeError, RequestException

load_dotenv()
getcontext().prec = 80

LOGGER = logging.getLogger("etherscan")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_API_MARKERS = (
    "rate limit",
    "max rate limit",
    "maximum rate limit",
    "server too busy",
    "query timeout",
    "timeout occurred",
    "temporarily unavailable",
)


class EtherscanError(RuntimeError):
    """Base client error."""


class EtherscanValidationError(EtherscanError):
    """Invalid client configuration or method argument."""


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


class TokenBucketRateLimiter:
    """Thread-safe token bucket shared by every worker."""

    def __init__(self, rate: float, capacity: Optional[float] = None) -> None:
        if rate <= 0:
            raise EtherscanValidationError("rate_limit_per_sec must be > 0")

        self._rate = float(rate)
        self._capacity = float(capacity if capacity is not None else max(1.0, rate))
        if self._capacity < 1:
            raise EtherscanValidationError("rate_limit_burst must be >= 1")

        self._tokens = self._capacity
        self._updated_at = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._updated_at = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                delay = (1.0 - self._tokens) / self._rate

            stop_event.wait(delay)

        raise KeyboardInterrupt


class CircuitBreaker:
    """Small thread-safe circuit breaker for transient upstream failures."""

    def __init__(self, failure_threshold: int, recovery_timeout: float) -> None:
        if failure_threshold < 1:
            raise EtherscanValidationError("circuit_breaker_failures must be >= 1")
        if recovery_timeout <= 0:
            raise EtherscanValidationError("circuit_breaker_recovery_sec must be > 0")

        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._half_open_probe = False
        self._lock = threading.Lock()

    def before_request(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return

            elapsed = time.monotonic() - self._opened_at
            if elapsed < self._recovery_timeout:
                remaining = self._recovery_timeout - elapsed
                raise EtherscanCircuitOpenError(
                    f"Circuit breaker is open; retry in {remaining:.1f}s"
                )

            if self._half_open_probe:
                raise EtherscanCircuitOpenError("Circuit breaker half-open probe in progress")

            self._half_open_probe = True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open_probe = False

    def record_failure(self) -> None:
        with self._lock:
            self._half_open_probe = False
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = time.monotonic()


@dataclass(frozen=True, slots=True)
class EtherscanConfig:
    api_key: str
    address: str
    chain_id: int = 1
    base_url: str = "https://api.etherscan.io/v2/api"
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    retries: int = 5
    backoff_factor: float = 0.5
    max_backoff: float = 20.0
    rate_limit_per_sec: float = 3.0
    rate_limit_burst: float = 1.0
    max_workers: int = 3
    page_size: int = 1000
    circuit_breaker_failures: int = 8
    circuit_breaker_recovery_sec: float = 30.0
    user_agent: str = "etherscan-v2-client/1.0"
    proxies: Optional[Mapping[str, str]] = None

    def validate(self) -> None:
        if not self.api_key.strip():
            raise EtherscanValidationError("ETHERSCAN_API_KEY is missing")
        validate_address(self.address, field="address")
        if self.chain_id <= 0:
            raise EtherscanValidationError("chain_id must be > 0")
        if not self.base_url.startswith(("https://", "http://")):
            raise EtherscanValidationError("base_url must be an HTTP(S) URL")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise EtherscanValidationError("timeouts must be > 0")
        if self.retries < 1:
            raise EtherscanValidationError("retries must be >= 1")
        if self.backoff_factor < 0 or self.max_backoff <= 0:
            raise EtherscanValidationError("invalid backoff settings")
        if self.max_workers < 1:
            raise EtherscanValidationError("max_workers must be >= 1")
        if not 1 <= self.page_size <= 1000:
            raise EtherscanValidationError("page_size must be between 1 and 1000")

    @property
    def normalized_address(self) -> str:
        return self.address.strip().lower()


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
        self._closed = False

    def __enter__(self) -> "EtherscanClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def stop(self) -> None:
        self._stop_event.set()

    def _session(self) -> Session:
        if self._closed:
            raise EtherscanError("Client is closed")

        session = getattr(self._thread_local, "session", None)
        if session is not None:
            return cast(Session, session)

        session = requests.Session()
        adapter = HTTPAdapter(
            max_retries=0,  # Retries are handled only in _request().
            pool_connections=self.config.max_workers,
            pool_maxsize=self.config.max_workers,
            pool_block=True,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": self.config.user_agent,
            }
        )
        if self.config.proxies:
            session.proxies.update(dict(self.config.proxies))

        self._thread_local.session = session
        with self._sessions_lock:
            self._sessions.append(session)
        return session

    def _request(self, params: Mapping[str, Any]) -> Any:
        if self._closed:
            raise EtherscanError("Client is closed")

        query = {
            "apikey": self.config.api_key,
            "chainid": str(self.config.chain_id),
            **{key: str(value) for key, value in params.items() if value is not None},
        }
        last_error: Optional[BaseException] = None

        for attempt in range(self.config.retries):
            if self._stop_event.is_set():
                raise KeyboardInterrupt

            self._circuit_breaker.before_request()
            self._rate_limiter.acquire(self._stop_event)

            try:
                response = self._session().get(
                    self.config.base_url,
                    params=query,
                    timeout=(self.config.connect_timeout, self.config.read_timeout),
                )
                if response.status_code in TRANSIENT_HTTP_STATUSES:
                    delay = self._retry_delay(attempt, response)
                    last_error = EtherscanRequestError(
                        f"Transient HTTP {response.status_code}: {response.text[:200]!r}"
                    )
                    self._circuit_breaker.record_failure()
                    self._log_retry(query, attempt, delay, last_error)
                    self._wait(delay)
                    continue

                response.raise_for_status()
                payload = self._decode_response(response)
                retryable, result = self._parse_payload(payload)
                if retryable:
                    delay = self._retry_delay(attempt, response)
                    last_error = EtherscanRequestError(
                        f"Retryable Etherscan response: {payload.get('result')!r}"
                    )
                    self._circuit_breaker.record_failure()
                    self._log_retry(query, attempt, delay, last_error)
                    self._wait(delay)
                    continue

                self._circuit_breaker.record_success()
                return result

            except EtherscanResponseError:
                self._circuit_breaker.record_success()
                raise
            except EtherscanCircuitOpenError:
                raise
            except (RequestException, JSONDecodeError) as exc:
                last_error = exc
                self._circuit_breaker.record_failure()
                delay = self._retry_delay(attempt)
                self._log_retry(query, attempt, delay, exc)
                self._wait(delay)

        raise EtherscanRequestError(
            f"Request failed after {self.config.retries} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _decode_response(response: Response) -> EtherscanResponse:
        payload = response.json()
        if not isinstance(payload, dict):
            raise EtherscanResponseError(
                f"Expected JSON object, got {type(payload).__name__}"
            )
        return cast(EtherscanResponse, payload)

    @staticmethod
    def _parse_payload(payload: EtherscanResponse) -> tuple[bool, Any]:
        status = str(payload.get("status", ""))
        message = str(payload.get("message", ""))
        result = payload.get("result")

        if status == "1":
            return False, result

        combined = f"{message} {result}".lower()
        if any(marker in combined for marker in RETRYABLE_API_MARKERS):
            return True, None

        if isinstance(result, str) and result.strip().lower() == "no transactions found":
            return False, []

        raise EtherscanResponseError(
            f"Etherscan error: status={status!r}, message={message!r}, result={result!r}"
        )

    def _retry_delay(self, attempt: int, response: Optional[Response] = None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(max(float(retry_after), 0.0), self.config.max_backoff)
                except ValueError:
                    pass

        exponential = self.config.backoff_factor * (2**attempt)
        jitter = random.uniform(0.0, min(1.0, max(exponential, 0.1)))
        return min(exponential + jitter, self.config.max_backoff)

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
            "Request %s/%s failed for %s/%s: %s; retrying in %.2fs",
            attempt + 1,
            self.config.retries,
            query.get("module"),
            query.get("action"),
            error,
            delay,
        )

    @staticmethod
    def units_to_decimal(value: str | int, decimals: int = 18) -> Decimal:
        if not 0 <= decimals <= 255:
            raise EtherscanValidationError("decimals must be between 0 and 255")
        try:
            return Decimal(str(value)) / (Decimal(10) ** decimals)
        except (InvalidOperation, ValueError) as exc:
            raise EtherscanResponseError(f"Invalid numeric result: {value!r}") from exc

    def get_balance(self, address: Optional[str] = None) -> Decimal:
        target = normalize_address(address or self.config.address)
        value = self._request(
            {
                "module": "account",
                "action": "balance",
                "address": target,
                "tag": "latest",
            }
        )
        return self.units_to_decimal(value)

    def get_token_balance(
        self,
        contract_address: str,
        decimals: int = 18,
        address: Optional[str] = None,
    ) -> Decimal:
        contract = normalize_address(contract_address, field="contract_address")
        target = normalize_address(address or self.config.address)
        value = self._request(
            {
                "module": "account",
                "action": "tokenbalance",
                "contractaddress": contract,
                "address": target,
                "tag": "latest",
            }
        )
        return self.units_to_decimal(value, decimals)

    def get_transactions_page(
        self,
        page: int,
        *,
        offset: Optional[int] = None,
        start_block: int = 0,
        end_block: int = 99_999_999,
        sort: str = "asc",
        address: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        page_size = offset if offset is not None else self.config.page_size
        validate_page_arguments(page, page_size, start_block, end_block, sort)
        target = normalize_address(address or self.config.address)

        result = self._request(
            {
                "module": "account",
                "action": "txlist",
                "address": target,
                "startblock": start_block,
                "endblock": end_block,
                "page": page,
                "offset": page_size,
                "sort": sort,
            }
        )
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise EtherscanResponseError("txlist result is not a list of objects")
        return cast(list[dict[str, Any]], result)

    def iter_transactions(
        self,
        *,
        page_size: Optional[int] = None,
        max_pages: Optional[int] = None,
        start_block: int = 0,
        end_block: int = 99_999_999,
        sort: str = "asc",
        parallel: bool = True,
        address: Optional[str] = None,
    ) -> Iterator[dict[str, Any]]:
        size = page_size if page_size is not None else self.config.page_size
        validate_page_arguments(1, size, start_block, end_block, sort)
        if max_pages is not None and max_pages < 1:
            raise EtherscanValidationError("max_pages must be >= 1 or None")

        kwargs = {
            "page_size": size,
            "max_pages": max_pages,
            "start_block": start_block,
            "end_block": end_block,
            "sort": sort,
            "address": address,
        }
        if parallel and self.config.max_workers > 1:
            yield from self._iter_transactions_parallel(**kwargs)
        else:
            yield from self._iter_transactions_serial(**kwargs)

    def _iter_transactions_serial(
        self,
        *,
        page_size: int,
        max_pages: Optional[int],
        start_block: int,
        end_block: int,
        sort: str,
        address: Optional[str],
    ) -> Iterator[dict[str, Any]]:
        page = 1
        while max_pages is None or page <= max_pages:
            transactions = self.get_transactions_page(
                page,
                offset=page_size,
                start_block=start_block,
                end_block=end_block,
                sort=sort,
                address=address,
            )
            LOGGER.info("Fetched page %d (%d transactions)", page, len(transactions))
            yield from transactions
            if len(transactions) < page_size:
                return
            page += 1

    def _iter_transactions_parallel(
        self,
        *,
        page_size: int,
        max_pages: Optional[int],
        start_block: int,
        end_block: int,
        sort: str,
        address: Optional[str],
    ) -> Iterator[dict[str, Any]]:
        """
        Fetch a bounded window concurrently, but emit pages in exact page order.

        At most max_workers pages are in flight. Once a short page is observed,
        no later pages are emitted and pending later work is cancelled where possible.
        """
        next_submit = 1
        next_emit = 1
        terminal_page: Optional[int] = None
        completed: dict[int, list[dict[str, Any]]] = {}
        in_flight: dict[Future[list[dict[str, Any]]], int] = {}

        def can_submit(page: int) -> bool:
            if max_pages is not None and page > max_pages:
                return False
            if terminal_page is not None and page > terminal_page:
                return False
            return not self._stop_event.is_set()

        with ThreadPoolExecutor(
            max_workers=self.config.max_workers,
            thread_name_prefix="etherscan",
        ) as executor:
            while len(in_flight) < self.config.max_workers and can_submit(next_submit):
                future = executor.submit(
                    self.get_transactions_page,
                    next_submit,
                    offset=page_size,
                    start_block=start_block,
                    end_block=end_block,
                    sort=sort,
                    address=address,
                )
                in_flight[future] = next_submit
                next_submit += 1

            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    page = in_flight.pop(future)
                    transactions = future.result()
                    completed[page] = transactions
                    LOGGER.info("Fetched page %d (%d transactions)", page, len(transactions))
                    if len(transactions) < page_size:
                        terminal_page = page if terminal_page is None else min(terminal_page, page)

                while next_emit in completed:
                    transactions = completed.pop(next_emit)
                    yield from transactions
                    if len(transactions) < page_size:
                        for future, page in list(in_flight.items()):
                            if page > next_emit:
                                future.cancel()
                        return
                    next_emit += 1

                while len(in_flight) < self.config.max_workers and can_submit(next_submit):
                    future = executor.submit(
                        self.get_transactions_page,
                        next_submit,
                        offset=page_size,
                        start_block=start_block,
                        end_block=end_block,
                        sort=sort,
                        address=address,
                    )
                    in_flight[future] = next_submit
                    next_submit += 1


def validate_address(value: str, *, field: str = "address") -> None:
    if not ADDRESS_RE.fullmatch(value.strip()):
        raise EtherscanValidationError(f"Invalid {field}: {value!r}")


def normalize_address(value: str, *, field: str = "address") -> str:
    validate_address(value, field=field)
    return value.strip().lower()


def validate_page_arguments(
    page: int,
    offset: int,
    start_block: int,
    end_block: int,
    sort: str,
) -> None:
    if page < 1:
        raise EtherscanValidationError("page must be >= 1")
    if not 1 <= offset <= 1000:
        raise EtherscanValidationError("offset/page_size must be between 1 and 1000")
    if start_block < 0 or end_block < start_block:
        raise EtherscanValidationError("invalid block range")
    if sort not in {"asc", "desc"}:
        raise EtherscanValidationError("sort must be 'asc' or 'desc'")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, "") else int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, "") else float(raw)


def build_proxy_mapping() -> Optional[dict[str, str]]:
    http_proxy = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or http_proxy
    if not http_proxy and not https_proxy:
        return None
    proxies: dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    return proxies


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Etherscan API V2 client")
    parser.add_argument("--api-key", default=os.getenv("ETHERSCAN_API_KEY", ""))
    parser.add_argument("--address", default=os.getenv("ETHEREUM_ADDRESS", ""))
    parser.add_argument("--chain-id", type=int, default=env_int("ETHERSCAN_CHAIN_ID", 1))
    parser.add_argument("--workers", type=int, default=env_int("ETHERSCAN_WORKERS", 3))
    parser.add_argument("--rate", type=float, default=env_float("ETHERSCAN_RATE", 3.0))
    parser.add_argument("--page-size", type=int, default=env_int("ETHERSCAN_PAGE_SIZE", 1000))
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--serial", action="store_true", help="Disable parallel pagination")
    parser.add_argument("--start-block", type=int, default=0)
    parser.add_argument("--end-block", type=int, default=99_999_999)
    parser.add_argument("--token-contract", default=os.getenv("TOKEN_CONTRACT_ADDRESS"))
    parser.add_argument("--token-decimals", type=int, default=env_int("TOKEN_DECIMALS", 18))
    parser.add_argument("--print-first", type=int, default=0)
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    return parser


def configure_logging(level: str) -> None:
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise EtherscanValidationError(f"Invalid log level: {level!r}")
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
    )


def install_signal_handlers(client: EtherscanClient) -> None:
    def handle_signal(signum: int, _frame: Optional[FrameType]) -> None:
        LOGGER.warning("Received signal %s; stopping", signum)
        client.stop()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    config = EtherscanConfig(
        api_key=args.api_key,
        address=args.address,
        chain_id=args.chain_id,
        max_workers=args.workers,
        rate_limit_per_sec=args.rate,
        page_size=args.page_size,
        proxies=build_proxy_mapping(),
    )

    try:
        with EtherscanClient(config) as client:
            install_signal_handlers(client)
            LOGGER.info("Native balance: %s", client.get_balance())

            transaction_count = 0
            for transaction_count, transaction in enumerate(
                client.iter_transactions(
                    page_size=args.page_size,
                    max_pages=args.max_pages,
                    start_block=args.start_block,
                    end_block=args.end_block,
                    parallel=not args.serial,
                ),
                start=1,
            ):
                if transaction_count <= args.print_first:
                    print(transaction)

            LOGGER.info("Transactions fetched: %d", transaction_count)

            if args.token_contract:
                balance = client.get_token_balance(
                    args.token_contract,
                    decimals=args.token_decimals,
                )
                LOGGER.info("Token balance: %s", balance)

        return 0
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted")
        return 130
    except EtherscanError as exc:
        LOGGER.error("%s", exc)
        return 1
    except ValueError as exc:
        LOGGER.error("Invalid numeric configuration: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
