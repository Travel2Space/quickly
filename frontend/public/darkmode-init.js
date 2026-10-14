// Pre-React flash guard: only toggles a class. DarkReader itself is bundled
// (DarkModeContext imports it from npm) — no third-party script at runtime.
(function () {
  try {
    var pref = localStorage.getItem('darkreader');
    var wantDark = pref === 'on' || pref === 'dark' ||
      (pref !== 'off' && pref !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (wantDark) document.documentElement.classList.add('darkreader-init');
  } catch (e) {}
})();
