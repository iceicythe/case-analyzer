"""Build an allowlisted public source/runtime archive; never read private config."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    files = {}
    def add(path):
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('Release source outside repository')
        files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in ('README.md','LICENSE','CHANGELOG.md','CONTRIBUTING.md',
                 '.gitignore','.gitattributes','.editorconfig','requirements-dev.txt',
                 'requirements.txt','setup.cmd','start.cmd','check-api.cmd'):
        add(ROOT/name)
    for folder, extensions in (
        ('app',{'.py'}),('static',{'.html','.js','.css','.svg'}),
        ('knowledge',{'.md','.json'}),('docs',{'.md','.png'}),
        ('examples',{'.py','.md','.csv','.json'}),('scripts',{'.py'}),
        ('tests',{'.py'}),('.github',{'.yml','.yaml','.md'}),
    ):
        for path in sorted((ROOT/folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in extensions: add(path)
    for path in sorted((ROOT/'configs').glob('*.json')):
        if not path.name.endswith('.local.json'): add(path)
    template = json.loads(files['configs/deepseek.example.json'])
    template['api_key'] = ''
    files['configs/deepseek.local.json'] = (json.dumps(template,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    files['configs/feishu.local.json'] = files['configs/feishu.example.json']
    manifest = {name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
    files['release-manifest.json'] = json.dumps(manifest,indent=2).encode('utf-8')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(files.items()):
            archive.writestr('case-analyzer/'+name,data)
    print(json.dumps({'archive':str(output),'files':len(files),'sha256':hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('output',type=Path)
    build(parser.parse_args().output)
