"""Opt-in read-only transport. No CLI/autoconnect; call only with fresh approval.

Existing run_validation(authorized=True) requires separate user authorization.
Importing this module opens no socket. All order mutation overrides are inherited.
"""
from ibkr_paper_read_only_validation import PaperReadOnlyProbe,run_validation
from executor_broker_reality import BrokerRealityValidation
from simulation_evidence import EvidenceError


class RealityProbe(PaperReadOnlyProbe):
    def tickSize(self,reqId,tickType,size):self._feed('sizes',reqId,tickType,size)
    def openOrder(self,orderId,contract,order,orderState):self._feed('detailed_order',orderId,contract,order,orderState)
    def mktDepthExchanges(self,descriptions):self._feed('depth_capability',descriptions)
    def updateMktDepth(self,reqId,position,operation,side,price,size):self._feed('depth',reqId,position,operation,side,price,size)
    def updateMktDepthL2(self,reqId,position,marketMaker,operation,side,price,size,isSmartDepth=False):
        self._feed('depth',reqId,position,operation,side,price,size)

    def inspect_depth(self,request_id,contract,*,authorized=False,rows=5):
        """Separate opt-in after qualification; unavailable depth stays unavailable."""
        with self.validation.lock:
            self.validation.gate()
            if authorized is not True or not self.validation.started or type(rows) is not int or not 1<=rows<=5:
                raise EvidenceError('EXPLICIT_READ_ONLY_DEPTH_AUTHORIZATION_REQUIRED')
            self.validation._known(request_id)
            exact=[c for c,_ in self.validation.adapter.exact.values() if c.con_id==contract.conId]
            if len(exact)!=1:raise EvidenceError('QUALIFIED_EXACT_DEPTH_CONTRACT_REQUIRED')
            c=exact[0]
            if (contract.symbol,contract.secType,contract.right,contract.lastTradeDateOrContractMonth,
                contract.tradingClass,contract.multiplier,contract.currency,contract.strike)!=(
                c.symbol,c.sec_type,c.right,c.expiration,c.trading_class,c.multiplier,c.currency,c.strike):
                raise EvidenceError('EXACT_DEPTH_CONTRACT_IDENTITY_REQUIRED')
            request=self.validation.streams['CALL'].requests.get(request_id) or self.validation.streams['PUT'].requests.get(request_id)
            if request['contract']!=exact[0] or contract.exchange not in ('SMART','CBOE'):
                raise EvidenceError('EXACT_DEPTH_REQUEST_MISMATCH')
            self.reqMktDepthExchanges()
            self.reqMktDepth(request_id,contract,rows,contract.exchange=='SMART',[])


def run_read_only(validation,*,authorized=False,**kwargs):
    if not isinstance(validation,BrokerRealityValidation):raise EvidenceError('PHASE11_READ_ONLY_VALIDATION_REQUIRED')
    return run_validation(validation,authorized=authorized,probe_factory=RealityProbe,**kwargs)
