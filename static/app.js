/* Live status, without reloading the page.
 *
 * A run in flight must never yank a file picker out from under the user, and
 * several sources must be queueable one after another -- so nothing here
 * reloads the document. It fetches one fragment and swaps only the regions that
 * fragment names (data-region), which are the status bits and the action
 * buttons: never a form, never an input.
 *
 * It also folds the overview's rows (a row's details open on demand, and stay
 * open for the tab's session) and switches the theme.
 *
 * No framework, no build step, and nothing is fetched from anywhere but this
 * app. Without JavaScript the pages still work: they are rendered server-side,
 * every row's details are open, and the "Refresh" link does what this does by
 * hand.
 */
(function () {
  'use strict';

  var INTERVAL = 2500;   // ms between two polls while a run is going
  var IDLE = 15000;      // ms when nothing is running: cheap, still live

  function regions(of) {
    var out = {};
    Array.prototype.forEach.call(of.children, function (node) {
      var name = node.getAttribute && node.getAttribute('data-region');
      if (name) { out[name] = node.innerHTML; }
    });
    return out;
  }

  function apply(fragment) {
    var found = regions(fragment);
    Object.keys(found).forEach(function (name) {
      var targets = document.querySelectorAll('[data-region="' + name + '"]');
      Array.prototype.forEach.call(targets, function (target) {
        target.innerHTML = found[name];
      });
    });
  }

  function busy() {
    return document.querySelectorAll('.tb-pill.live, .tb-dot.running').length > 0;
  }

  function once(url) {
    return fetch(url, { headers: { 'Accept': 'text/html' }, cache: 'no-store' })
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (html) { apply(new DOMParser().parseFromString(html, 'text/html').body); }
      })
      .catch(function () { /* offline, or the server is restarting: try again */ });
  }

  function poll() {
    var urls = (document.body.getAttribute('data-poll') || '').split(/\s+/).filter(Boolean);
    if (!urls.length || document.hidden) { return; }
    Promise.all(urls.map(once)).then(function () {
      window.setTimeout(poll, busy() ? INTERVAL : IDLE);
    });
  }

  // Coming back to a hidden tab: catch up at once rather than after the idle delay.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) { poll(); }
  });

  window.setTimeout(poll, INTERVAL);

  // ── Rows: folded until asked ──────────────────────────────────────────

  var OPEN_KEY = 'tb-open';

  function remembered() {
    try { return JSON.parse(sessionStorage.getItem(OPEN_KEY) || '[]'); } catch (e) { return []; }
  }

  function remember(id, open) {
    var ids = remembered().filter(function (other) { return other !== id; });
    if (open) { ids.push(id); }
    try { sessionStorage.setItem(OPEN_KEY, JSON.stringify(ids)); } catch (e) { /* private mode */ }
  }

  function setOpen(body, open) {
    body.classList.toggle('open', open);
    var toggle = document.querySelector('[aria-controls="' + body.id + '"]');
    if (toggle) { toggle.setAttribute('aria-expanded', open ? 'true' : 'false'); }
    remember(body.id, open);
  }

  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('.tb-toggle');
    if (toggle) {
      var body = document.getElementById(toggle.getAttribute('aria-controls'));
      if (body) { setOpen(body, !body.classList.contains('open')); }
      return;
    }
    // "Import a file" on a row: unfold it on its file picker rather than leave
    // the page. The link's own target is the fallback.
    var expand = event.target.closest('[data-expand]');
    if (expand) {
      var more = document.getElementById('more-' + expand.getAttribute('data-expand'));
      var input = more && more.querySelector('input[type=file]');
      if (input) {
        event.preventDefault();
        setOpen(more, true);
        input.focus();
      }
    }
  });

  remembered().forEach(function (id) {
    var body = document.getElementById(id);
    if (body) { setOpen(body, true); }
  });
  // A link to one source on the overview (/#renpho) opens its row.
  if (location.hash.length > 1) {
    var target = document.getElementById('more-' + location.hash.slice(1));
    if (target) { setOpen(target, true); }
  }

  // ── Theme: the OS one until the user picks ────────────────────────────

  document.addEventListener('click', function (event) {
    if (!event.target.closest('[data-theme-toggle]')) { return; }
    var root = document.documentElement;
    var next = root.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-bs-theme', next);
    try { localStorage.setItem('tb-theme', next); } catch (e) { /* private mode */ }
  });
})();
