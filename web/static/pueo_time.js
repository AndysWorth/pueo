/* Pueo local-time helpers — every absolute UI time renders in the viewer's zone.
   The server emits <time class="pueo-ts" datetime="...Z" data-fmt="datetime|date|time">
   with a UTC fallback (utils/core/timefmt.py::time_markup); this file rewrites them.
   JS callers format epochs/ISO strings with pueoFmt(). */
(function () {
  function _pad(n) { return n < 10 ? '0' + n : '' + n; }

  function _toDate(v) {
    if (v === null || v === undefined || v === '' || v === 0) return null;
    var d = (typeof v === 'number') ? new Date(v * 1000) : new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }

  /* Local ISO-style text: datetime "2026-11-23 10:56:16", date "2026-11-23",
     time "10:56:16", minute "2026-11-23 10:56". Accepts epoch seconds, an ISO
     string or a Date. Returns '—' for empty input. */
  function pueoFmt(v, style) {
    var d = (v instanceof Date) ? v : _toDate(v);
    if (!d) return '—';
    var date = d.getFullYear() + '-' + _pad(d.getMonth() + 1) + '-' + _pad(d.getDate());
    var hm = _pad(d.getHours()) + ':' + _pad(d.getMinutes());
    var time = hm + ':' + _pad(d.getSeconds());
    if (style === 'date') return date;
    if (style === 'time') return time;
    if (style === 'minute') return date + ' ' + hm;
    return date + ' ' + time;
  }

  /* Hover text: "Mon Nov 23 2026 10:56:16 EST · 15:56:16 UTC". */
  function pueoTitle(v) {
    var d = (v instanceof Date) ? v : _toDate(v);
    if (!d) return '';
    var local = d.toString().replace(/ GMT[+-]\d{4}/, '').replace(/ \((.*)\)$/, function (m, zone) {
      var abbr = zone.split(' ').map(function (w) { return w.charAt(0); }).join('');
      return ' ' + (zone.indexOf(' ') >= 0 ? abbr : zone);
    });
    var iso = d.toISOString();
    return local + ' · ' + iso.slice(0, 10) + ' ' + iso.slice(11, 19) + ' UTC';
  }

  function pueoLocalizeTimes(root) {
    var scope = root || document;
    if (!scope.querySelectorAll) return;
    scope.querySelectorAll('time.pueo-ts:not([data-localized])').forEach(function (el) {
      var d = _toDate(el.getAttribute('datetime'));
      if (!d) return;
      el.textContent = pueoFmt(d, el.getAttribute('data-fmt') || 'datetime');
      el.title = pueoTitle(d);
      el.setAttribute('data-localized', '1');
    });
  }

  window.pueoFmt = pueoFmt;
  window.pueoTitle = pueoTitle;
  window.pueoLocalizeTimes = pueoLocalizeTimes;

  /* Initial render, SPA tab swaps and SSE-inserted rows: one observer on
     #main-content (falls back to body for standalone pages) covers them all. */
  function _start() {
    pueoLocalizeTimes(document);
    var target = document.getElementById('main-content') || document.body;
    if (!target || typeof MutationObserver === 'undefined') return;
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].addedNodes.length) { pueoLocalizeTimes(target); return; }
      }
    }).observe(target, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _start);
  } else {
    _start();
  }
})();
