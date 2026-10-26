"""Market observation provenance, never a replacement for original entry gates."""
from dataclasses import dataclass,asdict
from datetime import datetime
from copy import deepcopy
import math
from executor_paper_broker import utc,integer,text_id
from simulation_evidence import validate_observation_time
from executor_paper_types import PaperBrokerError


@dataclass(frozen=True)
class MarketDatum:
    source:str
    instrument:str
    generation:int
    sequence:int
    value:object
    source_time:object
    receipt_time:datetime
    receipt_monotonic:float
    quality:str
    missing:tuple = ()
    origin:str = 'OFFLINE_FIXTURE'

    def assessment(self,now_wall,now_mono):
        if (not text_id(self.source) or not text_id(self.instrument) or not integer(self.generation,1)
                or not integer(self.sequence,1) or self.origin!='OFFLINE_FIXTURE'):raise PaperBrokerError('MARKET_PROVENANCE_REQUIRED')
        utc(self.receipt_time);missing=list(self.missing)
        if self.value is None or isinstance(self.value,bool) or isinstance(self.value,float) and not math.isfinite(self.value):missing.append('value')
        if self.source_time is None:missing.append('authoritative_source_timestamp')
        status='UNAVAILABLE'
        if not missing and self.quality=='LIVE':
            try:
                validate_observation_time(self.source_time,self.receipt_monotonic,now_wall,now_mono)
                validate_observation_time(self.receipt_time,self.receipt_monotonic,now_wall,now_mono)
                status='SOURCE_VALIDATED'
            except Exception:status='STALE_OR_INVALID'
        elif self.quality!='LIVE':status='REJECTED_'+self.quality
        return dict(source=self.source,instrument=self.instrument,generation=self.generation,sequence=self.sequence,
            source_time=utc(self.source_time).isoformat() if self.source_time else None,
            receipt_time=utc(self.receipt_time).isoformat(),receipt_monotonic=self.receipt_monotonic,
            source_age_seconds=(utc(now_wall)-utc(self.source_time)).total_seconds() if self.source_time else None,
            receipt_age_seconds=now_mono-self.receipt_monotonic,status=status,missing_fields=missing,
            value=deepcopy(self.value),origin=self.origin,authorizes_entry=False)


class MarketState:
    def __init__(self):self.values={};self.failure={}
    def ingest(self,datum,now_wall,now_mono):
        key=(datum.source,datum.instrument);old=self.values.get(key)
        if old and (datum.generation<old.generation or datum.generation==old.generation and
            (datum.sequence<=old.sequence or datum.source_time and old.source_time and datum.source_time<old.source_time)):
            if datum==old:return 'DUPLICATE'
            self.failure[key]='OUT_OF_ORDER';self.values.pop(key,None);raise PaperBrokerError('OUT_OF_ORDER_MARKET_EVENT')
        assessment=datum.assessment(now_wall,now_mono);self.values[key]=deepcopy(datum)
        if assessment['status']!='SOURCE_VALIDATED':self.failure[key]=assessment['status']
        else:self.failure.pop(key,None)
        return assessment
