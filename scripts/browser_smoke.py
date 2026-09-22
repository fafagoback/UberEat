"""CLI-only Chromium acceptance against a local candidate or deployed Pages."""
import argparse
import functools
import http.server
import json
import re
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def run(url=None, directory="web"):
    server = None
    if not url:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/'
    report = {'url': url, 'checks': [], 'errors': []}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on('pageerror', lambda error: report['errors'].append(str(error)))
            page.on('console', lambda message: report['errors'].append(message.text)
                    if message.type == 'error' else None)
            page.goto(url, wait_until='networkidle', timeout=60000)
            page.wait_for_function("document.getElementById('batch-time-text').textContent !== '載入中...'", timeout=90000)
            def check(name):
                text = page.locator('.radar-card:visible').all_inner_texts()
                text = '\n'.join(text)
                if re.search(r'\$NaN|undefined|\$0(?![\d.])|\$-\d', text):
                    raise AssertionError(f'{name}: invalid displayed price/value')
                report['checks'].append(name)
            check('dashboard')
            for name in ('今日大特價 (-30%+)', '新進店家速報', '老店新菜推薦', '折扣活動專區', '全庫商品即時檢索'):
                page.get_by_role('button', name=name, exact=True).click()
                page.wait_for_timeout(500)
                check(name)
            # UI data must actually exist, not merely render an empty shell.
            page.locator('#global-products-grid .radar-card').first.wait_for(timeout=60000)
            check('catalog cards present')
            next_page = page.locator('#global-pagination').get_by_role('button', name='下一頁', exact=True)
            if next_page.count() and next_page.is_enabled():
                first = page.locator('#global-products-grid .radar-card').first.inner_text()
                next_page.click()
                page.wait_for_timeout(1000)
                assert page.locator('#global-products-grid .radar-card').first.inner_text() != first
                check('pagination next')
                page.locator('#global-pagination').get_by_role('button', name='上一頁', exact=True).click()
                page.wait_for_timeout(500)
                check('pagination previous')
            history = page.locator('#global-products-grid button[data-action="history"]').first
            history.click()
            page.locator('#price-history-modal').wait_for(state='visible')
            page.locator('#modal-history-tbody tr').first.wait_for(timeout=30000)
            check('price history modal')
            page.keyboard.press('Escape')
            # Reload to close a modal without depending on an implementation-specific close control.
            page.reload(wait_until='networkidle')
            page.get_by_role('button', name='全庫商品即時檢索', exact=True).click()
            page.locator('#global-search-input').fill('雞排')
            page.wait_for_function("APP_STATE.globalProducts?.length > 0 && APP_STATE.globalProducts.every(p => (p.product_name + p.store_name + p.category_name).includes('雞排'))", timeout=60000)
            check('keyword search')
            page.locator('#global-city-select').select_option(label='📍 台北市')
            page.wait_for_function("APP_STATE.globalProducts?.length > 0 && APP_STATE.globalProducts.every(p => p.city === '台北市')", timeout=60000)
            check('city filter')
            report['visible_result_count'] = page.locator('#global-products-grid .radar-card').count()
            browser.close()
        if report['errors']:
            raise AssertionError(report['errors'])
        return report
    finally:
        if server:
            server.shutdown()
        Path('browser-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--url'); ap.add_argument('--directory', default='web'); a = ap.parse_args()
    print(json.dumps(run(a.url, a.directory), ensure_ascii=True))
