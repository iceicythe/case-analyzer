"""Check public files for accidental local artifacts and broken doc links."""
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {'.git','.venv','venv','__pycache__','runtime','artifacts','archive','acceptance_runtime'}
PRIVATE = {'deepseek.local.json','.env'}


def public_files():
    try:
        top = subprocess.run(['git','rev-parse','--show-toplevel'],cwd=ROOT,capture_output=True,text=True,check=True)
        if Path(top.stdout.strip()).resolve() == ROOT:
            result = subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True,check=True)
            names = [name for name in result.stdout.decode('utf-8').split('\0') if name]
            if names: return [ROOT/name for name in names]
    except (OSError,subprocess.CalledProcessError):
        pass
    return [p for p in ROOT.rglob('*') if p.is_file()
            and not set(p.relative_to(ROOT).parts)&IGNORED and p.name not in PRIVATE
            and not p.name.startswith('.env.')]


def check():
    errors = []; files = public_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if set(relative.parts)&IGNORED or path.name in PRIVATE or path.name.startswith('.env.'):
            errors.append(f'Private/generated path tracked: {relative}'); continue
        if not path.is_file():
            errors.append(f'Missing tracked file: {relative}'); continue
        if path.stat().st_size > 5*1024*1024:
            errors.append(f'Unexpected large public file: {relative}')
        if path.suffix.lower() in {'.png','.jpg','.jpeg','.zip'}: continue
        try: text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            errors.append(f'Unexpected binary file: {relative}'); continue
        if re.search(r'\bsk-[A-Za-z0-9_-]{24,}\b',text):
            errors.append(f'Possible credential in: {relative}')
        if re.search(r'[A-Za-z]:[\\/](?:Users|case-analyzer)[\\/]',text):
            errors.append(f'Personal absolute path in: {relative}')
        if relative.parts[0]=='configs' and path.suffix=='.json':
            try:
                config = json.loads(text)
                if isinstance(config,dict) and config.get('api_key'):
                    errors.append(f'Non-empty public API key: {relative}')
            except ValueError:
                errors.append(f'Invalid JSON: {relative}')
        if path.suffix=='.md':
            for target in re.findall(r'!?\[[^\]]*\]\(([^\s)]+)\)',text):
                link = urlsplit(target)
                if link.scheme or link.netloc or not link.path: continue
                resolved = (path.parent/unquote(link.path)).resolve()
                if not resolved.is_relative_to(ROOT) or not resolved.exists():
                    errors.append(f'Broken local link in {relative}: {target}')
    if errors:
        for error in errors: print(error)
        raise SystemExit(1)
    print(f'Public repository checks passed: {len(files)} files; local artifacts excluded; doc links valid.')


if __name__ == '__main__': check()
