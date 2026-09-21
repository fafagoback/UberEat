import json, sqlite3, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.serving_state import apply_snapshot
from src.hf_retention import retention_plan

S='11111111-1111-4111-8111-111111111111'; P='22222222-2222-4222-8222-222222222222'
def doc(price=100, product=P, name='COSTCO 牛肉', store='COSTCO 台北', products=True, is_open=True):
    return {'@id':'https://example/'+S,'store_uuid':S,'name':store,'address':{'addressLocality':'台北市'},
      'isOpen': is_open,
      'aggregateRating':{'ratingValue':4.8,'reviewCount':10},'hasMenu':{'hasMenuSection':[{'name':'食品','hasMenuItem':([{'identifier':product,'name':name,'description':'','offers':{'price':str(price)}}] if products else [])}]}}

class ServingStateTest(unittest.TestCase):
  def setUp(self):
    self.db=tempfile.NamedTemporaryFile(suffix='.db',delete=False).name; self.c=sqlite3.connect(self.db); self.c.row_factory=sqlite3.Row
  def run_batch(self,n,docs,threshold=3): return apply_snapshot(self.c,docs,f'202609{n:02d}120000',threshold)
  def test_closed_store_does_not_track_deals_or_remove_products(self):
    self.run_batch(1,[doc(100)])
    # Batch 2: Store is closed, returns empty menu
    self.run_batch(2,[doc(products=False, is_open=False)])
    prod = self.c.execute("select status, missing_streak, is_open from products").fetchone()
    self.assertEqual(prod["status"], "active")
    self.assertEqual(prod["missing_streak"], 0) # missing_streak not incremented
    store = self.c.execute("select is_open from stores").fetchone()
    self.assertEqual(store["is_open"], 0)
    # Batch 3: Store closed, returns product with different price
    self.run_batch(3,[doc(50, is_open=False)])
    prod = self.c.execute("select is_open, is_price_deal from products").fetchone()
    self.assertEqual(prod["is_open"], 0)
    self.assertEqual(prod["is_price_deal"], 0)
    self.assertEqual(self.c.execute("select count(*) from events where event_type='PRICE_CHANGED'").fetchone()[0], 0)
  def test_unchanged_and_price_events(self):
    self.run_batch(1,[doc()]); r=self.run_batch(2,[doc()]); self.assertEqual(r['unchanged'],1); self.assertEqual(self.c.execute('select count(*) from events').fetchone()[0],0)
    self.run_batch(3,[doc(100)]); self.run_batch(4,[doc(80)])
    self.assertEqual(self.c.execute("select event_type from events").fetchone()[0],'PRICE_CHANGED')
    self.assertEqual(self.c.execute("select price_novel_vs_previous_3 from products").fetchone()[0],1)
    row=self.c.execute("select reference_price,discount_amount,discount_pct,is_price_deal from products").fetchone()
    self.assertEqual(tuple(row),(100,20,20,1))

  def test_recurring_price_in_previous_three_is_not_a_change(self):
    self.run_batch(1,[doc(100)]); self.run_batch(2,[doc(1)]); self.run_batch(3,[doc(100)]); self.run_batch(4,[doc(1)])
    self.assertEqual(self.c.execute("select count(*) from events where event_type='PRICE_CHANGED'").fetchone()[0],0)
    row=self.c.execute("select price,recent_prices,price_novel_vs_previous_3 from products").fetchone()
    self.assertEqual(row['price'],1)
    self.assertEqual(json.loads(row['recent_prices']),[1.0,100.0,1.0])
    self.assertEqual(row['price_novel_vs_previous_3'],0)
    self.assertEqual(self.c.execute("select is_price_deal from products").fetchone()[0],0)
  def test_less_than_three_previous_prices_is_never_a_price_change(self):
    self.run_batch(1,[doc(100)]); self.run_batch(2,[doc(70)]); self.run_batch(3,[doc(50)])
    self.assertEqual(self.c.execute("select count(*) from events where event_type='PRICE_CHANGED'").fetchone()[0],0)
    self.assertEqual(self.c.execute("select price_novel_vs_previous_3 from products").fetchone()[0],0)
    self.assertEqual(self.c.execute("select is_price_deal from products").fetchone()[0],0)
  def test_store_events_start_after_baseline(self):
    self.run_batch(1,[doc(store='A')]); self.run_batch(2,[doc(store='B')])
    self.assertEqual(self.c.execute("select count(*) from events where event_type='STORE_NEW'").fetchone()[0],0)
    self.assertEqual(self.c.execute("select count(*) from events where event_type='STORE_CHANGED'").fetchone()[0],1)
  def test_missing_threshold_and_reappeared_not_new(self):
    self.run_batch(1,[doc()]); self.run_batch(2,[doc(products=False)]); self.assertEqual(self.c.execute('select status from products').fetchone()[0],'active')
    self.run_batch(3,[doc(products=False)]); self.run_batch(4,[doc(products=False)]); self.assertEqual(self.c.execute('select status from products').fetchone()[0],'inactive')
    r=self.run_batch(5,[doc()]); self.assertEqual(r['new'],0); self.assertEqual(r['reappeared'],1)
  def test_first_seen_and_search(self):
    self.run_batch(1,[doc()]); row=self.c.execute("select p.product_uuid from product_search f join products p using(store_uuid,product_uuid) where product_search match 'costco'").fetchone(); self.assertEqual(row[0],P)
    first=self.c.execute('select first_seen from products').fetchone()[0]; self.run_batch(9,[doc()]); self.assertEqual(self.c.execute('select first_seen from products').fetchone()[0],first)
    anchor=datetime.fromisoformat(first)+timedelta(days=6)
    self.assertEqual(self.c.execute("select count(*) from products where first_seen>=?",((anchor-timedelta(days=7)).isoformat(),)).fetchone()[0],1)
    anchor=datetime.fromisoformat(first)+timedelta(days=8)
    self.assertEqual(self.c.execute("select count(*) from products where first_seen>=?",((anchor-timedelta(days=7)).isoformat(),)).fetchone()[0],0)
  def test_duplicate_or_older_batch_is_rejected(self):
    self.run_batch(2,[doc()])
    with self.assertRaisesRegex(ValueError,'not newer'):
      self.run_batch(2,[doc(80)])
    with self.assertRaisesRegex(ValueError,'not newer'):
      self.run_batch(1,[doc(80)])
    self.assertEqual(self.c.execute('select price from products').fetchone()[0],100)

  def test_incremental_publish_queue_contains_only_changed_rows(self):
    self.run_batch(1,[doc(100)])
    self.c.execute('delete from pending_store_changes')
    self.c.execute('delete from pending_product_changes')
    self.c.commit()
    self.run_batch(2,[doc(100)])
    self.assertEqual(self.c.execute('select count(*) from pending_store_changes').fetchone()[0],0)
    self.assertEqual(self.c.execute('select count(*) from pending_product_changes').fetchone()[0],1)
    self.c.execute('delete from pending_product_changes'); self.c.commit()
    self.run_batch(3,[doc(100)]); self.c.execute('delete from pending_product_changes'); self.c.commit()
    self.run_batch(4,[doc(100)]); self.c.execute('delete from pending_product_changes'); self.c.commit()
    self.run_batch(5,[doc(100)])
    self.assertEqual(self.c.execute('select count(*) from pending_product_changes').fetchone()[0],0)

class RetentionTest(unittest.TestCase):
  def test_only_old_allowlisted_paths(self):
    now=datetime(2026,9,15,tzinfo=timezone.utc); paths=['TaiwanMenuSnapshots/20260701000000/a.tar.gz','TaiwanMenuSnapshots/20260901000000/a.tar.gz','v2/history/events/20260701000000.parquet','v2/current/state.db','serving/current.db']
    delete,keep=retention_plan(paths,now); self.assertEqual(len(delete),2); self.assertIn(paths[1],keep); self.assertNotIn(paths[3],delete); self.assertNotIn(paths[4],delete)

if __name__=='__main__': unittest.main()
