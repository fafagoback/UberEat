"""Pin one immutable, explicitly accepted HF history for a manual rebuild."""
import json
import os
from pathlib import Path
from huggingface_hub import HfApi, hf_hub_download

api = HfApi(token=os.getenv('HF_TOKEN'))
repo = os.getenv('HF_REPO_ID', 'hub-google/UberEat')
info = api.repo_info(repo, repo_type='dataset', files_metadata=True)
files = {x.rfilename: x for x in info.siblings}
rejections_path = Path('data/hf_rejected_batches.json')
rejections = json.loads(rejections_path.read_text(encoding='utf-8')) if rejections_path.exists() else {}
all_raw = sorted((x for x in info.siblings if x.rfilename.startswith('TaiwanMenuSnapshots/')
                  and x.rfilename.endswith('.tar.gz')), key=lambda x: x.rfilename)
raw = []
excluded = []
for item in all_raw:
    batch = item.rfilename.split('/')[1]
    if batch in rejections:
        excluded.append({'batch_id': batch, 'path': item.rfilename, 'reason': rejections[batch]})
        continue
    # First marker-bearing release, verified against the HF file listing on
    # 2026-09-24. Earlier archives predate this protocol; strict replay still
    # validates their nationwide store floor and the rejection audit excludes
    # the three known partial releases.
    complete_path = f'TaiwanMenuSnapshots/{batch}/complete.json'
    if batch >= '20260923183927' and complete_path not in files:
        raise SystemExit(f'Raw archive lacks complete.json: {item.rfilename}')
    if complete_path in files:
        marker_path = hf_hub_download(repo_id=repo, repo_type='dataset', filename=complete_path,
                                      revision=info.sha, token=os.getenv('HF_TOKEN'))
        marker = json.loads(Path(marker_path).read_text(encoding='utf-8'))
        lfs_sha = item.lfs.get('sha256') if isinstance(item.lfs, dict) else getattr(item.lfs, 'sha256', None)
        if (marker.get('complete') is not True or marker.get('batch_id') != batch
                or marker.get('archive_path') != item.rfilename
                or int(marker.get('archive_bytes') or -1) != int(item.size or 0)
                or marker.get('sha256') != lfs_sha):
            raise SystemExit(f'Raw completion marker mismatch: {complete_path}')
    raw.append(item)
if not raw:
    raise SystemExit('No complete Raw archives found')
manifest = {'repo': repo, 'revision': info.sha, 'latest_batch': raw[-1].rfilename.split('/')[1],
            'raw_count': len(raw), 'raw_bytes': sum(x.size or 0 for x in raw),
            'excluded': excluded,
            'archives': [{'path': x.rfilename, 'bytes': x.size,
                          'sha256': (x.lfs.get('sha256') if isinstance(x.lfs, dict) else getattr(x.lfs, 'sha256', None))} for x in raw]}
Path('source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(json.dumps({k: v for k, v in manifest.items() if k != 'archives'}))
with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
    f.write(f"revision={info.sha}\nlatest_batch={manifest['latest_batch']}\n")
