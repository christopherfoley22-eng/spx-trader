"""Synthetic evidence gates for the read-only Executor simulation."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math
from typing import Callable, Optional, Tuple
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MAX_AGE_SECONDS = 1.0
MAX_FUTURE_SECONDS = 0.25
MAX_SYNC_SECONDS = 0.25


class EvidenceError(RuntimeError):
    pass


class FeedStatus(Enum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    FROZEN = "FROZEN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class VerifiedOptionContract:
    symbol: str
    sec_type: str
    right: str
    expiration: str
    trading_class: str
    multiplier: str
    currency: str
    strike: float
    con_id: int


@dataclass(frozen=True)
class SPXObservation:
    price: float
    status: FeedStatus
    source_time: datetime
    received_monotonic: float


@dataclass(frozen=True)
class OptionObservation:
    contract: VerifiedOptionContract
    quote_con_id: int
    bid: float
    ask: float
    status: FeedStatus
    source_time: datetime
    received_monotonic: float


@dataclass(frozen=True)
class VerifiedBrokerSnapshot:
    account_id: str
    selected_account: str
    managed_accounts: Tuple[str, ...]
    complete: bool
    position_qty: int
    con_id: Optional[int]
    open_order_count: int
    received_monotonic: float


def positive_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def validate_contract(contract, direction, session_time: datetime,
                      session_has_expiration: Callable[[datetime], bool]):
    if not isinstance(contract, VerifiedOptionContract):
        raise EvidenceError("Contract metadata missing")
    if contract.symbol != "SPX" or contract.sec_type != "OPT":
        raise EvidenceError("Wrong underlying or security type")
    if direction not in {"CALL", "PUT"} or contract.right != {"CALL": "C", "PUT": "P"}[direction]:
        raise EvidenceError("Option right disagrees with direction")
    if contract.trading_class != "SPXW" or contract.multiplier != "100" or contract.currency != "USD":
        raise EvidenceError("Wrong trading class, multiplier, or currency")
    if not positive_number(contract.strike) or not isinstance(contract.con_id, int) or isinstance(contract.con_id, bool) or contract.con_id <= 0:
        raise EvidenceError("Invalid strike or conId")
    if not isinstance(session_time, datetime) or session_time.tzinfo is None:
        raise EvidenceError("Trading session time missing")
    session_time = session_time.astimezone(NY)
    trading_date = session_time.date()
    if contract.expiration != trading_date.strftime("%Y%m%d"):
        raise EvidenceError("Contract is not intended 0DTE expiration")
    if session_has_expiration(session_time) is not True:
        raise EvidenceError("No proven SPXW expiration for session")


def validate_observation_time(source_time, received_monotonic, now_wall,
                              now_monotonic):
    if not isinstance(source_time, datetime) or source_time.tzinfo is None:
        raise EvidenceError("Source timestamp missing timezone")
    if not isinstance(now_wall, datetime) or now_wall.tzinfo is None:
        raise EvidenceError("Clock timestamp missing timezone")
    if not isinstance(received_monotonic, (int, float)) or not math.isfinite(received_monotonic):
        raise EvidenceError("Monotonic receipt timestamp invalid")
    age = now_monotonic - received_monotonic
    if not math.isfinite(age) or age < 0 or age > MAX_AGE_SECONDS:
        raise EvidenceError("Observation stale or monotonic time invalid")
    if (source_time - now_wall).total_seconds() > MAX_FUTURE_SECONDS:
        raise EvidenceError("Source timestamp too far in future")
    if (now_wall - source_time).total_seconds() > MAX_AGE_SECONDS:
        raise EvidenceError("Source timestamp stale")


class EvidenceClock:
    """Tracks source ordering; failed observations do not advance the watermark."""

    def __init__(self):
        self.last_source = {}

    def validate_spx(self, spx, now_wall, now_monotonic):
        if not isinstance(spx, SPXObservation) or spx.status is not FeedStatus.LIVE:
            raise EvidenceError("Live SPX observation missing")
        if not positive_number(spx.price):
            raise EvidenceError("Invalid SPX price")
        validate_observation_time(spx.source_time, spx.received_monotonic,
                                  now_wall, now_monotonic)
        previous = self.last_source.get("SPX")
        if previous is not None and spx.source_time < previous:
            raise EvidenceError("SPX source timestamp moved backward")
        self.last_source["SPX"] = spx.source_time

    def validate_pair(self, direction, spx, option, now_wall, now_monotonic,
                      session_has_expiration):
        if not isinstance(spx, SPXObservation) or not isinstance(option, OptionObservation):
            raise EvidenceError("Market observations missing")
        if spx.status is not FeedStatus.LIVE or option.status is not FeedStatus.LIVE:
            raise EvidenceError("Live market data not proven")
        if not positive_number(spx.price) or not positive_number(option.bid) or not positive_number(option.ask):
            raise EvidenceError("Invalid market price")
        if option.ask < option.bid:
            raise EvidenceError("Crossed option market")
        validate_observation_time(spx.source_time, spx.received_monotonic, now_wall, now_monotonic)
        validate_observation_time(option.source_time, option.received_monotonic, now_wall, now_monotonic)
        if abs((spx.source_time - option.source_time).total_seconds()) > MAX_SYNC_SECONDS:
            raise EvidenceError("Underlying and option quote are not synchronized")
        if abs(spx.received_monotonic - option.received_monotonic) > MAX_SYNC_SECONDS:
            raise EvidenceError("Underlying and option receipt times are not synchronized")
        validate_contract(option.contract, direction, now_wall, session_has_expiration)
        if option.quote_con_id != option.contract.con_id:
            raise EvidenceError("Quote belongs to a different conId")
        pending = {"SPX": spx.source_time, ("OPTION", option.contract.con_id): option.source_time}
        for key, timestamp in pending.items():
            previous = self.last_source.get(key)
            if previous is not None and timestamp < previous:
                raise EvidenceError("Market source timestamp moved backward")
        self.last_source.update(pending)


def validate_broker_snapshot(snapshot, selected_account, now_monotonic):
    if not isinstance(snapshot, VerifiedBrokerSnapshot) or snapshot.complete is not True:
        raise EvidenceError("Broker snapshot incomplete")
    if (not isinstance(selected_account, str) or not selected_account.strip()
            or snapshot.selected_account != selected_account):
        raise EvidenceError("Selected account missing or changed")
    if (not isinstance(snapshot.managed_accounts, tuple)
            or not snapshot.managed_accounts
            or any(not isinstance(account, str) or not account.strip()
                   for account in snapshot.managed_accounts)):
        raise EvidenceError("Managed-account list malformed")
    if (not snapshot.account_id or snapshot.account_id != selected_account
            or selected_account not in snapshot.managed_accounts
            or len(set(snapshot.managed_accounts)) != len(snapshot.managed_accounts)):
        raise EvidenceError("Broker snapshot account ambiguous or wrong")
    age = now_monotonic - snapshot.received_monotonic
    if not math.isfinite(age) or age < 0 or age > MAX_AGE_SECONDS:
        raise EvidenceError("Broker snapshot stale")
    if not isinstance(snapshot.position_qty, int) or isinstance(snapshot.position_qty, bool) or not 0 <= snapshot.position_qty <= 25:
        raise EvidenceError("Broker quantity invalid")
    if not isinstance(snapshot.open_order_count, int) or isinstance(snapshot.open_order_count, bool) or snapshot.open_order_count < 0:
        raise EvidenceError("Broker order state invalid")
    if snapshot.position_qty == 0 and snapshot.con_id is not None:
        raise EvidenceError("Flat broker snapshot carries conId")
    if snapshot.position_qty > 0 and (not isinstance(snapshot.con_id, int) or isinstance(snapshot.con_id, bool) or snapshot.con_id <= 0):
        raise EvidenceError("Broker conId invalid")
