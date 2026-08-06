// PreviewPage.cs — a throwaway debug view at GET /preview.
//
// NOT part of the API contract in docs/SIM-AGENT-BRIEF.md, and not a stand-in
// for the real SIM surface: that belongs to deck-ui on the Pi, which renders
// deck-api's state, not this. This exists so a browser pointed straight at the
// agent shows what the agent believes, which is useful when the panel and the
// agent disagree and you need to know which one is wrong.
//
// Served BY the agent so it is same-origin: a page served from the Pi fetching
// :9109 cross-origin would be blocked, and adding CORS headers to a
// token-guarded LAN endpoint to work around that is not a trade worth making.
// The page reads the token out of its own query string and reuses it.
//
// Sized in vmin and reflowed by aspect ratio, so it reads on the 2560x720
// XENEON Edge and on a 1080p TV alike.
//
// It renders OBSERVED state only, and there is no command UI here on purpose.
// The one thing it goes out of its way to make explicit is the disagreement
// between the handle and the gear: captured live at 205-228 kt, above the King
// Air's gear operating limit, the lever moved five times and the gear never
// did. A panel that drew the commanded position would have read GEAR DOWN at
// 228 knots. This one says NOT MOVING and keeps showing the truth.

namespace FlightDeckSimAgent;

internal static class PreviewPage
{
    public const string Html = """
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>flightdeck-sim-agent</title>
<style>
  :root { --bg:#080a0e; --panel:#131820; --panel2:#0e131a; --line:#222b37; --dim:#61708350;
          --label:#6b7c91; --txt:#e6edf5; --ok:#4ade80; --mid:#fbbf24; --off:#5b6b7f;
          --bad:#f87171; --glow:0 0 18px -4px; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); height:100vh; overflow:hidden;
         font:16px/1.2 ui-monospace,"Cascadia Mono",Consolas,monospace;
         -webkit-font-smoothing:antialiased; }
  .wrap { height:100vh; display:flex; flex-direction:column; padding:2vmin 2.4vmin; gap:1.5vmin; }

  header { display:flex; align-items:center; gap:2vmin; border-bottom:1px solid var(--line);
           padding-bottom:1.3vmin; }
  .ac { font-size:clamp(17px,3.2vmin,32px); letter-spacing:.02em; white-space:nowrap;
        overflow:hidden; text-overflow:ellipsis; }
  .meta { color:var(--label); font-size:clamp(10px,1.6vmin,15px); letter-spacing:.1em; }
  .pill { margin-left:auto; display:flex; align-items:center; gap:.9vmin; padding:.7vmin 1.4vmin;
          border:1px solid var(--line); border-radius:99px; letter-spacing:.2em;
          font-size:clamp(10px,1.7vmin,16px); white-space:nowrap; }
  .dot { width:1.1vmin; height:1.1vmin; min-width:8px; min-height:8px; border-radius:50%;
         background:var(--off); }
  .pill.up { color:var(--ok); border-color:#1d4c33; } .pill.up .dot { background:var(--ok); box-shadow:var(--glow) var(--ok); }
  .pill.down { color:var(--bad); border-color:#4c2020; } .pill.down .dot { background:var(--bad); }

  main { flex:1; display:flex; gap:1.5vmin; min-height:0; }
  .tiles { flex:3; display:grid; grid-template-columns:1.7fr 1fr 1fr 1fr 1fr; gap:1.5vmin; }
  .reads { flex:1.1; display:grid; grid-template-rows:repeat(3,1fr); gap:1.5vmin; }

  .tile { position:relative; border:1px solid var(--line); border-radius:14px;
          background:linear-gradient(160deg,var(--panel),var(--panel2));
          padding:1.9vmin; display:flex; flex-direction:column; justify-content:center;
          align-items:flex-start; min-height:0; overflow:hidden; }
  .tile h2 { position:absolute; top:1.5vmin; left:1.9vmin; font-weight:400;
             font-size:clamp(9px,1.45vmin,13px); letter-spacing:.24em; color:var(--label); }
  .tile .unit { position:absolute; top:1.5vmin; right:1.9vmin; font-size:clamp(9px,1.45vmin,13px);
                letter-spacing:.18em; color:var(--label); }
  .val { font-size:clamp(23px,5.2vmin,56px); letter-spacing:.03em; line-height:1.05;
         font-variant-numeric:tabular-nums; }
  .val.sm { font-size:clamp(19px,4vmin,38px); }
  .sub { color:var(--label); font-size:clamp(10px,1.7vmin,15px); margin-top:.8vmin;
         letter-spacing:.06em; min-height:1.1em; }
  .on { color:var(--ok); text-shadow:var(--glow) #4ade8055; }
  .amber { color:var(--mid); text-shadow:var(--glow) #fbbf2455; }
  .offc { color:var(--off); }

  /* Unavailable on this airframe — greyed and named, never a dead control. */
  .tile.na { opacity:.32; }
  .tile.na .val { font-size:clamp(15px,2.9vmin,25px); color:var(--off); text-shadow:none; }

  .bar { margin-top:1.1vmin; width:100%; height:1.3vmin; min-height:8px; background:#070b0f;
         border:1px solid #1b232e; border-radius:99px; overflow:hidden; }
  .bar i { display:block; height:100%; width:0; background:var(--ok); transition:width .1s linear; }
  .bar i.amber { background:var(--mid); }

  /* The handle-vs-observed callout. Hidden unless they actually disagree. */
  .warn { margin-top:.9vmin; font-size:clamp(9px,1.6vmin,14px); letter-spacing:.14em;
          color:var(--mid); border:1px solid #4a3a14; background:#241d0c;
          border-radius:6px; padding:.5vmin 1vmin; display:none; }
  .warn.show { display:block; }

  /* Flap detent pips — one per handle position, filled to the current index. */
  .pips { display:flex; gap:.7vmin; margin-top:1.1vmin; }
  .pips b { flex:1; height:1.1vmin; min-height:7px; border-radius:99px; background:#141b24;
            border:1px solid #1e2732; }
  .pips b.f { background:var(--ok); border-color:var(--ok); }

  .reads .val { font-size:clamp(28px,7.2vmin,70px); }
  body.stale .wrap { opacity:.45; transition:opacity .2s; }

  @media (max-aspect-ratio: 2/1) {
    main { flex-direction:column; }
    .tiles { flex:2.4; grid-template-columns:repeat(3,1fr); grid-template-rows:repeat(2,1fr); }
    #t-gear { grid-column:span 2; }
    .reads { flex:1; grid-template-rows:none; grid-template-columns:repeat(3,1fr); }
  }
</style>
<div class="wrap">
  <header>
    <div class="ac" id="ac">—</div>
    <div class="meta" id="meta"></div>
    <div class="pill down" id="link"><span class="dot"></span><span id="linktxt">NO LINK</span></div>
  </header>
  <main>
    <div class="tiles">
      <div class="tile" id="t-gear"><h2>LANDING GEAR</h2><div class="unit" id="u-gear"></div>
        <div class="val" id="v-gear">—</div>
        <div class="sub" id="s-gear"></div>
        <div class="bar"><i id="b-gear"></i></div>
        <div class="warn" id="w-gear"></div>
      </div>
      <div class="tile" id="t-flaps"><h2>FLAPS</h2><div class="unit" id="u-flaps"></div>
        <div class="val sm" id="v-flaps">—</div>
        <div class="sub" id="s-flaps"></div>
        <div class="pips" id="p-flaps"></div>
      </div>
      <div class="tile" id="t-park"><h2>PARKING BRAKE</h2>
        <div class="val sm" id="v-park">—</div><div class="sub" id="s-park"></div></div>
      <div class="tile" id="t-lights"><h2>LANDING LIGHTS</h2>
        <div class="val sm" id="v-lights">—</div><div class="sub" id="s-lights"></div></div>
      <div class="tile" id="t-ap"><h2>AUTOPILOT</h2>
        <div class="val sm" id="v-ap">—</div><div class="sub" id="s-ap"></div></div>
    </div>
    <div class="reads">
      <div class="tile"><h2>INDICATED AIRSPEED</h2><div class="unit">KT</div>
        <div class="val" id="v-ias">—</div></div>
      <div class="tile"><h2>INDICATED ALTITUDE</h2><div class="unit">FT</div>
        <div class="val" id="v-alt">—</div></div>
      <div class="tile"><h2>HEADING MAGNETIC</h2><div class="unit">DEG</div>
        <div class="val" id="v-hdg">—</div></div>
    </div>
  </main>
</div>
<script>
(function () {
  var token = new URLSearchParams(location.search).get('token') || '';
  var $ = function (id) { return document.getElementById(id); };
  var lastOk = 0;

  // Absent from `controls` means the aircraft does not have it. Grey the tile
  // and name the reason — never render a dead control.
  function avail(key, present) {
    $('t-' + key).classList.toggle('na', !present);
    if (!present) {
      $('v-' + key).textContent = 'NOT FITTED';
      var s = $('s-' + key); if (s) s.textContent = '';
      var u = $('u-' + key); if (u) u.textContent = '';
    }
    return present;
  }

  function simple(key, present, state, onWord) {
    if (!avail(key, present)) return;
    var word = state.toUpperCase();
    $('v-' + key).textContent = word;
    $('v-' + key).className = 'val sm ' + (state === onWord ? 'on' : 'offc');
    $('s-' + key).textContent = state === onWord ? 'active' : 'inactive';
  }

  function paint(s) {
    $('ac').textContent = s.aircraft || '(no aircraft)';
    $('meta').textContent = 'SEQ ' + s.seq;
    var c = s.controls || {};

    if (avail('gear', !!c.gear)) {
      var g = c.gear;
      var cls = g.state === 'transit' ? 'amber' : g.state === 'down' ? 'on' : 'offc';
      $('v-gear').textContent = g.state.toUpperCase();
      $('v-gear').className = 'val ' + cls;
      $('u-gear').textContent = g.pct.toFixed(1) + '%';
      $('s-gear').textContent = 'HANDLE ' + g.handle.toUpperCase() + '  ·  OBSERVED ' + g.state.toUpperCase();
      $('b-gear').style.width = g.pct + '%';
      $('b-gear').className = g.state === 'transit' ? 'amber' : '';

      // Handle says one thing, the gear is doing another, and nothing is
      // moving. This is a command the sim declined — over the gear speed
      // limit, on the ground, whatever. Say so instead of drawing the lever.
      var stuck = g.state !== 'transit' && g.handle !== g.state;
      var w = $('w-gear');
      w.classList.toggle('show', stuck);
      if (stuck) w.textContent = 'COMMANDED ' + g.handle.toUpperCase() + ' · NOT MOVING · SIM DECLINED';
    }

    if (avail('flaps', !!c.flaps)) {
      var f = c.flaps;
      $('v-flaps').textContent = f.index + ' / ' + f.detents;
      $('v-flaps').className = 'val sm ' + (f.index > 0 ? 'on' : 'offc');
      $('u-flaps').textContent = f.angle_deg.toFixed(1) + '°';
      $('s-flaps').textContent = f.index === 0 ? 'UP / CLEAN' : 'DETENT ' + f.index;
      var pips = $('p-flaps');
      if (pips.children.length !== f.detents) {
        pips.innerHTML = '';
        for (var i = 0; i < f.detents; i++) pips.appendChild(document.createElement('b'));
      }
      for (var j = 0; j < pips.children.length; j++)
        pips.children[j].className = j < f.index ? 'f' : '';
    }

    simple('park', !!c.parking_brake, c.parking_brake ? c.parking_brake.state : '', 'set');
    simple('lights', !!c.landing_lights, c.landing_lights ? c.landing_lights.state : '', 'on');
    simple('ap', !!c.ap_master, c.ap_master ? c.ap_master.state : '', 'engaged');

    var r = s.readouts || {};
    $('v-ias').textContent = r.ias_kt;
    $('v-alt').textContent = r.alt_ft;
    $('v-hdg').textContent = ('00' + r.hdg_mag).slice(-3);
  }

  function link(up) {
    var el = $('link');
    el.className = 'pill ' + (up ? 'up' : 'down');
    $('linktxt').textContent = up ? 'LINK' : 'NO LINK';
  }

  // Losing the sim is normal operation, not an error: the workstation reboots
  // into Linux and this agent goes away with it. Dim, say NO LINK, keep trying.
  function tick() {
    fetch('/state?token=' + encodeURIComponent(token), { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .then(function (s) { lastOk = Date.now(); link(true); paint(s); })
      .catch(function () { link(false); });
    document.body.classList.toggle('stale', Date.now() - lastOk > 1500);
  }
  tick();
  setInterval(tick, 200);
})();
</script>
""";
}
