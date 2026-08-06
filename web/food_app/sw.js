/* 食品溯源 APP 离线缓存 Service Worker */
const CACHE = "food-kb-v1";
const CORE = ["/", "/manifest.json", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
});

self.addEventListener("fetch", (e) => {
  // 仅缓存 GET 静态/首页; API 请求走网络(不缓存, 保持数据实时)
  const url = new URL(e.request.url);
  if (e.request.method === "GET" && !url.pathname.startsWith("/api")) {
    e.respondWith(
      caches.match(e.request).then((cached) => cached || fetch(e.request).then((res) => {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, clone));
        return res;
      }).catch(() => caches.match("/")))
    );
  }
});
