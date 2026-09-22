"""Stage only frontend assets and a bounded, clearly dated fallback for Pages."""
import argparse
import json
import shutil
from pathlib import Path

ASSETS = ('index.html', 'app.js', 'packed-turso.js', 'data-contract.js', 'style.css',
          'config.js', 'version.json', '.nojekyll', 'favicon.svg', 'favicon.ico',
          'favicon.png', 'apple-touch-icon.png')


def build(output):
    root = Path(output)
    if root.exists() and any(root.iterdir()):
        raise ValueError('Pages staging directory must be empty')
    root.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        source = Path('web', name)
        if source.exists():
            shutil.copyfile(source, root / name)
    data = root / 'data'; data.mkdir(exist_ok=True)
    stats = json.loads(Path('web/data/stats.json').read_text(encoding='utf-8'))
    batch = stats.get('latest_batch', '')
    stats['fallback_only'] = True
    stats['latest_batch_formatted'] = f"離線備援（{stats.get('latest_batch_formatted', batch)}；清單限量）"
    (data / 'stats.json').write_text(json.dumps(stats, ensure_ascii=False), encoding='utf-8')
    for name in ('discounts', 'new_stores', 'new_products', 'promotions', 'products'):
        payload = json.loads(Path(f'web/data/{name}.json').read_text(encoding='utf-8'))
        rows = payload.get('items', []) if isinstance(payload, dict) else payload
        # Stable sample for outage browsing, never advertised as the complete catalog.
        rows = rows[:200]
        result = {'status': 'success', 'latest_batch': batch, 'fallback_only': True, 'items': rows}
        encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
        while len(encoded) > 500_000 and rows:
            rows = rows[:len(rows)//2]; result['items'] = rows
            encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
        (data / f'{name}.json').write_bytes(encoded)
    (data / 'history.json').write_text(json.dumps({'history': {}, 'fallback_only': True, 'latest_batch': batch}), encoding='utf-8')
    total = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    if total >= 10_000_000:
        raise ValueError('Static site exceeds project 10 MB staging budget')
    report = {'pages_uncompressed_bytes': total, 'fallback_batch': batch, 'fallback_max_rows_per_list': 200}
    print(json.dumps(report))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--output', default='_site'); a = p.parse_args()
    build(a.output)
