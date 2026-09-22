"""Conservative city extraction: ambiguous district/street names are not cities."""
import re

CITIES = ('基隆市 台北市 新北市 桃園市 新竹市 新竹縣 苗栗縣 台中市 彰化縣 南投縣 '
          '雲林縣 嘉義市 嘉義縣 台南市 高雄市 屏東縣 宜蘭縣 花蓮縣 台東縣 澎湖縣 金門縣 連江縣').split()
ALIASES = dict(zip(('Keelung City|Taipei City|New Taipei City|Taoyuan City|Hsinchu City|Hsinchu County|'
                   'Miaoli County|Taichung City|Changhua County|Nantou County|Yunlin County|Chiayi City|'
                   'Chiayi County|Tainan City|Kaohsiung City|Pingtung County|Yilan County|Hualien County|'
                   'Taitung County|Penghu County|Kinmen County|Lienchiang County').lower().split('|'), CITIES))


def canonical_city(region='', locality='', address=''):
    # Full address is more reliable than the old exporter's guessed region.
    for value in (address, region, locality):
        text = str(value or '').replace('臺', '台').strip()
        found = {city for city in CITIES if city in text}
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            return ''
        matches = {city for alias, city in ALIASES.items()
                   if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', text.lower())}
        # New Taipei City also contains Taipei City; prefer the longer exact alias.
        if text.lower() in ALIASES:
            return ALIASES[text.lower()]
        if 'new taipei city' in text.lower():
            matches.discard('台北市')
        if len(matches) == 1:
            return matches.pop()
    return ''
