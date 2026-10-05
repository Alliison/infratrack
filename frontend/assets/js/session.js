/* TrackInfra — o que as páginas dividem sobre sessão e dispositivo.

   Três chaves no navegador:
   - TV_KEY      token da TV (só numa TV);
   - DEVICE_KEY  qual TV este navegador é. Não é segredo: vai junto ao pedir
                 um QR novo para, depois de um restart, voltar a ser a MESMA TV
                 (nome e configuração);
   - CTL_KEY     token de controle do celular que gerencia a conta.

   `?tv=2` na URL separa as chaves da TV: duas "TVs" no mesmo navegador, para
   testar no dev sem precisar de outro aparelho.
   `?watch=<id>` abre o mosaico/SGI como ESPELHO de uma TV, no celular: usa o
   token de controle e a configuração daquela TV, e não se apresenta como TV. */
const TI = (() => {
  const qs = new URLSearchParams(location.search);
  const slot = qs.get("tv");
  const sfx = slot ? `_${slot}` : "";
  const watch = qs.get("watch");

  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
    del(k) { try { localStorage.removeItem(k); } catch (_) {} },
  };

  const TV_KEY = "trackinfra_token" + sfx;
  const DEVICE_KEY = "trackinfra_device" + sfx;
  const CTL_KEY = "trackinfra_ctl";

  /* ---- "Identificar": nome da TV em tela cheia por alguns segundos ---- */
  function identify(name) {
    let el = document.getElementById("ti-identify");
    if (!el) {
      el = document.createElement("div");
      el.id = "ti-identify";
      document.body.appendChild(el);
    }
    el.textContent = name || "TV";
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 10000);
  }

  return {
    TV_KEY, DEVICE_KEY, CTL_KEY, ls, watch,
    // Token que esta página usa: o da TV, ou o do celular no modo espelho.
    tokenKey: watch ? CTL_KEY : TV_KEY,
    // Repassa ?tv / ?watch para o iframe do SGI (mesma TV, mesmas chaves).
    passQuery: (() => {
      const p = new URLSearchParams();
      if (slot) p.set("tv", slot);
      if (watch) p.set("watch", watch);
      const s = p.toString();
      return s ? "?" + s : "";
    })(),
    // `?device=` só no espelho: a TV de verdade é identificada pelo token.
    devParam(prefix = "?") { return watch ? `${prefix}device=${encodeURIComponent(watch)}` : ""; },
    auth(token) { return { Authorization: `Bearer ${token}` }; },

    /* 401 com este header = desconectada de propósito (pelo celular, ou senha
       trocada): vai direto para o QR, sem a tolerância de blip. */
    revoked(r) { return r.status === 401 && r.headers.get("X-Session-Revoked") === "1"; },

    async newQr() {
      const device_id = ls.get(DEVICE_KEY);
      const r = await fetch("/api/auth/session", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id }),
      });
      return r.json();
    },

    authorized(s) {
      ls.set(TV_KEY, s.access_token);
      if (s.device_id) ls.set(DEVICE_KEY, s.device_id);
    },

    /* TV que já estava logada antes desta versão não sabe quem é: pergunta. */
    async learnDevice(token) {
      if (watch || ls.get(DEVICE_KEY)) return;
      try {
        const r = await fetch("/api/me", { headers: { Authorization: `Bearer ${token}` } });
        if (r.ok) { const me = await r.json(); if (me.device_id) ls.set(DEVICE_KEY, me.device_id); }
      } catch (_) {}
    },

    /* Conta ao backend o que a TV está mostrando (é o que o painel do celular
       exibe) e executa o que estiver pendente para ela. Nunca no espelho. */
    async heartbeat(token, state) {
      if (watch || !token) return;
      try {
        const r = await fetch("/api/devices/me/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify({ ...state, viewport: `${innerWidth}×${innerHeight}` }),
        });
        if (!r.ok) return;
        const { commands = [], name } = await r.json();
        for (const c of commands) {
          if (c === "identify") identify(name);
          if (c === "reload") location.reload();
        }
      } catch (_) { /* rede: o próximo ciclo manda de novo */ }
    },
    identify,
  };
})();
