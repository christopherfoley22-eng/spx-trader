"""Separately authorized read-only order/execution observations. No auto-connect."""
from ibapi.execution import ExecutionFilter
from ibkr_phase11_read_only import RealityProbe
from ibkr_paper_read_only_validation import run_validation
from executor_broker_lifecycle_capture import BrokerLifecycleValidation
from simulation_evidence import EvidenceError


class LifecycleReadOnlyProbe(RealityProbe):
    def __init__(self,validation,history_authorized=False):
        super().__init__(validation);self.history_authorized=history_authorized is True;self.history_started=False
    def _history_if_verified(self):
        if self.history_authorized and not self.history_started and self.validation.verified and self.validation.started:
            self.request_execution_history(31000,authorized=True)
    def nextValidId(self,orderId):
        super().nextValidId(orderId);self._history_if_verified()
    def managedAccounts(self,accountsList):
        super().managedAccounts(accountsList);self._history_if_verified()
    def orderStatus(self,*args):self._feed('order_status',*args)
    def execDetails(self,reqId,contract,execution):self._feed('execution_details',reqId,contract,execution)
    def execDetailsEnd(self,reqId):self._feed('execution_end',reqId)
    def commissionReport(self,report):self._feed('commission_report',report)
    def request_execution_history(self,request_id,*,authorized=False):
        self.validation.gate()
        if (authorized is not True or not self.validation.started or self.history_started or
            type(request_id) is not int or not 30000<=request_id<=2**31-1):raise EvidenceError('EXPLICIT_ONE_SHOT_READ_ONLY_HISTORY_AUTHORIZATION_REQUIRED')
        self.history_started=True;self.validation.history_requests.add(request_id)
        filt=ExecutionFilter();filt.acctCode=self.validation.approval.account
        self.reqExecutions(request_id,filt)  # Observation only, never an order API.


def run_lifecycle_read_only(validation,*,authorized=False,include_execution_history=False,**kwargs):
    if not isinstance(validation,BrokerLifecycleValidation) or type(include_execution_history) is not bool:
        raise EvidenceError('EXPLICIT_LIFECYCLE_CAPTURE_REQUIRED')
    return run_validation(validation,authorized=authorized,
        probe_factory=lambda v:LifecycleReadOnlyProbe(v,include_execution_history),**kwargs)
