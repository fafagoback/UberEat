from pathlib import Path
from datetime import datetime, timezone
import pytest
from src.city_normalization import canonical_city
from src.hf_retention import batch_time
from scripts.check_capacity import check


def test_city_does_not_infer_taipei_from_zhongshan_street():
    assert canonical_city(address='中山路100號') == ''
    assert canonical_city(region='台北市', address='臺東縣台東市中山路') == '台東縣'
    assert canonical_city(locality='New Taipei City') == '新北市'
    assert canonical_city(locality='Zhubei City') == ''


def test_retention_uses_taiwan_batch_timezone():
    assert batch_time('TaiwanMenuSnapshots/20260922063749/a') == datetime(2026,9,21,22,37,49,tzinfo=timezone.utc)


def test_capacity_rejects_oversized_next_stage(tmp_path):
    with pytest.raises(RuntimeError):
        check(tmp_path, required=10**18)


def test_no_normalized_publish_or_vacuum_in_daily_workflow():
    workflow = Path('.github/workflows/taiwan_store_crawler.yml').read_text(encoding='utf-8')
    assert 'serving.db' not in workflow
    assert 'deploy-pages' not in workflow
    assert 'VACUUM' not in workflow
