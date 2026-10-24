// Lays the aircraft out like a page of a spotter's sketchbook: each one drawn
// at a size relative to its real length, with handwritten notes beneath.

const page = document.getElementById("page");
const sketches = new Map(); // hex -> element
const firstSeen = new Map(); // hex -> order of arrival, so the page doesn't reshuffle
let arrivals = 0;
let last = null;
let refreshMs = 30000;

const fmt = new Intl.NumberFormat();
const POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const compass = (deg) => POINTS[Math.round(deg / 45) % 8];

// Stable pseudo-random numbers per aircraft so each keeps its own tilt.
function hashRand(str, salt) {
  let h = 2166136261 ^ salt;
  for (const c of str) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return ((h >>> 0) % 10000) / 10000;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Washed-in side-view silhouettes shown until the painting is ready, or when
// there's no photo at all. Chosen from the ADS-B emitter category.
const SILHOUETTES = {
  jet: {
    box: [200, 60],
    body: `<path d="M8,36 C8,31 13,29 22,29 L166,29 C180,29 193,32 197,36 C193,40 181,42 168,42 L24,42 C14,42 8,40 8,36 Z"/>
      <path d="M12,30 L4,3 L16,3 L44,29 Z"/>
      <path d="M6,34 L0,31 L2,36 L26,37 Z"/>
      <path d="M78,38 L104,54 L118,54 L104,38 Z"/>
      <path d="M92,43 L120,43 C125,43 127,46 127,48 C127,51 125,53 120,53 L92,53 C89,53 88,51 88,48 C88,45 89,43 92,43 Z"/>`,
    detail: `<path d="M42,33.5 L168,33.5" stroke-dasharray="1.6 2.6"/><path d="M179,32 L187,32 L191,35 L181,35 Z"/>`,
  },
  light: {
    box: [140, 56],
    body: `<path d="M10,30 C10,27 14,25 22,25 L62,22 L96,22 C108,22 118,26 122,31 C118,36 108,38 96,38 L60,38 L22,34 C14,33 10,32 10,30 Z"/>
      <path d="M14,26 L8,6 L18,6 L34,25 Z"/>
      <path d="M8,29 L2,27 L4,31 L24,31 Z"/>
      <path d="M54,18 L106,18 L106,22.5 L54,22.5 Z"/>
      <circle cx="82" cy="47" r="4"/><circle cx="114" cy="46" r="3.4"/>`,
    detail: `<path d="M72,22.5 L86,37"/><path d="M84,24 L99,24 L105,30 L86,30 Z"/>
      <path d="M124,17 L126,45"/><path d="M80,38 L82,43 M110,38 L113,43"/>`,
  },
  heli: {
    box: [160, 66],
    body: `<path d="M60,30 C60,22 76,18 96,18 C116,18 132,24 134,36 C135,46 124,50 108,50 L74,50 C64,50 60,42 60,30 Z"/>
      <path d="M62,32 L14,28 L14,34 L62,42 Z"/>
      <path d="M14,28 L8,14 L16,14 L22,29 Z"/>`,
    detail: `<path d="M18,9 L156,11" stroke-width="1.8"/><path d="M95,18 L95,10"/>
      <circle cx="11" cy="24" r="7"/><path d="M118,22 C126,24 132,30 133,36 L116,36 Z"/>
      <path d="M70,60 L132,60 C135,60 137,58 137,56 M82,50 L80,60 M120,50 L122,60"/>`,
  },
};

function silhouetteKind(p) {
  const c = (p.category || "").toUpperCase();
  if (c === "A7") return "heli";
  if (["A1", "B1", "B4", "B6"].includes(c)) return "light";
  return "jet";
}

function placeholderSvg(p) {
  const s = SILHOUETTES[silhouetteKind(p)];
  const [w, h] = s.box;
  return `<svg class="art placeholder" viewBox="-4 -4 ${w + 8} ${h + 8}" preserveAspectRatio="xMidYMid meet">
    <g filter="url(#wash)" fill="url(#washTone)" stroke="none">${s.body}</g>
    <g filter="url(#wobble)" fill="none" stroke="currentColor" stroke-width="0.8" stroke-linejoin="round">${s.body}${s.detail}</g>
    </svg>`;
}

const CATEGORY_NAMES = {
  A1: "light aircraft", A2: "small aircraft", A3: "airliner", A4: "large airliner",
  A5: "heavy jet", A6: "fast jet", A7: "helicopter", B1: "glider", B4: "microlight", B6: "drone",
};

function routeText(p) {
  const o = p.origin, d = p.destination;
  const name = (a) => (a ? a.city || a.iata || a.icao : "?");
  if (o || d) return `${name(o)} → ${name(d)}`;
  return p.operator || "";
}

function whereText(p) {
  const parts = [`${p.distance_km < 1 ? "right overhead" : `${Math.round(p.distance_km)} km ${compass(p.bearing)}`}`];
  if (p.on_ground) parts.push("on the ground");
  else if (p.altitude_ft != null) {
    let alt = `${fmt.format(Math.round(p.altitude_ft / 100) * 100)} ft`;
    if (p.vertical_rate > 300) alt += " ↑";
    else if (p.vertical_rate < -300) alt += " ↓";
    parts.push(alt);
  }
  if (p.track != null && !p.on_ground) parts.push(`heading ${compass(p.track)}`);
  return parts.join(" · ");
}

function notesHtml(p) {
  const title = p.callsign || p.registration || p.hex.toUpperCase();
  const reg = p.callsign && p.registration ? `<span class="reg">${escapeHtml(p.registration)}</span>` : "";
  const kind = p.type_name || p.icao_type || `${CATEGORY_NAMES[p.category] || "aircraft"}, type not yet known`;
  const route = routeText(p);
  const credit = p.photo_credit
    ? `<a class="credit" href="${p.photo_link}" target="_blank" rel="noopener">${escapeHtml(p.photo_credit)}</a>`
    : "";
  return `<div class="name">${escapeHtml(title)}${reg}</div>
    <div class="kind">${escapeHtml(kind)}</div>
    ${route ? `<div class="route">${escapeHtml(route)}</div>` : ""}
    <div class="where">${whereText(p)}</div>
    ${credit}`;
}

function buildSketch(p) {
  const div = document.createElement("div");
  // Notes usually go underneath, sometimes alongside, as they would on a real page.
  const r = hashRand(p.hex, 4);
  div.className = `sketch ${r < 0.55 ? "notes-below" : r < 0.8 ? "notes-right" : "notes-left"}`;
  div.style.setProperty("--tilt", `${(hashRand(p.hex, 1) - 0.5) * 7}deg`);
  div.style.setProperty("--note-tilt", `${(hashRand(p.hex, 5) - 0.5) * 4}deg`);
  div.dataset.image = p.image || "";
  div.innerHTML = `${p.image ? `<img class="art" src="${p.image}" alt="">` : placeholderSvg(p)}
    <div class="notes"></div>`;
  return div;
}

function setSize(div, p, pxPerMetre) {
  const art = div.querySelector(".art");
  // Drawings are cropped to the aircraft, so their width is its length on the page.
  const w = Math.max(80, p.length_m * pxPerMetre);
  const wide = Math.min(w, page.clientWidth * 0.5);
  art.style.width = `${wide}px`;
  const [bw, bh] = SILHOUETTES[silhouetteKind(p)].box;
  art.style.aspectRatio = String(p.image_aspect || (bw + 8) / (bh + 8));
  // Very long aircraft keep their notes underneath so they don't run off the page.
  div.classList.toggle("force-below", wide > page.clientWidth * 0.35);
}

function render(data) {
  last = data;
  document.documentElement.style.setProperty("--ts", String(data.text_scale || 1));
  const n = data.aircraft.length;
  const when = new Date(data.generated * 1000);
  document.getElementById("place").textContent = data.place_name;
  document.getElementById("summary").textContent =
    `${when.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}, ` +
    `${when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} · ` +
    `${n === 0 ? "nothing" : n} seen within ${data.radius_km} km`;
  document.getElementById("updated").textContent =
    `updated ${when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;

  for (const p of data.aircraft) {
    if (!firstSeen.has(p.hex)) firstSeen.set(p.hex, arrivals++);
  }
  const ordered = [...data.aircraft].sort((a, b) => firstSeen.get(a.hex) - firstSeen.get(b.hex));
  const seen = new Set(ordered.map((p) => p.hex));

  for (const [hex, div] of sketches) {
    if (!seen.has(hex)) {
      div.remove();
      sketches.delete(hex);
      firstSeen.delete(hex);
      positions.delete(hex);
    }
  }

  for (const p of ordered) {
    let div = sketches.get(p.hex);
    if (!div || div.dataset.image !== (p.image || "")) {
      const next = buildSketch(p);
      if (div) {
        next.classList.add("shown");
        div.replaceWith(next);
      }
      div = next;
      sketches.set(p.hex, div);
      requestAnimationFrame(() => requestAnimationFrame(() => div.classList.add("shown")));
    }
    div.querySelector(".notes").innerHTML = notesHtml(p);
    if (!div.isConnected) page.appendChild(div);
  }

  document.getElementById("empty")?.remove();
  if (n === 0) {
    const empty = document.createElement("div");
    empty.id = "empty";
    empty.textContent = "A quiet sky. Nothing to draw.";
    page.appendChild(empty);
    return;
  }

  fitToPage(ordered);
}

// Scatter the sketches over the page like a spotter's notebook. Each keeps
// its place between refreshes; newcomers go in the biggest open space. Sizes
// are shrunk together (so they stay true to each other) when the page is full,
// and grown again when it empties.
const positions = new Map(); // hex -> {x, y}
let layoutScale = 0;
const GAP = 14;

function mulberry(seed) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function separation(a, b) {
  const dx = Math.max(b.x - (a.x + a.w), a.x - (b.x + b.w), 0);
  const dy = Math.max(b.y - (a.y + a.h), a.y - (b.y + b.h), 0);
  return Math.hypot(dx, dy) || -Math.min(a.x + a.w - b.x, b.x + b.w - a.x, a.y + a.h - b.y, b.y + b.h - a.y);
}

function tryPlace(items, W, H) {
  const placed = [];
  const fits = (r) => r.x >= 0 && r.y >= 0 && r.x + r.w <= W && r.y + r.h <= H &&
    placed.every((o) => separation(r, o) >= GAP);

  // Keep anyone already on the page where they are, if they still fit.
  for (const it of items) {
    const pos = positions.get(it.hex);
    if (pos && fits({ ...it, ...pos })) {
      it.x = pos.x; it.y = pos.y;
      placed.push(it);
    }
  }
  // Then the rest, biggest first, each into the most open spot of many tried.
  const rest = items.filter((it) => !placed.includes(it)).sort((a, b) => b.w * b.h - a.w * a.h);
  for (const it of rest) {
    const rand = mulberry([...it.hex].reduce((h, c) => h * 31 + c.charCodeAt(0), 7));
    let best = null, bestScore = -Infinity;
    for (let i = 0; i < 400; i++) {
      const r = { ...it, x: rand() * (W - it.w), y: rand() * (H - it.h) };
      if (r.x < 0 || r.y < 0 || !fits(r)) continue;
      // Prefer the most open space on the page (the page edges count as
      // neighbours too, more weakly), with a little randomness so it doesn't
      // look computed.
      const edge = Math.min(r.x, r.y, W - r.x - r.w, H - r.y - r.h) * 2;
      const room = Math.min(edge, ...placed.map((o) => separation(r, o)));
      const score = Math.min(room, W * 0.25) + rand() * 30;
      if (score > bestScore) { best = r; bestScore = score; }
    }
    if (!best) return false;
    it.x = best.x; it.y = best.y;
    placed.push(it);
  }
  return true;
}

// A packed sky: staggered rows with uneven spacing, still not a neat grid.
function flowPlace(items, W) {
  let x = 0, y = 0, rowH = 0;
  for (const it of items) {
    const rand = mulberry([...it.hex].reduce((h, c) => h * 31 + c.charCodeAt(0), 3));
    if (x > 0 && x + it.w > W) { x = 0; y += rowH + GAP * 0.5; rowH = 0; }
    const dy = rand() * GAP * 1.5;
    it.x = x; it.y = y + dy;
    x += it.w + GAP * (0.6 + rand() * 1.6);
    rowH = Math.max(rowH, it.h + dy);
  }
}

function fitToPage(ordered, fresh = false) {
  const W = page.clientWidth, H = page.clientHeight;
  const base = W / 150; // px per metre at scale 1: an A380 spans half the page
  const measure = () => ordered.map((p) => {
    // Handwriting shrinks too, but stays readable.
    page.style.setProperty("--ns", String(Math.min(1, Math.max(0.85, 0.7 + 0.25 * layoutScale))));
    const div = sketches.get(p.hex);
    setSize(div, p, base * layoutScale);
    return { hex: p.hex, div, w: div.offsetWidth, h: div.offsetHeight };
  });
  const used = (items) => items.reduce((a, it) => a + it.w * it.h, 0) / (W * H);

  if (fresh || !layoutScale) { layoutScale = 4; positions.clear(); }
  let items = measure();
  if (used(items) < 0.12 && layoutScale < 4) { // page has emptied: draw bigger again
    layoutScale = Math.min(4, layoutScale * 1.4); positions.clear(); items = measure();
  }
  let placed = false;
  while (layoutScale > 0.3) {
    if (used(items) < 0.55 && tryPlace(items, W, H)) { placed = true; break; }
    layoutScale *= 0.9; positions.clear(); items = measure();
  }
  if (!placed) {
    // Too many to scatter: staggered rows, shrinking until they fit the page.
    for (;;) {
      flowPlace(items, W);
      if (Math.max(...items.map((it) => it.y + it.h)) <= H || layoutScale < 0.12) break;
      layoutScale *= 0.92; items = measure();
    }
  }
  for (const it of items) {
    positions.set(it.hex, { x: it.x, y: it.y });
    it.div.style.left = `${it.x}px`;
    it.div.style.top = `${it.y}px`;
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/sky", { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    refreshMs = (data.refresh_seconds || 30) * 1000;
    render(data);
  } catch (e) {
    document.getElementById("updated").textContent = `couldn't reach receiver: ${e.message}`;
  } finally {
    setTimeout(refresh, refreshMs);
  }
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { layoutScale = 0; last && render(last); }, 150);
});

refresh();
