import hashlib
import json
from pathlib import Path

root = Path('C:/Users/<operator>/esp')
metadata = root / 'esp-idf/tools/tools.json'
tools = json.loads(metadata.read_text())['tools']
verified = []
for archive in (root / 'tools/dist').iterdir():
    if archive.suffix == '.tmp':
        continue
    matches = []
    for tool in tools:
        for version in tool['versions']:
            for platform, value in version.items():
                if isinstance(value, dict) and value.get('rename_dist', value.get('url', '').split('/')[-1]) == archive.name:
                    matches.append((tool['name'], version['name'], platform, value))
    if not matches:
        raise SystemExit(f'No trusted SDK metadata for {archive}')
    actual = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    match = matches[0]
    spec = match[3]
    assert actual == spec['sha256'], (archive.name, actual, spec['sha256'])
    assert archive.stat().st_size == spec['size'], archive.name
    row = {'archive': archive.name, 'tool': match[0], 'version': match[1], 'url': spec['url'], 'size': archive.stat().st_size, 'sha256': actual, 'verified': True}
    verified.append(row)
    print('VERIFIED', archive.name, actual)
(root / 'tool-downloads-verified.json').write_text(json.dumps({'sdk_commit': 'fff9895c82d744c7237be8847347bdd1b07c6643', 'tools_json_sha256': hashlib.sha256(metadata.read_bytes()).hexdigest(), 'downloads': verified}, indent=2))
