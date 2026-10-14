"use strict";

/* Källspelare: videon spelas direkt från YouTube, VK Video eller RuTube, inbäddad i appen.
   Inget ljud eller video sparas lokalt — vi styr bara spelaren och läser av tiden. */

(function () {
  function embedFor(meta) {
    if (!meta || !meta.video_id) return null;
    const ex = String(meta.extractor || "").toLowerCase();
    const id = String(meta.video_id);
    if (ex.startsWith("youtube") && /^[\w-]{6,}$/.test(id)) return { kind: "youtube", id };
    if (ex.startsWith("rutube") && /^[a-f0-9]{16,}$/i.test(id)) return { kind: "rutube", id };
    if (ex === "vk") {
      const m = id.match(/^(-?\d+)_(\d+)$/);
      if (m) return { kind: "vk", oid: m[1], vid: m[2] };
    }
    return null;
  }

  /* Gemensam klocka för spelare som bara rapporterar tiden några gånger i sekunden. */
  function clockState() {
    return {
      t: 0, at: 0, playing: false, dur: 0, rate: 1,
      set(t) { this.t = t; this.at = performance.now(); },
      now() { return this.playing ? this.t + ((performance.now() - this.at) / 1000) * this.rate : this.t; },
    };
  }

  function iframe(src) {
    const f = document.createElement("iframe");
    f.src = src;
    f.allow = "autoplay; encrypted-media; fullscreen; picture-in-picture; screen-wake-lock";
    f.allowFullscreen = true;
    f.referrerPolicy = "strict-origin-when-cross-origin";
    f.title = "Videospelare";
    return f;
  }

  /* ---------------------------------------------------------------- YouTube */

  let ytReady = null;
  function loadYT() {
    if (ytReady) return ytReady;
    ytReady = new Promise((resolve) => {
      if (window.YT && window.YT.Player) return resolve();
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => { if (prev) prev(); resolve(); };
      const s = document.createElement("script");
      s.src = "https://www.youtube.com/iframe_api";
      document.head.append(s);
    });
    return ytReady;
  }

  function youtube(box, emb, on) {
    let p = null, ready = false, pending = null, playing = false;
    const holder = document.createElement("div");
    box.append(holder);
    loadYT().then(() => {
      p = new window.YT.Player(holder, {
        host: "https://www.youtube-nocookie.com",
        videoId: emb.id,
        width: "100%",
        height: "100%",
        playerVars: { playsinline: 1, rel: 0, modestbranding: 1, origin: location.origin },
        events: {
          onReady: () => {
            ready = true;
            on.ready(p.getDuration());
            if (pending) { pending(); pending = null; }
          },
          onStateChange: (e) => {
            if (e.data === 1) { playing = true; on.play(); }
            else if (e.data === 2 || e.data === 0) { playing = false; on.pause(); }
          },
          // 101/150 = ägaren tillåter inte inbäddning, 100 = borttagen, 2/5 = fel i spelaren.
          onError: (e) => on.error(`youtube:${e.data}`),
        },
      });
    });
    const when = (fn) => (ready ? fn() : (pending = fn));
    return {
      canRate: true,
      play: () => when(() => p.playVideo()),
      pause: () => when(() => p.pauseVideo()),
      seek: (t) => when(() => p.seekTo(Math.max(0, t), true)),
      setRate: (r) => when(() => p.setPlaybackRate(r)),
      time: () => (ready ? p.getCurrentTime() : 0),
      duration: () => (ready ? p.getDuration() : 0),
      paused: () => !playing,
      destroy: () => { try { p && p.destroy(); } catch { /* */ } box.replaceChildren(); },
    };
  }

  /* ---------------------------------------------------------------- VK Video */

  function vk(box, emb, on) {
    const c = clockState();
    const f = iframe(`https://vkvideo.ru/video_ext.php?oid=${emb.oid}&id=${emb.vid}&hd=2&js_api=1`);
    box.append(f);
    let inited = false, origin = "*";
    const queue = [];
    const post = (msg) => {
      if (!inited && msg.method !== "init") { queue.push(msg); return; }
      f.contentWindow && f.contentWindow.postMessage(msg, origin);
    };
    const onMsg = (e) => {
      if (e.source !== f.contentWindow || !e.data || typeof e.data !== "object") return;
      origin = e.origin;
      const d = e.data;
      if (typeof d.duration === "number" && d.duration) c.dur = d.duration;
      if (typeof d.time === "number") c.set(d.time);
      switch (d.event) {
        case "inited":
          inited = true;
          on.ready(c.dur);
          while (queue.length) post(queue.shift());
          break;
        case "started": case "resumed":
          c.set(c.t); c.playing = true; on.play(); break;
        case "paused": case "ended":
          c.playing = false; on.pause(); break;
        case "error":
          c.playing = false; on.error(`vk:${d.errorCode || ""}`); break;
        case "seeked":
          on.seeked && on.seeked(); break;
      }
    };
    window.addEventListener("message", onMsg);
    // Spelaren svarar först när den laddat; fråga tills den hälsar.
    let tries = 0;
    const hello = setInterval(() => {
      if (inited || tries++ > 40) { clearInterval(hello); return; }
      post({ method: "init" });
    }, 400);
    return {
      canRate: false,
      play: () => post({ method: "play" }),
      pause: () => post({ method: "pause" }),
      seek: (t) => { c.set(Math.max(0, t)); post({ method: "seek", time: Math.max(0, t) }); },
      setRate: () => {},
      time: () => c.now(),
      duration: () => c.dur,
      paused: () => !c.playing,
      destroy: () => { clearInterval(hello); window.removeEventListener("message", onMsg); box.replaceChildren(); },
    };
  }

  /* ---------------------------------------------------------------- RuTube */

  function rutube(box, emb, on) {
    const c = clockState();
    const f = iframe(`https://rutube.ru/play/embed/${emb.id}`);
    box.append(f);
    let ready = false;
    const queue = [];
    const post = (type, data = {}) => {
      if (!ready) { queue.push([type, data]); return; }
      f.contentWindow && f.contentWindow.postMessage(JSON.stringify({ type, data }), "*");
    };
    const onMsg = (e) => {
      if (e.source !== f.contentWindow) return;
      let m = e.data;
      if (typeof m === "string") { try { m = JSON.parse(m); } catch { return; } }
      if (!m || !m.type) return;
      const d = m.data || {};
      switch (m.type) {
        case "player:ready":
          ready = true;
          on.ready(c.dur);
          while (queue.length) post(...queue.shift());
          break;
        case "player:durationChange":
          if (d.duration) c.dur = d.duration;
          break;
        case "player:currentTime":
          if (typeof d.time === "number") c.set(d.time);
          break;
        case "player:changeState":
          if (d.state === "playing") { c.set(c.t); c.playing = true; on.play(); }
          else if (["paused", "stopped", "ended"].includes(d.state)) { c.playing = false; on.pause(); }
          break;
        case "player:error":
          // T.ex. blocked_by_copyright: RuTube tillåter inte inbäddning i din region.
          if (d.fatal !== false) on.error(`rutube:${d.type || ""}`);
          break;
      }
    };
    window.addEventListener("message", onMsg);
    return {
      canRate: false,
      play: () => post("player:play"),
      pause: () => post("player:pause"),
      seek: (t) => { c.set(Math.max(0, t)); post("player:setCurrentTime", { time: Math.max(0, t) }); },
      setRate: () => {},
      time: () => c.now(),
      duration: () => c.dur,
      paused: () => !c.playing,
      destroy: () => { window.removeEventListener("message", onMsg); box.replaceChildren(); },
    };
  }

  function create(box, emb, on) {
    if (emb.kind === "youtube") return youtube(box, emb, on);
    if (emb.kind === "vk") return vk(box, emb, on);
    if (emb.kind === "rutube") return rutube(box, emb, on);
    return null;
  }

  /* Länk till källan vid en viss tid, för sajter som inte går att bädda in. */
  function linkAt(meta, t) {
    if (!meta || !meta.url) return null;
    const s = Math.max(0, Math.floor(t));
    try {
      const u = new URL(meta.url);
      if (/youtube\.com|youtu\.be|rutube\.ru|vk(video)?\.(com|ru)/.test(u.hostname)) u.searchParams.set("t", String(s));
      return u.toString();
    } catch {
      return meta.url;
    }
  }

  window.SourcePlayer = { embedFor, create, linkAt };
})();
