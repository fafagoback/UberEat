"""Compare normalized SQLite search with the local packed prototype."""
from __future__ import annotations

import argparse, hashlib, json, sqlite3, time
from collections import defaultdict
from difflib import SequenceMatcher
import msgpack, zstandard as zstd
from prototype_pack_db import normalize, query_tokens


def bucket_id(term, count):
    return int.from_bytes(hashlib.blake2s(term.encode(), digest_size=4).digest(), "big") % count


class PackedSearch:
    def __init__(self, path):
        self.db = sqlite3.connect(path); self.dec = zstd.ZstdDecompressor()
        self.buckets = json.loads(self.db.execute("SELECT value FROM metadata WHERE key='buckets'").fetchone()[0])
        self.cache = {}

    def posting(self, term):
        bid = bucket_id(term, self.buckets)
        if bid not in self.cache:
            blob, checksum = self.db.execute("SELECT payload,checksum FROM search_buckets WHERE bucket_id=?", (bid,)).fetchone()
            raw = self.dec.decompress(blob)
            assert hashlib.sha256(raw).hexdigest() == checksum
            self.cache[bid] = msgpack.unpackb(raw, raw=False)
        return self.cache[bid].get(term, [])

    def search(self, keyword, city="", promo=False, min_price=None, max_price=None,
               min_discount=None, fuzzy=False, limit=24):
        term_groups = []
        for text in (keyword, city):
            q = query_tokens(text)
            if q and fuzzy and text == keyword and len(q) > 1:
                votes = defaultdict(int)
                for term in q:
                    for ref in self.posting(term): votes[ref] += 1
                term_groups.append({ref for ref, count in votes.items() if count >= max(1, len(q) - 1)})
            elif q: term_groups.extend([set(self.posting(t)) for t in q])
        if promo: term_groups.append(set(self.posting("f:promo")))
        if min_price is not None or max_price is not None:
            lo = int(float(min_price or 0) // 50); hi = int(float(max_price if max_price is not None else 100000) // 50)
            term_groups.append(set().union(*(set(self.posting(f"f:price:{band}")) for band in range(lo, hi + 1))))
        if min_discount is not None:
            lo = int(float(min_discount) // 5)
            term_groups.append(set().union(*(set(self.posting(f"f:discount:{band}")) for band in range(lo, 21))))
        if not term_groups: return []
        refs = set.intersection(*term_groups)
        by_store = defaultdict(set)
        for ref in refs: by_store[ref >> 20].add(ref & ((1 << 20) - 1))
        found = []
        for store_id, wanted in by_store.items():
            product_offset = 0
            for blob, checksum in self.db.execute("SELECT payload,checksum FROM store_bundles WHERE store_id=? ORDER BY chunk_no", (store_id,)):
                raw = self.dec.decompress(blob); assert hashlib.sha256(raw).hexdigest() == checksum
                value = msgpack.unpackb(raw, raw=False)
                if value.get("store") is not None:
                    store_cols = value["store_columns"]; store = dict(zip(store_cols, value["store"]))
                    product_cols = value["product_columns"]
                for _, arr in value["products"]:
                    if product_offset in wanted:
                        p = dict(zip(product_cols, arr))
                        hay = normalize(" ".join(str(x or "") for x in (p["product_name"], p["category"], store["name"])))
                        cityhay = normalize(" ".join(str(x or "") for x in (store["city"], store["locality"], store["address"])))
                        keyword_ok = not keyword or normalize(keyword) in hay
                        if fuzzy and keyword and not keyword_ok:
                            keyword_ok = SequenceMatcher(None, normalize(keyword), normalize(p["product_name"])).ratio() >= .55
                        effective = float(p["effective_price"] or p["price"] or 0)
                        discount = float(p.get("discount_pct") or 0)
                        if keyword_ok and (not city or normalize(city) in cityhay) and (not promo or p["promo_type"] not in (None,"","無")) and float(p["price"] or 0) >= 1 and (min_price is None or effective >= min_price) and (max_price is None or effective <= max_price) and (min_discount is None or discount >= min_discount):
                            found.append((store["rating"], float(p["price"]), p["store_uuid"], p["product_uuid"]))
                    product_offset += 1
        found.sort(key=lambda x: (-(x[0] if x[0] is not None else -1e99), x[1], x[2], x[3]))
        return [(x[2], x[3]) for x in found[:limit]]


def sql_search(db, keyword, city="", promo=False, limit=24):
    clauses=["p.price>=1"]; args=[]
    if keyword: clauses.append("(p.product_name LIKE ? OR s.name LIKE ? OR p.category LIKE ?)"); args += [f"%{keyword}%"]*3
    if city: clauses.append("(s.city LIKE ? OR s.locality LIKE ? OR s.address LIKE ?)"); args += [f"%{city}%"]*3
    if promo: clauses.append("p.promo_type!='' AND p.promo_type!='無'")
    sql=f"SELECT p.store_uuid,p.product_uuid FROM products p JOIN stores s USING(store_uuid) WHERE {' AND '.join(clauses)} ORDER BY s.rating DESC NULLS LAST,p.price ASC,p.store_uuid,p.product_uuid LIMIT {limit}"
    return [tuple(r) for r in db.execute(sql,args)]


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("source"); ap.add_argument("packed"); args=ap.parse_args()
    src=sqlite3.connect(args.source)
    queries=[("雞排","",False),("咖啡","台北",False),("牛肉","",True),("麥當勞","",False),("便當","新北",False)]
    report=[]
    for keyword,city,promo in queries:
        t=time.perf_counter(); expected=sql_search(src,keyword,city,promo); sql_ms=(time.perf_counter()-t)*1000
        cold=PackedSearch(args.packed); t=time.perf_counter(); actual=cold.search(keyword,city,promo); cold_ms=(time.perf_counter()-t)*1000
        t=time.perf_counter(); warm=cold.search(keyword,city,promo); warm_ms=(time.perf_counter()-t)*1000
        report.append({"keyword":keyword,"city":city,"promo":promo,"sql_ms":round(sql_ms,2),"packed_cold_ms":round(cold_ms,2),"packed_warm_ms":round(warm_ms,2),"expected":len(expected),"actual":len(actual),"top24_equal":expected==actual,"overlap":len(set(expected)&set(actual))})
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
