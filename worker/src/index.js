import { createClient } from '@libsql/client/web';

const json = (body, status = 200, origin = '*') => new Response(JSON.stringify(body), {
  status, headers: {'content-type':'application/json; charset=utf-8','access-control-allow-origin':origin,'cache-control':'public, max-age=60'}
});
const positive = (v, d, max=100) => Math.min(max, Math.max(1, Number.parseInt(v || d, 10) || d));
const rows = result => result.rows.map(row => Object.fromEntries(result.columns.map(c=>[c,row[c]])));

export default { async fetch(request, env) {
  const url = new URL(request.url), origin = env.ALLOWED_ORIGIN || '*';
  if (request.method === 'OPTIONS') return new Response(null,{headers:{'access-control-allow-origin':origin,'access-control-allow-methods':'GET'}});
  if (!env.TURSO_DATABASE_URL || !env.TURSO_AUTH_TOKEN) return json({error:'serving database is not configured'},503,origin);
  const db=createClient({url:env.TURSO_DATABASE_URL,authToken:env.TURSO_AUTH_TOKEN});
  const page=positive(url.searchParams.get('page'),1,100000), size=positive(url.searchParams.get('page_size'),50,100), offset=(page-1)*size;
  try {
    if (url.pathname === '/stats') {
      const r=await db.batch([
        "SELECT COUNT(*) total_stores FROM stores WHERE status='active'",
        "SELECT COUNT(*) total_products FROM products WHERE status='active'",
        "SELECT COUNT(*) promotions FROM products WHERE status='active' AND promo_type NOT IN ('','無')",
        "SELECT value latest_batch FROM metadata WHERE key='latest_batch'"
      ]); return json(Object.assign({},...r.map(x=>rows(x)[0]||{})),200,origin);
    }
    const list = async (where, args=[], order='p.first_seen DESC') => {
      const base=` FROM products p JOIN stores s USING(store_uuid) WHERE p.status='active' AND s.status='active' AND ${where}`;
      const [count,data]=await db.batch([
        {sql:'SELECT COUNT(*) total'+base,args},
        {sql:`SELECT p.store_uuid,s.name store_name,p.product_uuid,p.product_name,p.category,p.price,p.effective_price,p.promo_type promotion,s.city,s.rating,p.order_url,p.first_seen${base} ORDER BY ${order} LIMIT ? OFFSET ?`,args:[...args,size,offset]}
      ]); return json({total:Number(rows(count)[0]?.total||0),page,page_size:size,items:rows(data)},200,origin);
    };
    if (url.pathname === '/search') {
      const q=(url.searchParams.get('q')||'').trim(), city=(url.searchParams.get('city')||'').trim();
      if (!q) return json({total:0,page,page_size:size,items:[]},200,origin);
      const sort={price_asc:'p.effective_price ASC',price_desc:'p.effective_price DESC',rating_desc:'s.rating DESC'}[url.searchParams.get('sort')]||'s.rating DESC,p.product_name';
      const match=q.replace(/["']/g,' ').split(/\s+/).filter(Boolean).map(x=>`"${x}"*`).join(' AND ');
      return await list(`EXISTS(SELECT 1 FROM product_search f WHERE f.store_uuid=p.store_uuid AND f.product_uuid=p.product_uuid AND product_search MATCH ?) ${city?'AND s.city=?':''}`,[match,...(city?[city]:[])],sort);
    }
    if (url.pathname === '/new-products') return await list("p.first_seen >= datetime('now', '-' || ? || ' days')",[positive(url.searchParams.get('days'),7,60)]);
    if (url.pathname === '/promotions') return await list("p.promo_type NOT IN ('','無')",[],'p.effective_price ASC');
    if (url.pathname === '/discounts') return await list("EXISTS(SELECT 1 FROM events e WHERE e.store_uuid=p.store_uuid AND e.product_uuid=p.product_uuid AND e.event_type='PRICE_CHANGED' AND e.event_time>=datetime('now','-60 days'))",[],'p.effective_price ASC');
    if (url.pathname === '/new-stores') {
      const days=positive(url.searchParams.get('days'),7,60), [count,data]=await db.batch([
        {sql:"SELECT COUNT(*) total FROM stores WHERE status='active' AND first_seen>=datetime('now','-'||?||' days')",args:[days]},
        {sql:"SELECT store_uuid,name store_name,address,city,locality,rating,review_count,order_url,first_seen FROM stores WHERE status='active' AND first_seen>=datetime('now','-'||?||' days') ORDER BY first_seen DESC LIMIT ? OFFSET ?",args:[days,size,offset]}
      ]); return json({total:Number(rows(count)[0]?.total||0),page,page_size:size,items:rows(data)},200,origin);
    }
    const m=url.pathname.match(/^\/product\/([^/]+)\/([^/]+)\/history$/);
    if (m) { const days=positive(url.searchParams.get('days'),60,60); const r=await db.execute({sql:"SELECT event_time,event_type,old_state,new_state FROM events WHERE store_uuid=? AND product_uuid=? AND event_time>=datetime('now','-'||?||' days') ORDER BY event_time",args:[decodeURIComponent(m[1]),decodeURIComponent(m[2]),days]}); return json({items:rows(r)},200,origin); }
    return json({error:'not found'},404,origin);
  } catch(e) { return json({error:'query failed',detail:String(e.message||e)},500,origin); }
}};
