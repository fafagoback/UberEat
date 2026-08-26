/**
 * UberEats Radar - Cloudflare Worker API
 * 直接連線 Cloudflare D1 (Serverless SQL)，為線上前端提供 100% 即時動態查詢。
 *
 * v2.1 - 修正邏輯:
 * - 大特價: 今日價格 vs 過去7天最高價比較
 * - 新店家: 首次出現在 Uber Eats 平台 ≤ 7 天的店家
 * - 老店新菜: 首次出現在 Uber Eats 平台 ≤ 7 天的商品 (所屬店家已存在 > 7 天)
 * - 促銷篩選: 使用正向匹配 (quantity > 1 OR promo_type LIKE '%買%送%')
 * - 全面過濾 price <= 0 的廣告/公告項目
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
      "Cache-Control": "public, max-age=60, s-maxage=300",
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

      // 計算 7 天前的時間戳記 (台灣時間 UTC+8 格式 YYYYMMDDhhmmss)
      const now = new Date();
      const twNow = new Date(now.getTime() + 8 * 60 * 60 * 1000);
      const sevenDaysAgo = new Date(twNow.getTime() - 7 * 24 * 60 * 60 * 1000);
      const sevenDaysAgoStr = [
        sevenDaysAgo.getUTCFullYear(),
        String(sevenDaysAgo.getUTCMonth() + 1).padStart(2, '0'),
        String(sevenDaysAgo.getUTCDate()).padStart(2, '0'),
        String(sevenDaysAgo.getUTCHours()).padStart(2, '0'),
        String(sevenDaysAgo.getUTCMinutes()).padStart(2, '0'),
        String(sevenDaysAgo.getUTCSeconds()).padStart(2, '0'),
      ].join('');

      // 正向匹配促銷條件 SQL 片段
      const PROMO_CONDITION = "(p.quantity > 1 OR (p.promo_type IS NOT NULL AND p.promo_type != '無' AND p.promo_type != ''))";

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

        // 直接查詢計算各項統計，與清單顯示邏輯 100% 一致
        const [storeRes, prodRes, promoRes, newStoreRes, newProdRes] = await Promise.all([
          // 總店家數 (當前批次)
          env.DB.prepare("SELECT COUNT(DISTINCT store_id) as cnt FROM stores WHERE crawled_time = ?").bind(latestBatch).first(),
          // 總商品數 (當前批次，正常商品價格 >= 1)
          env.DB.prepare("SELECT COUNT(*) as cnt FROM products WHERE crawled_time = ? AND price >= 1").bind(latestBatch).first(),
          // 促銷特惠數 (當前批次)
          env.DB.prepare(`SELECT COUNT(*) as cnt FROM products p WHERE p.crawled_time = ? AND p.price >= 1 AND ${PROMO_CONDITION}`).bind(latestBatch).first(),
          // 全新進駐店家數: 首次出現在最新批次的店家
          env.DB.prepare(`
            SELECT COUNT(DISTINCT s1.store_id) as cnt FROM stores s1
            WHERE s1.crawled_time = ?
              AND (SELECT MIN(s0.crawled_time) FROM stores s0 WHERE s0.store_id = s1.store_id) = s1.crawled_time
          `).bind(latestBatch).first(),
          // 老店新推菜色數: 菜品首次出現在最新批次，且所屬店家在更早批次就已存在
          env.DB.prepare(`
            SELECT COUNT(*) as cnt FROM products p1
            WHERE p1.crawled_time = ?
              AND p1.price >= 1
              AND (SELECT MIN(p0.crawled_time) FROM products p0 WHERE p0.product_id = p1.product_id) = p1.crawled_time
              AND (SELECT MIN(s0.crawled_time) FROM stores s0 WHERE s0.store_id = p1.store_id) < p1.crawled_time
          `).bind(latestBatch).first(),
        ]);

        // 大特價計數與最大現省: 今日價 vs 過去7天最高價 (降幅 >= 30% 且 現省 >= $20)
        const discountStatsQuery = `
          SELECT 
            COUNT(*) as cnt,
            COALESCE(MAX(sub.max_7d_eff - sub.curr_eff), 0) as max_savings
          FROM (
            SELECT
              p1.product_id,
              ROUND(p1.price * 1.0 / p1.quantity, 2) as curr_eff,
              (
                SELECT MAX(p0.price * 1.0 / p0.quantity)
                FROM products p0
                WHERE p0.product_id = p1.product_id
                  AND p0.crawled_time >= ?
                  AND p0.crawled_time < p1.crawled_time
                  AND p0.price >= 1
              ) as max_7d_eff
            FROM products p1
            WHERE p1.crawled_time = ?
              AND p1.price >= 1
          ) sub
          WHERE sub.max_7d_eff IS NOT NULL
            AND sub.max_7d_eff > 0
            AND ((sub.max_7d_eff - sub.curr_eff) / sub.max_7d_eff * 100.0) >= 30.0
            AND (sub.max_7d_eff - sub.curr_eff) >= 20.0
        `;
        const discountStatsRes = await env.DB.prepare(discountStatsQuery).bind(sevenDaysAgoStr, latestBatch).first();

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
          big_discounts_count: discountStatsRes?.cnt || 0,
          new_stores_count: newStoreRes?.cnt || 0,
          new_products_count: newProdRes?.cnt || 0,
          promotions_count: promoRes?.cnt || 0,
          max_savings_twd: Math.round(discountStatsRes?.max_savings || 0),
        });
      }

      // -------------------------------------------------------------
      // 2. GET /api/discounts (大特價即時篩選)
      //    邏輯: 今日實質單價 vs 過去 7 天內該商品最高實質單價
      // -------------------------------------------------------------
      if (path === "/api/discounts") {
        const minDiscount = parseFloat(params.get("min_discount") || "30.0");
        const minSavings = parseFloat(params.get("min_savings") || "20.0");
        const keyword = (params.get("q") || "").trim().toLowerCase();
        const category = (params.get("category") || "").trim();
        const sortBy = params.get("sort") || "discount_desc";

        if (!latestBatch) {
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
            p1.price as curr_raw_price,
            p1.quantity as curr_qty,
            p1.promo_type,
            ROUND(p1.price * 1.0 / p1.quantity, 2) as curr_eff_price,
            (
              SELECT MAX(p0.price * 1.0 / p0.quantity)
              FROM products p0
              WHERE p0.product_id = p1.product_id
                AND p0.crawled_time >= ?
                AND p0.crawled_time < p1.crawled_time
                AND p0.price >= 1
            ) as max_7day_eff_price,
            COALESCE(NULLIF(s.order_action_url, ''), s.store_url, (SELECT store_url FROM stores WHERE store_id = p1.store_id LIMIT 1), '') as order_action_url,
            s.rating_value,
            s.review_count,
            s.locality,
            s.street_address
          FROM products p1
          LEFT JOIN stores s ON p1.store_id = s.store_id AND p1.crawled_time = s.crawled_time
          WHERE p1.crawled_time = ?
            AND p1.price >= 1
        `;

        const res = await env.DB.prepare(query).bind(sevenDaysAgoStr, latestBatch).all();
        let items = [];

        for (const r of res.results || []) {
          const currEff = Number(r.curr_eff_price);
          const maxEff = r.max_7day_eff_price !== null ? Number(r.max_7day_eff_price) : null;

          // 跳過無歷史比較資料的商品
          if (maxEff === null || maxEff <= 0) continue;

          const savings = maxEff - currEff;
          const dropPct = (savings / maxEff) * 100.0;

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
              original_price: maxEff,
              current_price: currEff,
              prev_raw_price: maxEff,
              curr_raw_price: Number(r.curr_raw_price),
              prev_qty: 1,
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
      // 3. GET /api/new-stores (全新進駐店家: 首次出現在最新批次)
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
            ) as cuisines,
            (SELECT MIN(s0.crawled_time) FROM stores s0 WHERE s0.store_id = s1.store_id) as first_seen
          FROM stores s1
          WHERE s1.crawled_time = ?
            AND (SELECT MIN(s0.crawled_time) FROM stores s0 WHERE s0.store_id = s1.store_id) = s1.crawled_time
          ORDER BY s1.rating_value DESC, s1.total_menu_items DESC;
        `;
        const res = await env.DB.prepare(query).bind(latestBatch).all();
        const items = (res.results || []).map((d) => ({
          ...d,
          store_url: (d.store_url || "").replace(/&amp;/g, "&"),
          order_action_url: (d.order_action_url || d.store_url || "").replace(/&amp;/g, "&"),
        }));
        return jsonResponse({ status: "success", total: items.length, items });
      }

      // -------------------------------------------------------------
      // 4. GET /api/new-products (老店新菜: 菜品首次出現，所屬店家在更早批次已存在)
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
            s.rating_value,
            (SELECT MIN(p0.crawled_time) FROM products p0 WHERE p0.product_id = p1.product_id) as product_first_seen
          FROM products p1
          JOIN stores s ON p1.store_id = s.store_id AND p1.crawled_time = s.crawled_time
          WHERE p1.crawled_time = ?
            AND p1.price >= 1
            AND (SELECT MIN(p0.crawled_time) FROM products p0 WHERE p0.product_id = p1.product_id) = p1.crawled_time
            AND (SELECT MIN(s0.crawled_time) FROM stores s0 WHERE s0.store_id = p1.store_id) < p1.crawled_time
          ORDER BY p1.store_name, (p1.price * 1.0 / p1.quantity) DESC;
        `;
        const res = await env.DB.prepare(query).bind(latestBatch).all();
        const items = (res.results || []).map((d) => ({
          ...d,
          order_action_url: (d.order_action_url || "").replace(/&amp;/g, "&"),
        }));
        return jsonResponse({ status: "success", total: items.length, items });
      }

      // -------------------------------------------------------------
      // 5. GET /api/promotions (買一送一與促銷特惠專區)
      //    正向匹配: quantity > 1 OR promo_type LIKE '%買%送%' OR promo_type LIKE '%折%'
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
            AND ${PROMO_CONDITION}
            AND p.price >= 1
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

        const sqlWhere = ["p.crawled_time = ?", "p.price >= 1"];
        const sqlParams = [latestBatch];

        if (keyword) {
          // 支援常用品牌/品項中英同義詞與特殊字展開 (例如 dazs / haagen / 哈根達斯)
          const synonymMap = [
            { pattern: /dazs|haagen|häagen|哈根/i, terms: ['dazs', 'haagen', 'häagen', '哈根', '哈根達斯'] },
            { pattern: /movenpick|mövenpick|莫凡彼/i, terms: ['movenpick', 'mövenpick', '莫凡彼'] },
            { pattern: /cold\s*stone|酷聖石/i, terms: ['cold stone', 'coldstone', '酷聖石'] },
            { pattern: /starbucks|星巴克/i, terms: ['starbucks', '星巴克'] },
            { pattern: /mcdonald|麥當勞/i, terms: ['mcdonald', '麥當勞'] },
            { pattern: /kfc|肯德基/i, terms: ['kfc', '肯德基'] },
            { pattern: /coca|coke|可樂|可口可樂/i, terms: ['coca', 'coke', '可樂', '可口可樂'] },
            { pattern: /costco|好市多/i, terms: ['costco', '好市多'] },
            { pattern: /全家|familymart/i, terms: ['全家', 'familymart'] },
            { pattern: /7-11|7-eleven|統一超商/i, terms: ['7-11', '7-eleven', '統一超商'] }
          ];

          let matchedTerms = null;
          for (const s of synonymMap) {
            if (s.pattern.test(keyword)) {
              matchedTerms = s.terms;
              break;
            }
          }

          if (matchedTerms) {
            const orClauses = [];
            for (const t of matchedTerms) {
              orClauses.push("p.product_name LIKE ?");
              orClauses.push("p.store_name LIKE ?");
              orClauses.push("p.description LIKE ?");
              sqlParams.push(`%${t}%`, `%${t}%`, `%${t}%`);
            }
            sqlWhere.push(`(${orClauses.join(" OR ")})`);
          } else {
            const terms = keyword.split(/\s+/).filter(Boolean);
            for (const term of terms) {
              sqlWhere.push("(p.product_name LIKE ? OR p.store_name LIKE ? OR p.description LIKE ?)");
              const kwLike = `%${term}%`;
              sqlParams.push(kwLike, kwLike, kwLike);
            }
          }
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
          // 僅顯示促銷: 嚴格正向匹配過濾 + 排序
          sqlWhere.push("(p.quantity > 1 OR (p.promo_type IS NOT NULL AND p.promo_type != '無' AND p.promo_type != ''))");
          sortClause = "ORDER BY (CASE WHEN (p.quantity > 1 OR (p.promo_type IS NOT NULL AND p.promo_type != '無' AND p.promo_type != '')) THEN 0 ELSE 1 END) ASC, (p.price * 1.0 / p.quantity) ASC, s.rating_value DESC";
        } else if (sortBy === "promo_first") {
          // 優惠活動優先: 促銷商品排在前，無促銷商品排在後
          sortClause = "ORDER BY (CASE WHEN (p.quantity > 1 OR (p.promo_type IS NOT NULL AND p.promo_type != '無' AND p.promo_type != '')) THEN 0 ELSE 1 END) ASC, s.rating_value DESC, (p.price * 1.0 / p.quantity) ASC";
        } else if (sortBy === "price_asc") {
          sortClause = "ORDER BY (p.price * 1.0 / p.quantity) ASC, s.rating_value DESC";
        } else if (sortBy === "price_desc") {
          sortClause = "ORDER BY (p.price * 1.0 / p.quantity) DESC";
        } else if (sortBy === "name_asc") {
          sortClause = "ORDER BY p.product_name ASC";
        } else if (sortBy === "rating_desc") {
          sortClause = "ORDER BY (CASE WHEN s.rating_value IS NOT NULL THEN s.rating_value ELSE 0 END) DESC, (p.price * 1.0 / p.quantity) ASC";
        }

        const whereClause = " WHERE " + sqlWhere.join(" AND ");

        // 總數
        const countQuery = `SELECT COUNT(*) as total FROM products p LEFT JOIN stores s ON p.store_id = s.store_id AND p.crawled_time = s.crawled_time ${whereClause}`;
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
