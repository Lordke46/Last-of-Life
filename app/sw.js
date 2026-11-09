/* İddia Oran service worker. /sw.js olarak sunulur (kapsam "/"); __VERSION__ ve __PRECACHE__ app/pwa.py'de doldurulur.
   - Statik dosyalar: cache-first; uygulama kabuğu ("/"): network-first, önbellek adı sürümlü (static dosyalar değişince sürüm değişir).
   - /api (GET): network-first; ağ yok/yavaş/5xx ise son kaydedilen yanıt döner ve sayfaya "sw-cache-hit" mesajı gider.
   - Takım logoları (ESPN CDN): cache-first. */
const VERSION = "__VERSION__";
const STATIC_CACHE = `static-${VERSION}`;
const API_CACHE = "public-api-v2"; // yalnız açıkça public olan veri; eski kullanıcı önbelleğini kullanma
const AUTH_REQUIRED = __AUTH_REQUIRED__;
const IMG_CACHE = "img-v1";
const PRECACHE = __PRECACHE__;
const API_MAX = 60;
const IMG_MAX = 150;
const NETWORK_TIMEOUT_MS = 8000; // kayıtlı yanıt varken ağı en fazla bu kadar bekle
const NO_CACHE_API = /^\/api\/(?:update|auth|leagues|results|bulletin)(\/|$)/;

async function clearApiCaches() {
  const keys = await caches.keys();
  await Promise.all(keys.filter((k) => k.startsWith("api-") || k.startsWith("public-api-")).map((k) => caches.delete(k)));
}

function cacheableResponse(res) {
  const control = res.headers.get("cache-control") || "";
  const vary = res.headers.get("vary") || "";
  return res.ok && /\bpublic\b/i.test(control) && !/\b(private|no-store)\b/i.test(control)
    && !/(?:^|,)\s*(cookie|authorization|\*)\s*(?:,|$)/i.test(vary) && !res.headers.has("set-cookie");
}

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(STATIC_CACHE)
      .then((c) => c.addAll(PRECACHE.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => (k.startsWith("static-") && k !== STATIC_CACHE) || k.startsWith("api-") || (AUTH_REQUIRED && k.startsWith("public-api-"))).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (url.origin === location.origin && url.pathname.startsWith("/api/auth/")) {
    e.respondWith((async () => {
      const res = await fetch(req);
      if (req.method !== "GET" && res.ok) await clearApiCaches();
      return res;
    })());
    return;
  }
  if (req.method !== "GET") return;
  if (url.origin !== location.origin) {
    if (req.destination === "image") e.respondWith(cachedImage(e));
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    if (!AUTH_REQUIRED && !NO_CACHE_API.test(url.pathname) && !req.headers.has("authorization") && req.cache !== "no-store") e.respondWith(apiNetworkFirst(e));
  } else if (req.mode === "navigate") {
    if (url.pathname === "/") e.respondWith(appShell(req));
  } else if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    e.respondWith(staticCacheFirst(req));
  }
});

async function staticCacheFirst(req) {
  const cache = await caches.open(STATIC_CACHE);
  return (await cache.match(req)) || fetch(req);
}

async function appShell(req) {
  if (AUTH_REQUIRED) return fetch(req); // oturum kapısını çevrimdışı kabukla atlama
  const cache = await caches.open(STATIC_CACHE);
  try {
    const response = await fetch(new Request(req, {cache:"no-cache"}));
    if (response.ok) {
      await cache.put("/",response.clone());
      return response;
    }
    const cached = await cache.match("/");
    return cached || response;
  } catch (error) {
    const cached = await cache.match("/");
    if (cached) return cached;
    throw error;
  }
}

async function trim(cache, max) {
  const keys = await cache.keys();
  await Promise.all(keys.slice(0, Math.max(0, keys.length - max)).map((k) => cache.delete(k)));
}

async function notifyCacheHit(url, cachedAt) {
  for (const client of await self.clients.matchAll({ type: "window" })) {
    client.postMessage({ type: "sw-cache-hit", url, cachedAt });
  }
}

async function apiNetworkFirst(e) {
  const req = e.request;
  const cache = await caches.open(API_CACHE);
  const cached = await cache.match(req);
  const network = fetch(req).then((res) => {
    if (cacheableResponse(res)) e.waitUntil(cache.put(req, res.clone()).then(() => trim(cache, API_MAX)));
    else if (/\b(private|no-store)\b/i.test(res.headers.get("cache-control") || "") || res.status === 401 || res.status === 403) e.waitUntil(cache.delete(req));
    else if (res.status >= 500 && cached) throw new Error(`HTTP ${res.status}`);
    return res;
  });
  try {
    if (!cached) return await network;
    e.waitUntil(network.catch(() => {})); // zaman aşımında bile ağ yanıtı önbelleği güncellesin
    const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), NETWORK_TIMEOUT_MS));
    return await Promise.race([network, timeout]);
  } catch (err) {
    if (cached) {
      e.waitUntil(notifyCacheHit(req.url, cached.headers.get("date")));
      return cached;
    }
    return new Response(JSON.stringify({ detail: "Çevrimdışısınız ve bu veri henüz kaydedilmemiş." }),
      { status: 503, headers: { "Content-Type": "application/json; charset=utf-8" } });
  }
}

async function cachedImage(e) {
  const req = e.request;
  const cache = await caches.open(IMG_CACHE);
  const hit = await cache.match(req.url);
  if (hit) return hit;
  try { // CORS ile çek: opaque yanıtlar kota şişirir; ESPN CDN "access-control-allow-origin: *" döner
    const res = await fetch(req.url, { mode: "cors", credentials: "omit" });
    if (res.ok) e.waitUntil(cache.put(req.url, res.clone()).then(() => trim(cache, IMG_MAX)));
    return res;
  } catch (err) {
    return fetch(req); // CORS yoksa veya ağ yoksa: önbelleğe almadan olduğu gibi geç
  }
}
