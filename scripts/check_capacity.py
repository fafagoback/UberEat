"""Read-only resource guard. Values are bytes, never compressed cache estimates."""
import argparse
import json
import shutil
from pathlib import Path


def check(path='.', required=0, reserve=2 * 1024**3):
    disk = shutil.disk_usage(path)
    report = {'disk_total_bytes': disk.total, 'disk_free_bytes': disk.free,
              'required_bytes': required, 'reserve_bytes': reserve}
    print(json.dumps(report), flush=True)
    if disk.free < required + reserve:
        raise RuntimeError('Insufficient disk for the next stage plus reserve')
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--required-bytes', type=int, default=0)
    p.add_argument('--input-dir')
    p.add_argument('--packed')
    a = p.parse_args()
    needed = a.required_bytes
    if a.input_dir:
        needed += sum(f.stat().st_size for f in Path(a.input_dir).rglob('*.db'))
    if a.packed:
        size = Path(a.packed).stat().st_size
        print(json.dumps({'packed_bytes': size}))
        if size >= 4_750_000_000:
            raise SystemExit('Packed database exceeds the unchanged storage guard')
    check(required=needed)
