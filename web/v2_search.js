/* Thin GitHub Pages client for the Cloudflare Worker serving API. */
(function(){
  'use strict';
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const api=async(path,params={})=>{const base=String(window.UBER_RADAR_CONFIG?.WORKER_API_BASE_URL||'').replace(/\/$/,'');if(!/^https:\/\//.test(base)||base.includes('YOUR-SUBDOMAIN'))throw new Error('Worker API is not configured');const u=new URL(base+path);Object.entries(params).forEach(([k,v])=>v!==''&&v!=null&&v!=='全部'&&u.searchParams.set(k,v));const r=await fetch(u,{headers:{accept:'application/json'}});if(!r.ok)throw new Error(`API ${r.status}`);return r.json();};
  const product=x=>({...x,store_id:x.store_uuid,product_id:x.product_uuid,category_name:x.category,eff_price:x.effective_price,promo_type:x.promotion,rating_value:x.rating,order_action_url:x.order_url,current_price:x.price});
  async function install(){
    for(let i=0;i<100&&(typeof APP_STATE==='undefined'||typeof renderGlobalProducts!=='function');i++)await wait(50);if(typeof APP_STATE==='undefined')return;
    if(String(window.UBER_RADAR_CONFIG?.WORKER_API_BASE_URL||'').includes('YOUR-SUBDOMAIN')) return;
    window.UBER_RADAR_SERVING_API=true;
    fetchGlobalProducts=async(page=1)=>{const s=APP_STATE.filters,d=await api('/search',{q:s.globalSearch,page,page_size:PAGE_SIZE,city:s.globalCity,sort:s.globalSort});APP_STATE.globalProducts=d.items.map(product);APP_STATE.globalPage=d.page;APP_STATE.globalTotalItems=d.total;APP_STATE.globalTotalPages=Math.max(1,Math.ceil(d.total/PAGE_SIZE));renderGlobalProducts();};
    fetchNewProducts=async(page=1)=>{const d=await api('/new-products',{days:7,page,page_size:PAGE_SIZE});APP_STATE.newProducts=d.items.map(product);APP_STATE.filteredProducts=APP_STATE.newProducts;APP_STATE.productsPage=page;renderNewProducts();};
    fetchNewStores=async(page=1)=>{const d=await api('/new-stores',{days:7,page,page_size:PAGE_SIZE});APP_STATE.newStores=d.items.map(x=>({...x,rating_value:x.rating,street_address:x.address}));APP_STATE.filteredStores=APP_STATE.newStores;APP_STATE.storesPage=page;renderNewStores();};
    fetchPromotions=async(page=1)=>{const d=await api('/promotions',{page,page_size:PAGE_SIZE});APP_STATE.promotions=d.items.map(product);APP_STATE.filteredPromotions=APP_STATE.promotions;APP_STATE.promosPage=page;renderPromotions();};
    fetchDiscounts=async(page=1)=>{const d=await api('/discounts',{page,page_size:PAGE_SIZE});APP_STATE.rawDiscounts=d.items.map(product);APP_STATE.discounts=APP_STATE.rawDiscounts;APP_STATE.discountsPage=page;renderDiscounts();};
    try{const stats=await api('/stats');updateStatsUI(stats);await Promise.all([fetchDiscounts(1),fetchNewStores(1),fetchNewProducts(1),fetchPromotions(1)]);}catch(e){console.error('[Serving API]',e);}
  } install();
})();
