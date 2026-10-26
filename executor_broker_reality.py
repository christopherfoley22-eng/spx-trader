"""Read-only broker reality diagnostics. Never an execution/session permit.

Reuses account allowlisting, read-only GUI attestation and one-shot lifecycle.
Callback receipt proves observation only, not authoritative quote freshness.
"""
from copy import deepcopy
from decimal import Decimal,InvalidOperation
from executor_ibkr_read_only import ReadOnlyPaperValidation,SPX_QUOTE
from simulation_evidence import EvidenceError


class BrokerRealityValidation(ReadOnlyPaperValidation):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.size_fields={};self.book_fields={};self.order_details={};self.depth_capabilities=[]

    def _known(self,request_id):
        self.gate()
        if request_id==SPX_QUOTE and self.adapter.underlying_done:return
        if not any(request_id in s.requests for s in self.streams.values()):
            raise EvidenceError('UNKNOWN_MARKET_REQUEST')

    def sizes(self,request_id,tick_type,size):
        if tick_type not in (0,3,69,70):return
        self._known(request_id)
        try:value=Decimal(str(size))
        except InvalidOperation:raise EvidenceError('INVALID_SIZE')
        if not value.is_finite() or value<0:raise EvidenceError('INVALID_SIZE')
        self.size_fields[(request_id,tick_type)]=dict(request_id=request_id,tick_type=tick_type,size=str(value),
            local_receipt_utc=self.wall().isoformat(),local_receipt_monotonic=self.mono(),source_timestamp=None,
            source_age_lower_bound_seconds=0,source_age_upper_bound_seconds=None,
            quality='DELAYED' if tick_type in (69,70) else 'UNVERIFIED_SOURCE_TIME',authorizes_entry=False)

    def depth_capability(self,descriptions):
        self.gate()
        self.depth_capabilities=[dict(exchange=x.exchange,sec_type=x.secType,listing_exchange=x.listingExch,
            service_type=x.serviceDataType,aggregation_group=x.aggGroup) for x in descriptions]

    def depth(self,request_id,position,operation,side,price,size):
        self._known(request_id)
        if type(position) is not int or not 0<=position<100 or operation not in (0,1,2) or side not in (0,1):
            raise EvidenceError('INVALID_DEPTH_OPERATION')
        # Retain events rather than claiming a complete executable book. Missing
        # initial snapshots, resets and sequence gaps require another validation.
        row=dict(request_id=request_id,position=position,operation=operation,side=side,
            price=str(price),size=str(size),source_timestamp=None,local_receipt_utc=self.wall().isoformat(),
            local_receipt_monotonic=self.mono(),quality='INCOMPLETE_UNTIMESTAMPED_DEPTH',authorizes_entry=False)
        self.book_fields.setdefault(request_id,[]).append(row)
        self.book_fields[request_id]=self.book_fields[request_id][-256:]

    def detailed_order(self,order_id,contract,order,state):
        self.open_order(order_id,contract,order)
        # These are observations of potentially unmanaged orders. Never bind,
        # change ownership, infer coverage or release local reservations here.
        self.order_details[order_id]=dict(order_id=order_id,con_id=contract.conId,account=order.account,
            perm_id=getattr(order,'permId',None),client_id=getattr(order,'clientId',None),
            order_ref=getattr(order,'orderRef',None),parent_id=getattr(order,'parentId',None),
            order_type=getattr(order,'orderType',None),action=getattr(order,'action',None),
            quantity=str(getattr(order,'totalQuantity','')),stop_price=str(getattr(order,'auxPrice','')),
            oca_type=getattr(order,'ocaType',None),status=getattr(state,'status',None),
            receipt_utc=self.wall().isoformat(),receipt_monotonic=self.mono(),native_coverage_verified=False)

    def report(self):
        result=super().report()
        result['phase11_reality']=dict(size_fields=deepcopy(list(self.size_fields.values())),
            depth_events=deepcopy(self.book_fields),depth_capabilities=deepcopy(self.depth_capabilities),
            open_order_details=deepcopy(list(self.order_details.values())),
            connection_attempted_not_observation_verification=self.started,
            evidence_classification='UNVERIFIED_UNTIL_SEPARATELY_AUTHORIZED_CAPTURE_REVIEW',
            source_timestamp_uncertainty='UNBOUNDED_IF_NOT_SUPPLIED_NO_RECEIPT_SUBSTITUTION',
            broker_native_protection_verified=False,execution_enabled=False,
            reconciliation='DIAGNOSTIC_ONLY_NOT_COMPLETE_EXECUTION_COMMISSION_HISTORY')
        return result
