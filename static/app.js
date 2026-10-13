/* Live status, without reloading the page.
 *
 * A run in flight must never yank a file picker out from under the user, and
 * several sources must be queueable one after another -- so nothing here
 * reloads the document. It fetches one fragment and swaps only the regions that
 * fragment names (data-region), which are the status bits and the action
 * buttons: never a form, never an input.
 *
 * No framework, no build step, and nothing is fetched from anywhere but this
 * app. Without JavaScript the pages still work: they are rendered server-side,
 * and the "Refresh" link does what this does by hand.
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
})();
