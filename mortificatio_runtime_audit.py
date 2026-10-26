"""Offline runtime policy and complete local-wheel hash locking. No installer/network."""
import argparse
from email.parser import BytesParser
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import ssl
import sys
import zipfile

# Maintained packaging parsers shipped in the existing pinned pip; build tool only.
from pip._vendor.packaging.markers import default_environment
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.specifiers import SpecifierSet
from pip._vendor.packaging.tags import cpython_tags,compatible_tags,mac_platforms
from pip._vendor.packaging.utils import canonicalize_name,parse_wheel_filename
from pip._vendor.packaging.version import Version

REQUIRED=frozenset({'webauthn','cryptography','pyopenssl','cbor2','pyasn1','pyasn1-modules',
    'cffi','pycparser','typing-extensions','ibapi','pip','setuptools'})
MINIMUM={'webauthn':Version('3.0.1'),'setuptools':Version('78.1.1'),'pip':Version('26.2.2')}
# Official 2026-10-25 security releases revoke compromised CA keys. These
# reviewed floors do not replace a complete advisory/provenance review.
PYTHON_SECURITY_FLOORS={(3,13):(3,13,18),(3,14):(3,14,10)}


def runtime_audit():
    packages={canonicalize_name(d.metadata['Name']):d.version for d in importlib.metadata.distributions()}
    issues=[]
    for name in sorted(REQUIRED-set(packages)):
        issues.append('MISSING_REQUIRED_PACKAGE:'+name)
    if sys.version_info<(3,13):issues.append('PYTHON_3_13_MIGRATION_REQUIRED')
    floor=PYTHON_SECURITY_FLOORS.get(tuple(sys.version_info[:2]))
    if floor and tuple(sys.version_info[:3])<floor:
        issues.append('PYTHON_SECURITY_PATCH_REQUIRED')
    if not ssl.OPENSSL_VERSION.startswith('OpenSSL ') or ssl.OPENSSL_VERSION_INFO<(3,5):
        issues.append('MAINTAINED_TLS_STACK_REVALIDATION_REQUIRED')
    for name,floor in MINIMUM.items():
        if name in packages and Version(packages[name])<floor:issues.append(name.upper()+'_BELOW_REVIEWED_FLOOR')
    return dict(python=platform.python_version(),tls=ssl.OPENSSL_VERSION,packages=packages,
        issues=issues,execution_enabled=False,assessment='NO_GO' if issues else 'REQUIRES_FULL_ACCEPTANCE',
        vulnerability_scan_complete=False)


def lock_local_wheels(directory,target='3.13.0'):
    """Validate complete wheel closure; hash actual artifacts, never installed RECORDs.

    This does NOT install, resolve online, assert zero CVEs, or authorize execution.
    Target platform is this macOS architecture; target interpreter is CPython 3.13.
    """
    version=Version(target)
    if version.release[:2]!=(3,13):raise ValueError('Explicit CPython 3.13 target required')
    root=Path(directory)
    if not root.is_dir() or root.is_symlink():raise ValueError('Local wheel directory required')
    platforms=list(mac_platforms(arch=platform.machine()))
    tags=set(cpython_tags((3,13),platforms=platforms))|set(compatible_tags((3,13),interpreter='cp313',platforms=platforms))
    environment=default_environment();environment.update(python_version='3.13',python_full_version=target,
        implementation_version=target,extra='')
    records={}
    for file in sorted(root.glob('*.whl')):
        if file.is_symlink():raise ValueError('Symlink wheel refused')
        name,wheel_version,_,wheel_tags=parse_wheel_filename(file.name)
        name=canonicalize_name(name)
        if not wheel_tags&tags:raise ValueError('Wheel incompatible with CPython 3.13/macOS: '+file.name)
        if name in records:raise ValueError('Ambiguous duplicate wheel for '+name)
        if name in MINIMUM and wheel_version<MINIMUM[name]:raise ValueError('Vulnerable/obsolete policy floor: '+name)
        with zipfile.ZipFile(file) as archive:
            metadata=[i for i in archive.infolist() if i.filename.endswith('.dist-info/METADATA')]
            if len(metadata)!=1 or metadata[0].file_size>1024*1024:raise ValueError('Bounded unique wheel metadata required')
            info=BytesParser().parsebytes(archive.read(metadata[0]))
        if canonicalize_name(info['Name'])!=name or Version(info['Version'])!=wheel_version:
            raise ValueError('Wheel metadata identity disagreement')
        if version not in SpecifierSet(info.get('Requires-Python','')):raise ValueError('Requires-Python rejects target: '+name)
        digest=hashlib.sha256()
        with file.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        records[name]=dict(version=wheel_version,hash=digest.hexdigest(),requirements=info.get_all('Requires-Dist',[]))
    if REQUIRED-set(records):raise ValueError('Incomplete wheelhouse: '+', '.join(sorted(REQUIRED-set(records))))
    for name,record in records.items():
        for raw in record['requirements']:
            requirement=Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(environment):continue
            dependency=canonicalize_name(requirement.name)
            if requirement.url or requirement.extras or dependency not in records or records[dependency]['version'] not in requirement.specifier:
                raise ValueError('Unresolved/unsupported dependency: '+name+' -> '+raw)
    lines=['# Offline artifact lock; CPython '+target+' / macOS '+platform.machine(),
        '# Generated from local wheels. Does not certify CVE clearance or broker compatibility.']
    lines+=['{}=={} --hash=sha256:{}'.format(name,record['version'],record['hash']) for name,record in sorted(records.items())]
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse');parser.add_argument('--output');parser.add_argument('--target',default='3.13.0')
    args=parser.parse_args()
    if args.wheelhouse or args.output:
        if not args.wheelhouse or not args.output:parser.error('--wheelhouse and --output required together')
        content=lock_local_wheels(args.wheelhouse,args.target)
        with Path(args.output).open('x') as output:output.write(content)
        return 0
    result=runtime_audit();print(json.dumps(result,indent=2,sort_keys=True))
    return 1 if result['issues'] else 0


if __name__=='__main__':raise SystemExit(main())
