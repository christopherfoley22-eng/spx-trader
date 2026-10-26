"""Guarded Phase 12 diagnostic launcher. Default is local preflight, never connect."""
import argparse
import json
import os
from pathlib import Path
import stat
from mortificatio_runtime_audit import runtime_audit


def private_destination(raw):
    path=Path(raw).absolute()
    if any(p.is_symlink() for p in (path,)+tuple(path.parents)):
        raise ValueError('SYMLINK_DESTINATION_REJECTED')
    if path.exists() or stat.S_IMODE(path.parent.stat().st_mode)!=0o700:
        raise ValueError('NEW_FILE_IN_PRIVATE_0700_DIRECTORY_REQUIRED')
    return path


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config');parser.add_argument('--capture');parser.add_argument('--report')
    parser.add_argument('--seconds',type=int,choices=range(1,61),default=10)
    parser.add_argument('--client-id',type=int,default=5091)
    parser.add_argument('--include-execution-history',action='store_true')
    parser.add_argument('--authorize-read-only-connection',action='store_true')
    args=parser.parse_args(argv)
    audit=runtime_audit()
    summary=dict(connection_attempted=False,execution_enabled=False,runtime_issues=audit['issues'],
                 paper='NO_GO',live='NO_GO')
    if not args.authorize_read_only_connection:
        summary['status']='LOCAL_PREFLIGHT_ONLY'
        print(json.dumps(summary,sort_keys=True));return 1 if audit['issues'] else 0
    if audit['issues']:
        summary['status']='RUNTIME_BLOCKED'
        print(json.dumps(summary,sort_keys=True));return 1
    try:
        if not all((args.config,args.capture,args.report)):raise ValueError('PRIVATE_PATHS_REQUIRED')
        capture=private_destination(args.capture);report=private_destination(args.report)
        if len({capture,report,Path(args.config).absolute()})!=3:raise ValueError('DISTINCT_FILES_REQUIRED')
        # Import the transport only after explicit authorization and runtime checks.
        from ibkr_paper_read_only_validation import load_config
        from executor_broker_lifecycle_capture import BrokerLifecycleValidation
        from ibkr_phase12_read_only import run_lifecycle_read_only
        approval,settings=load_config(args.config)
        # Exclusive report creation before transport: no overwrite or stdout secrets.
        fd=os.open(str(report),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as output:
            validation=BrokerLifecycleValidation(approval,settings,capture_path=capture)
            summary['connection_attempted']=True
            result=run_lifecycle_read_only(validation,authorized=True,
                include_execution_history=args.include_execution_history,
                seconds=args.seconds,client_id=args.client_id)
            json.dump(result,output,sort_keys=True);output.flush();os.fsync(output.fileno())
        summary['status']='DIAGNOSTIC_RECORDED_NOT_TRADING_ACCEPTANCE'
        print(json.dumps(summary,sort_keys=True))
        return 0 if result.get('paper_account_matched') and not result.get('session_failure') else 1
    except Exception:
        summary['status']='CONFIGURATION_OR_CAPTURE_REJECTED'
        print(json.dumps(summary,sort_keys=True));return 1


if __name__=='__main__':raise SystemExit(main())
