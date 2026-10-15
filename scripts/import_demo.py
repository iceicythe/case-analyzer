"""Import public synthetic examples through the running workbench's HTTP API."""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def import_demo(base_url):
    def request(path, value=None):
        req = urllib.request.Request(base_url.rstrip('/')+path,
            data=json.dumps(value).encode('utf-8') if value is not None else None,
            headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:
            return json.load(response)
    existing = {case['case_id'] for case in request('/api/data')['cases']}
    for directory in sorted((ROOT/'examples').glob('demo_*')):
        if not directory.is_dir(): continue
        payload = json.loads((directory/'source.json').read_text(encoding='utf-8'))
        if payload['case_id'] in existing:
            print(f"Skip existing: {payload['case_id']}"); continue
        payload['files'] = {p.name:p.read_text(encoding='utf-8') for p in sorted(directory.glob('*.csv'))}
        request('/api/import', payload)
        print(f"Imported: {payload['case_id']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8770')
    args = parser.parse_args()
    try: import_demo(args.url)
    except (OSError,ValueError) as error:
        parser.exit(1,f'Import failed: {error}\n先启动工作台，必要时用 --url 指定地址。\n')
