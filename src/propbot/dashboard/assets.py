"""The panel's only stylesheet and script, served from `/assets/` (CSP allows 'self' only).

No framework, no external font or CDN: the panel works without internet. The script does
one thing: it refreshes the live section by fetching an HTML fragment from the same origin
every few seconds. Forms live outside that section, so a refresh never wipes what the user
is typing; refreshing pauses while the tab is hidden and shows a "no connection" line on failure.
"""

from typing import Final

REFRESH_MS: Final = 5000
CSS_PATH: Final = "/assets/app.css"
JS_PATH: Final = "/assets/app.js"

APP_CSS: Final = """\
:root {
  color-scheme: light dark;
  --bg: #f6f7f9; --card: #ffffff; --text: #16181d; --muted: #4a5160; --line: #d5d9e0;
  --ok-bg: #dff3e4; --ok-fg: #0d4a1f; --warn-bg: #fff1cc; --warn-fg: #5c4100;
  --bad-bg: #fbdcdc; --bad-fg: #6b0f0f; --info-bg: #e1ebfb; --info-fg: #0f2f66;
  --accent: #1d4ed8; --danger: #b42318;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12141a; --card: #1b1e26; --text: #eceef3; --muted: #b3b9c6; --line: #353a46;
    --ok-bg: #173b22; --ok-fg: #c8f0d3; --warn-bg: #3d3110; --warn-fg: #ffe7a3;
    --bad-bg: #4a1717; --bad-fg: #ffd1d1; --info-bg: #182a4a; --info-fg: #d3e2ff;
    --accent: #8fb3ff; --danger: #ff8b80;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
a { color: var(--accent); }
.top {
  display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; align-items: center;
  padding: .75rem 1rem; border-bottom: 1px solid var(--line); background: var(--card);
}
.brand { font-weight: 700; }
.top nav { display: flex; gap: 1rem; }
.top nav a[aria-current="page"] { font-weight: 700; text-decoration: none; }
.mode { color: var(--muted); margin-left: auto; }
main {
  max-width: 1100px; margin: 0 auto; padding: 1rem; display: grid; gap: 1rem;
  grid-template-columns: minmax(0, 1fr);
}
/* Grid items default to min-width: auto, so one wide table would stretch the whole page
   past a phone's viewport; with 0 the table scrolls inside its own .table-wrap instead. */
main > *, .grid > * { min-width: 0; }
#live { display: grid; gap: 1rem; grid-template-columns: minmax(0, 1fr); }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 8px;
  padding: 1rem; min-width: 0;
}
.card h2 { margin: 0 0 .5rem; font-size: 1.1rem; }
.grid {
  display: grid; gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
}
.muted, .source { color: var(--muted); font-size: .9rem; }
.badge {
  display: inline-block; padding: .1rem .55rem; border-radius: 999px; font-weight: 700;
  border: 1px solid currentColor;
}
.badge.ok { background: var(--ok-bg); color: var(--ok-fg); }
.badge.warn { background: var(--warn-bg); color: var(--warn-fg); }
.badge.bad { background: var(--bad-bg); color: var(--bad-fg); }
.badge.info { background: var(--info-bg); color: var(--info-fg); }
.banner { padding: .75rem 1rem; border-radius: 8px; border: 1px solid currentColor; }
.banner.bad { background: var(--bad-bg); color: var(--bad-fg); }
.banner.warn { background: var(--warn-bg); color: var(--warn-fg); }
.gauge { margin: .6rem 0; }
.gauge .title { font-weight: 600; }
.gauge meter { width: 100%; height: 1rem; }
.gauge .value { font-variant-numeric: tabular-nums; }
.gauge.warn .value { color: var(--danger); font-weight: 700; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .92rem; }
th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; }
td.num { font-variant-numeric: tabular-nums; }
form { display: grid; gap: .5rem; margin: .5rem 0 1rem; }
fieldset { border: 1px solid var(--line); border-radius: 8px; padding: .75rem; margin: 0; }
fieldset[disabled] { opacity: .65; }
legend { font-weight: 700; padding: 0 .25rem; }
label { display: block; }
input[type="text"], input[type="password"] {
  width: 100%; max-width: 22rem; padding: .4rem .5rem; font: inherit; color: var(--text);
  background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
}
button {
  justify-self: start; padding: .45rem .9rem; font: inherit; font-weight: 600; cursor: pointer;
  border-radius: 6px; border: 1px solid var(--accent); background: var(--accent);
  color: var(--card);
}
button.danger { border-color: var(--danger); background: var(--danger); }
button:disabled { cursor: not-allowed; }
.check { display: flex; gap: .4rem; align-items: center; }
pre.reply {
  white-space: pre-wrap; word-break: break-word; background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: .75rem;
}
#conn { font-weight: 700; color: var(--danger); }
footer { color: var(--muted); font-size: .85rem; text-align: center; padding: 1rem; }
/* Analysis screen: cards, tables and inline SVG charts (dashboard.analysis / charts). */
:root {
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --neg: #d03b3b;
  --limit: #b42318; --track: #eef0f3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --neg: #e66767;
    --limit: #ff8b80; --track: #262a33;
  }
}
h1 { margin: 0 0 .5rem; font-size: 1.35rem; }
.an-card h3 { margin: 1rem 0 .4rem; font-size: 1rem; }
.an-card dd, .an-card li, .an-card p, .an-card h2 { overflow-wrap: anywhere; }
.an-card td, .an-card th { overflow-wrap: break-word; }
.an-flags { margin: .25rem 0; }
.an-verdict { font-size: 1.05rem; }
.an-split { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
.an-tables {
  display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
}
.an-mini th, .an-mini td { white-space: nowrap; overflow-wrap: normal; }
.an-multiples {
  display: grid; gap: .5rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.an-kv { display: grid; grid-template-columns: minmax(8rem, max-content) 1fr; gap: .2rem 1rem; }
.an-kv dt { color: var(--muted); }
.an-kv dd { margin: 0; }
.an-table caption { text-align: left; color: var(--muted); padding: .25rem 0; }
.an-problems code, .an-card code { overflow-wrap: anywhere; }
.an-chart { margin: .75rem 0; min-width: 0; }
.an-multiples svg.chart { max-width: 360px; }
.an-chart figcaption { margin-top: .25rem; }
svg.chart { display: block; width: 100%; height: auto; max-width: 760px; }
svg.chart text { fill: var(--text); }
svg.chart .c-muted { fill: var(--muted); }
svg.cellbar { width: 60px; height: 10px; vertical-align: middle; }
.c-bar-a { fill: var(--series-1); }
.c-bar-b { fill: var(--series-2); }
.c-bar-c { fill: var(--series-3); }
.c-bar-a.c-neg { fill: var(--neg); }
.c-track { fill: var(--track); }
.c-axis, .c-grid { stroke: var(--line); stroke-width: 1; }
.c-zero { stroke: var(--muted); stroke-width: 1; }
.c-marker { stroke: var(--text); stroke-width: 1.5; }
.c-limit { stroke: var(--limit); stroke-width: 2; }
.c-line { stroke: var(--series-1); stroke-width: 2; fill: none; }
.c-dot { fill: var(--series-1); stroke: var(--card); stroke-width: 2; }
"""

APP_JS: Final = """\
(function () {
  "use strict";
  var box = document.getElementById("live");
  var conn = document.getElementById("conn");
  if (!box) { return; }
  var url = box.getAttribute("data-src");
  var timer = null;
  function show(ok) {
    if (!conn) { return; }
    conn.textContent = ok ? "" : "bağlantı yok";
    conn.hidden = ok;
  }
  function refresh() {
    if (document.hidden) { return; }
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("HTTP " + response.status); }
        return response.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        box.replaceChildren.apply(box, Array.prototype.slice.call(doc.body.childNodes));
        show(true);
      })
      .catch(function () { show(false); });
  }
  function start() { if (timer === null) { timer = window.setInterval(refresh, __REFRESH_MS__); } }
  function stop() { if (timer !== null) { window.clearInterval(timer); timer = null; } }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stop(); } else { refresh(); start(); }
  });
  start();
})();
""".replace("__REFRESH_MS__", str(REFRESH_MS))
