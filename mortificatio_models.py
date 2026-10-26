"""Phase 1 shared models. All application entry commands remain OFFLINE ONLY."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import re
import uuid


class ApplicationError(RuntimeError):
    def __init__(self,code,status=403):
        super().__init__(code)
        self.code,self.status=code,status


@dataclass(frozen=True)
class AccountBinding:
    account: str
    environment: str
    risk_version: str

    def validate(self):
        if (not isinstance(self.account,str) or not re.fullmatch(r'DU[0-9]{5,}',self.account)
                or self.environment!='PAPER' or not isinstance(self.risk_version,str)
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',self.risk_version)):
            raise ApplicationError('PERMITTED_PAPER_BINDING_REQUIRED')


@dataclass(frozen=True)
class ApplicationPolicy:
    binding: AccountBinding
    max_trades_per_day: int = 2

    def validate(self):
        self.binding.validate()
        if type(self.max_trades_per_day) is not int or not 1<=self.max_trades_per_day<=2:
            raise ApplicationError('FREQUENCY_POLICY_MUST_NOT_WEAKEN_EXISTING_LIMIT')


@dataclass(frozen=True)
class TradeCommand:
    command_id: str
    direction: str
    entry_epoch: int

    def validate(self):
        try:
            parsed=uuid.UUID(self.command_id)
            valid=str(parsed)==self.command_id and parsed.version==4
        except (TypeError,ValueError,AttributeError): valid=False
        if not valid or self.direction not in ('CALL','PUT') or type(self.entry_epoch) is not int or self.entry_epoch<0:
            raise ApplicationError('INVALID_DIRECTION_COMMAND',400)


@dataclass(frozen=True)
class BrokerAdmissionEvidence:
    """Trusted server-side mock-provider output, never browser-supplied evidence.

    This is an OFFLINE admission interface, not market-data source proof or a
    replacement for the existing broker/0.25-second market validators.
    """
    binding: AccountBinding
    generation: int
    connected: bool
    account_verified: bool
    risk_passed: bool
    market_passed: bool
    buying_power_passed: bool
    observed_at: datetime
    expires_at: datetime
    evidence_ref: str
    origin: str = 'OFFLINE_FIXTURE'


@dataclass(frozen=True)
class ManagedTradeEvidence:
    trade_id: str
    binding: AccountBinding
    generation: int
    revision: int
    evidence_id: str
    entry_filled: bool
    first_fill_at: Optional[datetime]
    position_quantity: int
    outstanding_orders: int
    reconciliation_complete: bool
    execution_history_complete: bool
    commissions_complete: bool
    origin: str = 'OFFLINE_FIXTURE'
