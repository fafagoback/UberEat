"""Pin one immutable HF revision for every shard in a build."""
import json
import os
from pathlib import Path
from huggingface_hub import HfApi

api = HfApi(token=os.getenv('HF_TOKEN'))
repo = os.getenv('HF_REPO_ID', 'hub-google/UberEat')
info = api.repo_info(repo, repo_type='dataset', files_metadata=True)
raw = sorted((x for x in info.siblings if x.rfilename.startswith('TaiwanMenuSnapshots/')
              and x.rfilename.endswith('.tar.gz')), key=lambda x: x.rfilename)
if not raw:
    raise SystemExit('No complete Raw archives found')
manifest = {'repo': repo, 'revision': info.sha, 'latest_batch': raw[-1].rfilename.split('/')[1],
            'raw_count': len(raw), 'raw_bytes': sum(x.size or 0 for x in raw),
            'archives': [{'path': x.rfilename, 'bytes': x.size,
                          'sha256': (x.lfs.get('sha256') if isinstance(x.lfs, dict) else getattr(x.lfs, 'sha256', None))} for x in raw]}
Path('source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(json.dumps({k: v for k, v in manifest.items() if k != 'archives'}))
with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
    f.write(f"revision={info.sha}\nlatest_batch={manifest['latest_batch']}\n")
