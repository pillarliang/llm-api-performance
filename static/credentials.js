/* Browser-local credentials, scoped to a source and its exact API endpoint. */
"use strict";
window.providerKeyStore = (() => {
  const prefix = "llm-performance.provider-key.v1:";
  let available = true;
  function endpoint(provider) {
    if (provider.mode !== "custom") return null;
    const value = provider.base_url.trim();
    if (!value || /\s/.test(value)) return null;
    try {
      const url = new URL(value.includes("://") ? value : `https://${value}`);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) return null;
      let path = url.pathname.replace(/\/+$/, "");
      if (url.hostname.endsWith(".aliyuncs.com")) {
        if (path.endsWith("/api/v1")) path = path.slice(0, -7) + "/compatible-mode/v1";
        else if (!path) path = "/compatible-mode/v1";
      }
      return url.origin + path;
    } catch { return null; }
  }
  // Recover source metadata from keys saved by older versions without workspace autosave.
  function savedProviders() {
    const candidates = new Map();
    try {
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (!key?.startsWith(prefix) || !localStorage.getItem(key)) continue;
        try {
          const [id, base] = JSON.parse(key.slice(prefix.length));
          if (typeof id !== "string" || typeof base !== "string" || !id) continue;
          const provider = {id, mode:"custom", base_url:base};
          if (endpoint(provider) !== base) continue;
          provider.name = new URL(base).hostname === "api.deepseek.com" ? "DeepSeek" : new URL(base).hostname;
          // An ID with several saved endpoints is ambiguous; do not guess which one to use.
          candidates.set(id, candidates.has(id) ? null : provider);
        } catch {}
      }
      return [...candidates.values()].filter(Boolean);
    } catch { available = false; return []; }
  }
  function slot(provider) {
    const base = endpoint(provider);
    return base ? prefix + JSON.stringify([provider.id, base]) : null;
  }
  function suggestBase(provider) {
    const base = endpoint(provider);
    if (!base) return null;
    const url = new URL(base);
    const match = url.pathname.match(/\/(?:chat\/completions|completions|models|responses)(\/v1)?$/);
    if (!match) return null;
    let path = url.pathname.slice(0, match.index);
    if (url.hostname === "api.deepseek.com" && !path) path = "";
    else if (match[1] && !path.endsWith("/v1")) path += "/v1";
    return url.origin + path;
  }
  function read(provider) {
    const key = slot(provider);
    if (!key) return "";
    try { return localStorage.getItem(key) || ""; }
    catch { available = false; return ""; }
  }
  function write(provider, value) {
    const key = slot(provider);
    if (!key) return false;
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
      available = true;
      return true;
    } catch { available = false; return false; }
  }
  function clear(id) {
    try {
      const keys = [];
      for (let index = 0; index < localStorage.length; index++) {
        const key = localStorage.key(index);
        if (!key?.startsWith(prefix)) continue;
        try { if (JSON.parse(key.slice(prefix.length))[0] === id) keys.push(key); } catch {}
      }
      keys.forEach(key => localStorage.removeItem(key));
      available = true;
      return true;
    } catch { available = false; return false; }
  }
  return {endpoint, slot, suggestBase, read, write, clear, savedProviders, get available() {return available;}};
})();
