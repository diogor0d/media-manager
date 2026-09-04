"use strict";

const CACHE_NAME = "media-manager-shell-pwa3";
const SHELL = new Map([
  ["/", "text/html"],
  ["/assets/app.css?v=pwa3", "text/css"],
  ["/assets/app.js?v=pwa3", "text/javascript"],
  ["/assets/logo.svg", "image/svg+xml"],
  ["/favicon.svg", "image/svg+xml"],
  ["/manifest.webmanifest", "application/manifest+json"],
  ["/assets/icon-192.png", "image/png"],
  ["/assets/icon-512.png", "image/png"],
  ["/assets/apple-touch-icon.png", "image/png"],
]);

self.addEventListener("install", (event) => {
  event.waitUntil(cacheShell());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(
      names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name)),
    )).then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/v1/")) return;

  if (request.mode === "navigate" && url.pathname === "/") {
    event.respondWith(networkFirstPage(request));
    return;
  }
  const cacheKey = `${url.pathname}${url.search}`;
  if (SHELL.has(cacheKey)) event.respondWith(cacheFirst(request, SHELL.get(cacheKey)));
});

async function cacheShell() {
  const cache = await caches.open(CACHE_NAME);
  await Promise.all(Array.from(SHELL, async ([path, contentType]) => {
    const response = await fetch(path, { cache: "reload", redirect: "error" });
    if (!isCacheable(response, contentType)) throw new Error(`Invalid application shell response: ${path}`);
    await cache.put(path, response);
  }));
}

async function networkFirstPage(request) {
  try {
    const response = await fetch(request);
    if (isCacheable(response, "text/html")) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put("/", response.clone());
    }
    return response;
  } catch (_) {
    return (await caches.match("/")) || Response.error();
  }
}

async function cacheFirst(request, contentType) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (isCacheable(response, contentType)) {
    const cache = await caches.open(CACHE_NAME);
    await cache.put(request, response.clone());
  }
  return response;
}

function isCacheable(response, expectedType = null) {
  if (!response.ok || response.redirected || new URL(response.url).origin !== self.location.origin) return false;
  return !expectedType || response.headers.get("Content-Type")?.includes(expectedType);
}
