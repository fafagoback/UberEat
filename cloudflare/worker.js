/**
 * UberEats Radar - Cloudflare Worker API
 * 直接連線 Cloudflare D1 (Serverless SQL)，為線上前端提供 100% 即時動態查詢。
 */

export default {
  async fetch(request, env, ctx) {
    // 處理 CORS Preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
          "Access-Control-Max-Age": "86400",
        },
      });
    }

    const url = new URL(request.url);
    const path = url.pathname;
    const params = url.searchParams;

    // 基本安全防護與 Header
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "public, max-age=60, s-maxage=300", // 快取 1~5 分鐘提升極速體驗
    };

    const jsonResponse = (data, status = 200) => {
      return new Response(JSON.stringify(data), {
        status,
        headers: corsHeaders,
      });
    };

    const errorResponse = (msg, status = 500) => {
      return new Response(JSON.stringify({ status: "error", message: msg }), {
        status,
        headers: corsHeaders,
      });
    };

    if (!env.DB) {
      return errorResponse("Cloudflare D1 binding (env.DB) is missing", 500);
    }

    try {
      // 取得最新與前一次批次
      const batchQuery = await env.DB.prepare(
        "SELECT DISTINCT crawled_time FROM products ORDER BY crawled_time DESC LIMIT 2"
      ).all();
      const batches = (batchQuery.results || []).map((r) => r.crawled_time);
      const latestBatch = batches[0] || "";
      const prevBatch = batches[1] || "";

      // -------------------------------------------------------------
      // 1. GET /api/stats (系統概覽統計)
      // -------------------------------------------------------------
      if (path === "/api/stats") {
        if (!latestBatch) {
          return jsonResponse({
            status: "success",
            latest_batch: "",
            total_stores: 0,
            total_products: 0,
            big_discounts_count: 0,
            new_stores_count: 0,
            new_products_count: 0,
            promotions_count: 0,
          });
        }

        const [storeRes, prodRes, alertRes, promoRes] = await Promise.all([
          env.DB.prepare("SELECT COUNT(DISTINCT store_id) as cnt FROM stores WHERE crawled_time = ?").bind(latestBatch).first(),
          env.DB.prepare("SELECT COUNT(*) as cnt FROM products WHERE crawled_time = ? AND price > 0").bind(latestBatch).first(),
          env.DB.prepare("SELECT alert_type, COUNT(*) as cnt FROM alerts_history WHERE crawled_time = ? GROUP BY alert_type").bind(latestBatch).all(),
          env.DB.prepare("SELECT COUNT(*) as cnt FROM products WHERE crawled_time = ? AND (quantity > 1 OR (promo_type != '無' AND promo_type != '' AND promo_type IS NOT NULL)) AND price > 0").bind(latestBatch).first(),
        ]);

        const alertCounts = {};
        for (const row of alertRes.results || []) {
          alertCounts[row.alert_type] = row.cnt;
        }

        let dateFmt = "";
        if (latestBatch.length === 14) {
          dateFmt = `${latestBatch.slice(0, 4)}-${latestBatch.slice(4, 6)}-${latestBatch.slice(6, 8)} ${latestBatch.slice(8, 10)}:${latestBatch.slice(10, 12)}`;
        }

        return jsonResponse({
          status: "success",
          latest_batch: latestBatch,
          latest_batch_formatted: dateFmt,
          prev_batch: prevBatch,
          batches: batches,
          total_stores: storeRes?.cnt || 0,
          total_products: prodRes?.cnt || 0,
          big_discounts_count: alertCounts["BIG_DISCOUNT"] || 0,
          new_stores_count: alertCounts["NEW_STORE"] || 0,
          new_products_count: alertCounts["NEW_PRODUCT"] || 0,
          promotions_count: promoRes?.cnt || 0,
        });
      }

      // -------------------------------------------------------------
      // 2. GET /api/discounts (大特價即時篩選)
      // -------------------------------------------------------------
      if (path === "/api/discounts") {
        const minDiscount = parseFloat(params.get("min_discount") || "30.0");
        const minSavings = parseFloat(params.get("min_savings") || "20.0");
        const keyword = (params.get("q") || "").trim().toLowerCase();
        const category = (params.get("category") || "").trim();
        const sortBy = params.get("sort") || "discount_desc";

        if (!prevBatch || !latestBatch) {
          return jsonResponse({ status: "success", total: 0, items: [] });
        }

        const query = `
          SELECT 
            p1.product_id,
            p1.store_id,
            p1.store_name,
            p1.product_name,
            p1.category_name,
            p1.description,
            ROUND(p0.price * 1.0 / p0.quantity, 2) as prev_eff_price,
            ROUND(p1.price * 1.0 / p1.quantity, 2) as curr_eff_price,
            p0.price as prev_raw_price,
            p1.price as curr_raw_price,
            p0.quantity as prev_qty,
            p1.quantity as curr_qty,
            p1.promo_type,
            COALESCE(NULLIF(s.order_action_url, ''), s.store_url, (SELECT store_url FROM stores WHERE store_id = p1.store_id LIMIT 1), '') as order_action_url,
            s.rating_value,
            s.review_count,
            s.locality,
            s.street_address
          FROM products p1
          JOIN products p0 ON p1.product_id = p0.product_id AND p1.store_id = p0.store_id
          LEFT JOIN stores s ON p1.store_id = s.store_id AND p1.crawled_time = s.crawled_time
          WHERE p0.crawled_time = ?
            AND p1.crawled_time = ?
            AND p0.price > 0
            AND p1.price > 0
        `;

        const res = await env.DB.prepare(query).bind(prevBatch, latestBatch).all();
        let items = [];

        for (const r of res.results || []) {
          const prevEff = Number(r.prev_eff_price);
          const currEff = Number(r.curr_eff_price);
          if (prevEff <= 0) continue;

          const savings = prevEff - currEff;
          const dropPct = (savings / prevEff) * 100.0;

          if (dropPct >= minDiscount && savings >= minSavings) {
            const pName = r.product_name || "";
            const sName = r.store_name || "";
            const catName = r.category_name || "未分類";

            if (keyword && !pName.toLowerCase().includes(keyword) && !sName.toLowerCase().includes(keyword)) {
              continue;
            }
            if (category && category !== "全部" && !catName.includes(category)) {
              continue;
            }

            items.push({
              product_id: r.product_id,
              store_id: r.store_id,
              store_name: sName,
              product_name: pName,
              category_name: catName,
              description: r.description || "",
              original_price: prevEff,
              current_price: currEff,
              prev_raw_price: Number(r.prev_raw_price),
              curr_raw_price: Number(r.curr_raw_price),
              prev_qty: Number(r.prev_qty),
              curr_qty: Number(r.curr_qty),
              discount_pct: Math.round(dropPct * 10) / 10,
              savings_amount: Math.round(savings * 10) / 10,
              promo_type: r.promo_type,
              order_action_url: (r.order_action_url || "").replace(/&amp;/g, "&"),
              rating_value: r.rating_value !== null ? Number(r.rating_value) : null,
              review_count: r.review_count !== null ? Number(r.review_count) : null,
              locality: r.locality || "",
              street_address: r.street_address || "",
              crawled_time: latestBatch,
            });
          }
        }

        if (sortBy === "discount_desc") {
          items.sort((a, b) => b.discount_pct - a.discount_pct || b.savings_amount - a.savings_amount);
        } else if (sortBy === "savings_desc") {
          items.sort((a, b) => b.savings_amount - a.savings_amount || b.discount_pct - a.discount_pct);
        } else if (sortBy === "price_asc") {
          items.sort((a, b) => a.current_price - b.current_price);
        } else if (sortBy === "price_desc") {
          items.sort((a, b) => b.current_price - a.current_price);
        }

        return jsonResponse({ status: "success", total: items.length, items });
      }

      // -------------------------------------------------------------
      // 3. GET /api/new-stores (全新店家)
      // -------------------------------------------------------------
      if (path === "/api/new-stores") {
        const query = `
          SELECT 
            s1.store_id,
            s1.store_name,
            s1.store_type,
            s1.store_url,
            s1.rating_value,
            s1.review_count,
            s1.price_range,
            s1.telephone,
            s1.region,
            s1.locality,
            s1.street_address,
            COALESCE(NULLIF(s1.order_action_url, ''), s1.store_url, '') as order_action_url,
            s1.total_menu_items,
            s1.crawled_time,
            (
              SELECT GROUP_CONCAT(cuisine_name, '、')
              FROM store_cuisines sc
              WHERE sc.store_id = s1.store_id AND sc.crawled_time = s1.crawled_time
            ) as cuisines
          FROM stores s1
          WHERE s1.crawled_time = ?
            AND s1.store_id NOT IN (
              SELECT DISTINCT s0.store_id
              FROM stores s0
              WHERE s0.crawled_time < ?
            )
          ORDER BY s1.rating_value DESC, s1.total_menu_items DESC;
        `;
        const res = await env.DB.prepare(query).bind(latestBatch, latestBatch).all();
        const items = (res.results || []).map((d) => ({
          ...d,
          store_url: (d.store_url || "").replace(/&amp;/g, "&"),
          order_action_url: (d.order_action_url || d.store_url || "").replace(/&amp;/g, "&"),
        }));
        return jsonResponse({ status: "success", total: items.length, items });
      }

      // -------------------------------------------------------------
      // 4. GET /api/new-products (老店新品)
      // -------------------------------------------------------------
      if (path === "/api/new-products") {
        const query = `
          SELECT 
            p1.product_id,
            p1.store_id,
            p1.store_name,
            p1.category_name,
            p1.product_name,
            p1.price,
            p1.currency,
            p1.description,
            p1.promo_type,
            p1.quantity,
            ROUND(p1.price * 1.0 / p1.quantity, 2) as eff_price,
            COALESCE(NULLIF(s.order_action_url, ''), s.store_url, (SELECT store_url FROM stores WHERE store_id = p1.store_id LIMIT 1), '') as order_action_url,
            s.rating_value
          FROM products p1
          JOIN stores s ON p1.store_id = s.store_id AND p1.crawled_time = s.crawled_time
          WHERE p1.crawled_time = ?
            AND p1.price > 0
            AND p1.product_id NOT IN (
              SELECT DISTINCT p0.product_id
              FROM products p0
              WHERE p0.crawled_time < ?
            )
            AND p1.store_id IN (
              SELECT DISTINCT s0.store_id
              FROM stores s0
              WHERE s0.crawled_time < ?
            )
          ORDER BY p1.store_name, (p1.price * 1.0 / p1.quantity) DESC;
        `;
        const res = await env.DB.prepare(query).bind(latestBatch, latestBatch, latestBatch).all();
        const items = (res.results || []).map((d) => ({
          ...d,
          order_action_url: (d.order_action_url || "").replace(/&amp;/g, "&"),
        }));
        return jsonResponse({ status: "success", total: items.length, items });
      }

      // -------------------------------------------------------------
      // 5. GET /api/promotions (買一送一與促銷特惠專區)
      // -------------------------------------------------------------
      if (path === "/api/promotions") {
        const query = `
          SELECT 
            p.product_id,
            p.store_id,
            p.store_name,
            p.category_name,
            p.product_name,
            p.price,
            p.quantity,
            p.promo_type,
            ROUND(p.price * 1.0 / p.quantity, 2) as eff_price,
            p.description,
            COALESCE(NULLIF(s.order_action_url, ''), s.store_url, (SELECT store_url FROM stores WHERE store_id = p.store_id LIMIT 1), '') as order_action_url,
            s.rating_value,
            s.locality
          FROM products p
          LEFT JOIN stores s ON p.store_id = s.store_id AND p.crawled_time = s.crawled_time
          WHERE p.crawled_time = ?
            AND (p.quantity > 1 OR (p.promo_type != '無' AND p.promo_type != '' AND p.promo_type IS NOT NULL))
            AND p.price > 0
          ORDER BY (CASE WHEN p.quantity > 1 THEN 0 ELSE 1 END) ASC, (p.price * 1.0 / p.quantity) ASC;
        `;
        const res = await env.DB.prepare(query).bind(latestBatch).all();
        const items = (res.results || []).map((d) => ({
          ...d,
          order_action_url: (d.order_action_url || "").replace(/&amp;/g, "&"),
        }));
        return jsonResponse({ status: "success", total: items.length, items });
      }

      // -------------------------------------------------------------
      // 6. GET /api/products (全品庫分頁檢索)
      // -------------------------------------------------------------
      if (path === "/api/products") {
        const keyword = (params.get("q") || "").trim();
        const category = (params.get("category") || "").trim();
        const storeId = (params.get("store_id") || "").trim();
        const minPrice = parseFloat(params.get("min_price") || "0");
        const maxPrice = parseFloat(params.get("max_price") || "99999");
        const page = parseInt(params.get("page") || "1", 10);
        const limit = parseInt(params.get("limit") || "24", 10);
        const sortBy = params.get("sort") || "rating_desc";

        const sqlWhere = ["p.crawled_time = ?", "p.price > 0"];
        const sqlParams = [latestBatch];

        if (keyword) {
          sqlWhere.push("(p.product_name LIKE ? OR p.store_name LIKE ? OR p.description LIKE ?)");
          const kwLike = `%${keyword}%`;
          sqlParams.push(kwLike, kwLike, kwLike);
        }
        if (category && category !== "全部") {
          sqlWhere.push("p.category_name LIKE ?");
          sqlParams.push(`%${category}%`);
        }
        if (storeId) {
          sqlWhere.push("p.store_id = ?");
          sqlParams.push(storeId);
        }
        if (minPrice > 0) {
          sqlWhere.push("(p.price * 1.0 / p.quantity) >= ?");
          sqlParams.push(minPrice);
        }
        if (maxPrice < 99999) {
          sqlWhere.push("(p.price * 1.0 / p.quantity) <= ?");
          sqlParams.push(maxPrice);
        }

        let sortClause = "ORDER BY s.rating_value DESC, (p.price * 1.0 / p.quantity) ASC";
        if (sortBy === "promo_only") {
          sqlWhere.push("((p.quantity > 1) OR (p.promo_type != '無' AND p.promo_type != '' AND p.promo_type IS NOT NULL))");
          sortClause = "ORDER BY (CASE WHEN p.quantity > 1 THEN 0 ELSE 1 END) ASC, (p.price * 1.0 / p.quantity) ASC, s.rating_value DESC";
        } else if (sortBy === "promo_first") {
          sortClause = "ORDER BY (CASE WHEN (p.quantity > 1 OR (p.promo_type != '無' AND p.promo_type != '' AND p.promo_type IS NOT NULL)) THEN 0 ELSE 1 END) ASC, (p.price * 1.0 / p.quantity) ASC, s.rating_value DESC";
        } else if (sortBy === "price_asc") {
          sortClause = "ORDER BY (p.price * 1.0 / p.quantity) ASC";
        } else if (sortBy === "price_desc") {
          sortClause = "ORDER BY (p.price * 1.0 / p.quantity) DESC";
        } else if (sortBy === "name_asc") {
          sortClause = "ORDER BY p.product_name ASC";
        } else if (sortBy === "rating_desc") {
          sortClause = "ORDER BY s.rating_value DESC, (p.price * 1.0 / p.quantity) ASC";
        }

        const whereClause = " WHERE " + sqlWhere.join(" AND ");

        // 總數
        const countQuery = `SELECT COUNT(*) as total FROM products p ${whereClause}`;
        const countRes = await env.DB.prepare(countQuery).bind(...sqlParams).first();
        const total = countRes?.total || 0;

        const offset = (page - 1) * limit;
        const dataQuery = `
          SELECT 
            p.product_id,
            p.store_id,
            p.store_name,
            p.category_name,
            p.product_name,
            p.price,
            p.quantity,
            p.promo_type,
            ROUND(p.price * 1.0 / p.quantity, 2) as eff_price,
            p.description,
            COALESCE(NULLIF(s.order_action_url, ''), s.store_url, (SELECT store_url FROM stores WHERE store_id = p.store_id LIMIT 1), '') as order_action_url,
            s.rating_value,
            s.review_count,
            s.locality
          FROM products p
          LEFT JOIN stores s ON p.store_id = s.store_id AND p.crawled_time = s.crawled_time
          ${whereClause}
          ${sortClause}
          LIMIT ? OFFSET ?;
        `;

        const dataRes = await env.DB.prepare(dataQuery).bind(...sqlParams, limit, offset).all();
        const items = (dataRes.results || []).map((d) => ({
          ...d,
          order_action_url: (d.order_action_url || "").replace(/&amp;/g, "&"),
        }));

        return jsonResponse({
          status: "success",
          total,
          page,
          limit,
          total_pages: Math.ceil(total / limit) || 1,
          items,
        });
      }

      // -------------------------------------------------------------
      // 7. GET /api/history (商品價格趨勢)
      // -------------------------------------------------------------
      if (path === "/api/history") {
        const productId = (params.get("product_id") || "").trim();
        if (!productId) {
          return errorResponse("product_id is required", 400);
        }

        const query = `
          SELECT 
            p.product_id,
            p.crawled_time,
            p.store_name,
            p.product_name,
            p.price,
            p.quantity,
            p.promo_type,
            ROUND(p.price * 1.0 / p.quantity, 2) as eff_price
          FROM products p
          WHERE p.product_id = ?
          ORDER BY p.crawled_time ASC;
        `;
        const res = await env.DB.prepare(query).bind(productId).all();
        return jsonResponse({
          status: "success",
          product_id: productId,
          history: res.results || [],
        });
      }

      return errorResponse(`Endpoint not found: ${path}`, 404);
    } catch (err) {
      return errorResponse(err.message || String(err), 500);
    }
  },
};
