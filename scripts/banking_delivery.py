"""Allowlisted local demo packaging and integrity checks; no user data or credentials."""
import hashlib
import importlib.metadata
import json
import re
import zipfile
from datetime import datetime,timezone
from pathlib import Path,PurePosixPath
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
MANIFEST='banking-release.json'
SCRIPT_NAMES={'__init__.py','serve_banking.py','banking_delivery.py','build_banking_release.py','init_banking_demo.py','launch_banking.py','manage_banking_identity.py','manage_banking_storage.py','export_banking_research.py'}


def selected_files(root):
    root=Path(root).resolve();files=set()
    for directory in ('backend','backend/agents','backend/rag','backend/eval','backend/tests'):
        files.update((root/directory).glob('*.py'))
    files.update((root/'backend/eval').glob('*.json'))
    for name in SCRIPT_NAMES:
        path=root/'scripts'/name
        if path.is_file():files.add(path)
    for name in ('requirements.txt','.env.example','frontend/package.json','frontend/package-lock.json','docs/DELIVERY_README.md'):
        path=root/name
        if path.is_file():files.add(path)
    for directory in ('frontend/dist','frontend/public/data'):
        files.update(path for path in (root/directory).rglob('*') if path.is_file())
    files.update(frontend_sources(root))
    for name in ('banking_release_v1.json','banking77_external_v1.json','amlsim_external_v1.json','amlsim_recall_v2.json','amlsim_robustness_v1.json'):
        path=root/'backend/eval/reports'/name
        if path.is_file():files.add(path)
    first=root/'backend/eval/reports/monthly_challenge_v1/first_run.json'
    if first.is_file():files.add(first)
    first=root/'backend/eval/reports/ai_expert_v1/first_run.json'
    if first.is_file():files.add(first)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('Release inputs must stay inside the project')
    return sorted(files)


def frontend_sources(root):
    root=Path(root);files=set()
    for directory in ('frontend/src','frontend/public','frontend/scripts'):
        files.update(path for path in (root/directory).rglob('*') if path.is_file())
    files.update((root/'frontend').glob('tsconfig*.json'))
    for name in ('index.html','package.json','package-lock.json','vite.config.ts'):
        path=root/'frontend'/name
        if path.is_file():files.add(path)
    return sorted(files)


def check_frontend(root):
    root=Path(root).resolve()
    saved=json.loads((root/'frontend/dist/build-meta.json').read_text(encoding='utf-8'))['files']
    actual={path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in frontend_sources(root)}
    if actual!=saved:raise ValueError('Frontend source changed since build; rebuild before delivery')
    return True


def secret_issues(root,contents):
    configured=dotenv_values(Path(root)/'.env') if (Path(root)/'.env').exists() else {}
    values={name:str(value).encode() for name,value in configured.items() if value and len(str(value))>=20 and re.search(r'KEY|SECRET|PASSWORD|TOKEN',name)}
    issues=[]
    for name,data in contents.items():
        if re.search(rb'(?m)^-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?$',data):issues.append({'file':name,'reason':'private_key_material'})
        for key,value in values.items():
            if value in data:issues.append({'file':name,'reason':'configured_secret','setting':key})
    return issues


def dependency_lock():
    packages={distribution.metadata['Name']:distribution.version for distribution in importlib.metadata.distributions() if distribution.metadata.get('Name')}
    return ('# Observed Python environment; install with the same Python/OS family.\n'+'\n'.join(f'{name}=={version}' for name,version in sorted(packages.items(),key=lambda item:item[0].lower()))+'\n').encode()


def third_party_notices(root):
    lines=['Frontend dependency license notices collected from this build environment.\n']
    modules=Path(root)/'frontend/node_modules'
    for package in sorted(modules.iterdir()) if modules.exists() else []:
        candidates=sorted(package.iterdir()) if package.name.startswith('@') and package.is_dir() else [package]
        for candidate in candidates:
            for pattern in ('LICENSE','LICENSE.txt','LICENSE.md','license','license.txt'):
                path=candidate/pattern
                if path.is_file():
                    lines.extend(['\n===== '+str(candidate.relative_to(modules))+' =====\n',path.read_text(encoding='utf-8',errors='replace')]);break
    return '\n'.join(lines).encode()


def verify(root):
    root=Path(root).resolve();manifest=json.loads((root/MANIFEST).read_text(encoding='utf-8'));errors=[]
    for name,expected in manifest['files'].items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:raise ValueError('Invalid manifest path')
        path=root.joinpath(*relative.parts)
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():errors.append(name);continue
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:errors.append(name)
    if errors:raise ValueError('Release integrity check failed: '+', '.join(errors[:5]))
    check_frontend(root)
    return {'verified':True,'files':len(manifest['files']),'release_id':manifest['release_id']}


def build(root=ROOT,output=None):
    root=Path(root).resolve()
    if not (root/'frontend/dist/index.html').is_file():raise ValueError('Build the frontend first')
    check_frontend(root)
    from backend.banking_readiness import source_hash
    report=json.loads((root/'backend/eval/reports/banking_release_v1.json').read_text(encoding='utf-8'))
    if report['source_sha256']!=source_hash() or report['regression']['passed']!=report['regression']['total'] or report['intent']['passed']!=report['intent']['total'] or not report['interpretation_side_effects']['no_funds_moved']:
        raise ValueError('Current source must have a passing, matching full regression report before packaging')
    contents={path.relative_to(root).as_posix():path.read_bytes() for path in selected_files(root)}
    contents.setdefault('scripts/__init__.py',b'')
    contents.setdefault('backend/__init__.py',b'')
    contents['requirements.lock.txt']=dependency_lock()
    contents['THIRD_PARTY_NOTICES.txt']=third_party_notices(root)
    index=contents['frontend/dist/index.html'].decode('utf-8')
    for asset in re.findall(r'(?:src|href)=[\"\'](/assets/[^\"\']+)[\"\']',index):
        if 'frontend/dist'+asset not in contents:raise ValueError('Frontend entry references a missing asset')
    issues=secret_issues(root,contents)
    if issues:raise ValueError('Secret scan blocked release: '+json.dumps(issues,ensure_ascii=False))
    now=datetime.now(timezone.utc);identifier=now.strftime('%Y%m%dT%H%M%S%fZ')
    manifest={'schema_version':1,'release_id':identifier,'created_at':now.isoformat(),'scope':'Loopback banking competition sandbox; no real funds or real-user records','files':{name:hashlib.sha256(data).hexdigest() for name,data in sorted(contents.items())}}
    contents[MANIFEST]=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    directory=Path(output or root/'output/releases').resolve()
    if not directory.is_relative_to(root/'output'):raise ValueError('Release output must stay inside project output/')
    directory.mkdir(parents=True,exist_ok=True);archive=directory/('starbank-'+identifier+'.zip')
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as package:
        for name,data in sorted(contents.items()):package.writestr(name,data)
    checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(checksum+'  '+archive.name+'\n',encoding='utf-8')
    return {'archive':str(archive),'sha256':checksum,'files':len(contents),'contains_user_data':False}
