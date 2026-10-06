"""恢复缺失的官方检测/对齐权重下载；不运行推理，不替换已有完整文件。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

root = Path('/root/autodl-tmp/avatar-motion/EXP-001')
manifest = json.loads((root / 'sad_setup/权重清单.json').read_text())
selected = [x for x in manifest['assets'] if x['role'] in ('required_alignment', 'required_detection')]
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

def download(item):
    destination = root / 'SadTalker' / item['path']
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    partial = destination.with_name(destination.name + '.' + stamp + '.download2.part')
    if partial.exists():
        raise FileExistsError(partial)
    started = time.monotonic()
    completed = subprocess.run(['curl', '-fLsS', '--connect-timeout', '15', '--max-time', '300',
                                '--retry', '2', '--retry-delay', '2', '-o', str(partial), item['url']],
                               capture_output=True, text=True)
    result = {'path': item['path'], 'url': item['url'], 'returncode': completed.returncode,
              'wall_seconds': round(time.monotonic()-started, 3), 'stderr': completed.stderr,
              'upstream_sha256_verified': False}
    if partial.is_file():
        result['bytes'] = partial.stat().st_size
        result['sha256'] = hashlib.sha256(partial.read_bytes()).hexdigest()
    result['verified_size'] = result.get('bytes') == item['bytes']
    if completed.returncode == 0 and result['verified_size']:
        if destination.exists():
            raise FileExistsError(destination)
        partial.rename(destination)
        result['state'] = 'DOWNLOADED'
    else:
        result['state'] = 'FAILED'
    return result

with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(download, selected))
with (root / 'evidence/sadtalker' / f'download-recovery2-{stamp}.json').open('x') as handle:
    json.dump(results, handle, indent=2)
print(json.dumps(results), flush=True)
raise SystemExit(0 if all(x['state'] == 'DOWNLOADED' for x in results) else 1)
