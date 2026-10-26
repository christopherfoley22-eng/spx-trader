"""Cold-start existing OFFLINE protective journals with dashboard storage absent.

This is an explicit local, capability-authenticated recovery entrypoint. It never
constructs dashboard authentication, restores entry leases, creates broker proof,
connects a socket or imports a test authenticator. All broker evidence must still
be supplied and verified by the existing synthetic-only session boundary.
"""
from pathlib import Path
from executor_allocation_service import AllocationBoundary
from executor_application import UnifiedApplication,DisabledBrokerBoundary
from executor_lifecycle import OfflineExecutionLifecycle
from mortificatio_models import ApplicationError


class UnavailableFoundation:
    """Deny all app storage access; never a replacement in-memory database."""
    def __init__(self,clock):self.clock=clock
    def transact(self,*args,**kwargs):raise OSError('Dashboard foundation unavailable in protection-only recovery')
    def database(self):raise OSError('Dashboard foundation unavailable in protection-only recovery')


class NoDashboardAuthority:
    def __init__(self,policy,clock):self.application_policy=policy;self.store=UnavailableFoundation(clock)
    def __getattr__(self,name):raise ApplicationError('DASHBOARD_DISABLED_PROTECTION_ONLY',503)


class NoEntryMarketProvider:
    def snapshot(self,*args):raise ApplicationError('RECOVERY_CANNOT_ACQUIRE')


def recover_offline_protection(root,approval,fixture,authority,service_token,application_policy,
                               allocation_policy,wall_clock,monotonic_clock,start_worker=True):
    """Requires existing checkpoint/session journals; refuses fresh empty recovery."""
    authority.require(service_token,'RECONCILE');authority.require(service_token,'OBSERVE_RISK')
    application_policy.validate()
    if application_policy.binding!=authority.binding:raise ApplicationError('PROTECTIVE_RECOVERY_BINDING_MISMATCH')
    root=Path(root).resolve()
    if any(not (root/name).is_file() or (root/name).is_symlink() for name in ('protection.sqlite','session.sqlite')):
        raise ApplicationError('EXISTING_PROTECTIVE_CHECKPOINT_REQUIRED',503)
    security=NoDashboardAuthority(application_policy,lambda:wall_clock().timestamp())
    app=UnifiedApplication(security,authority,DisabledBrokerBoundary(),AllocationBoundary(allocation_policy,NoEntryMarketProvider()))
    return OfflineExecutionLifecycle(app,root,approval,fixture,service_token,wall_clock,monotonic_clock,start_worker=start_worker)
