// Совместимо с Chrome 75: без ?., ??, replaceAll и т.п.

// --- Переключатель светлой / тёмной темы ---
(function () {
  'use strict';
  var root = document.documentElement;
  var meta = document.querySelector('meta[name="theme-color"]');
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function effective() {
    var t = root.getAttribute('data-theme');
    if (t === 'light' || t === 'dark') return t;
    return (mq && mq.matches) ? 'dark' : 'light';
  }
  function paintMeta() {
    if (meta) meta.setAttribute('content', effective() === 'dark' ? '#0f1113' : '#3a3c40');
  }
  paintMeta();

  var btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function () {
      var next = effective() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (e) {}
      paintMeta();
    });
  }
  // Если пользователь не выбирал тему — следуем за системой на лету
  if (mq && mq.addEventListener) {
    mq.addEventListener('change', function () {
      var t = root.getAttribute('data-theme');
      if (t !== 'light' && t !== 'dark') paintMeta();
    });
  }
})();

(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function fmtHms(sec) {
    if (sec === null || isNaN(sec)) return '';
    var sign = sec < 0 ? '-' : '';
    sec = Math.abs(Math.floor(sec));
    return sign + pad(Math.floor(sec / 3600)) + '.' + pad(Math.floor(sec % 3600 / 60)) + '.' + pad(sec % 60);
  }

  function parseHms(v) {
    v = (v || '').trim();
    if (!v) return null;
    var p = v.split(/[.:]/);
    if (p.length === 2) p.push('0');
    if (p.length !== 3) return NaN;
    for (var i = 0; i < 3; i++) if (!/^\d+$/.test(p[i])) return NaN;
    if (+p[1] > 59 || +p[2] > 59) return NaN;
    return (+p[0]) * 3600 + (+p[1]) * 60 + (+p[2]);
  }

  function parseLocal(v) {
    // 'YYYY-MM-DDTHH:MM[:SS]' -> Date в локальном времени
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(v || '');
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }

  function toLocalInput(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  var begin = $('BEGIN_TIME');
  if (!begin) return;
  var end = $('END_TIME'), endNow = $('END_NOW'), numb = $('NUMB_RBS'),
      prostoy = $('PROSTOY'), length = $('LENGHT'), common = $('COMMON_PROSTOY');

  if (!begin.value) begin.value = toLocalInput(new Date());

  function recalc() {
    end.disabled = endNow.checked;
    if (endNow.checked) end.value = '';

    var b = parseLocal(begin.value);
    var e = endNow.checked ? new Date() : parseLocal(end.value);
    length.textContent = (b && e) ? fmtHms((e - b) / 1000) : '—';
    length.parentNode.classList.toggle('bad', !!(b && e && e < b));

    var p = parseHms(prostoy.value);
    prostoy.classList.toggle('bad', isNaN(p) && p !== null);
    var n = (numb.value || '').trim();
    var cnt = /^[СсCc]$/.test(n) ? 1 : (/^\d+$/.test(n) ? +n : null);
    common.textContent = (p !== null && !isNaN(p) && cnt !== null) ? fmtHms(p * cnt) : '—';
  }

  var hms = document.querySelectorAll('input.hms');
  for (var i = 0; i < hms.length; i++) {
    hms[i].addEventListener('input', function () {
      var v = parseHms(this.value);
      this.classList.toggle('bad', isNaN(v) && v !== null);
      recalc();
    });
  }
  [begin, end, endNow, numb].forEach(function (el) {
    el.addEventListener('input', recalc);
    el.addEventListener('change', recalc);
  });
  recalc();
  setInterval(function () { if (endNow.checked) recalc(); }, 1000);
})();

// --- Скоростной ввод: автозаполнение из справочника БС, шаблоны, горячие клавиши ---
(function () {
  'use strict';
  function $(id) { return document.getElementById(id); }
  function byName(n) { return document.querySelector('[name="' + n + '"]'); }
  function setVal(n, v) {
    var el = byName(n);
    if (!el || v == null) return;
    el.value = v;
    el.dispatchEvent(new Event('input'));
    el.dispatchEvent(new Event('change'));
  }

  // --- Автозаполнение из справочника ---
  var search = $('station-search');
  if (search) {
    var box = $('station-suggest');
    var hidden = $('STATION_CODE');
    var picked = $('station-picked');
    var clearBtn = $('station-clear');
    var timer = null, items = [], active = -1;

    function hide() { box.hidden = true; box.innerHTML = ''; items = []; active = -1; }

    function choose(s) {
      setVal('SERV_ELEM', s.ADDR);
      setVal('NAPR', s.NAPR);
      setVal('TEHC', s.TEHC);
      setVal('NETW_TYPE', s.NETW_TYPE);
      setVal('EQIP_TYPE', s.EQIP_TYPE);
      hidden.value = s.CODE;
      search.value = s.CODE + ' — ' + s.ADDR;
      if (picked) { picked.hidden = false; picked.querySelector('b').textContent = s.CODE; }
      hide();
    }

    function render() {
      if (!items.length) { hide(); return; }
      box.innerHTML = '';
      items.forEach(function (s, i) {
        var d = document.createElement('div');
        d.className = 'ac-opt' + (i === active ? ' active' : '');
        d.innerHTML = '<b>' + s.CODE + '</b> <span>' + s.ADDR + '</span> <i>' + s.TEHC + ' · ' + s.NETW_TYPE + '</i>';
        d.addEventListener('mousedown', function (e) { e.preventDefault(); choose(s); });
        box.appendChild(d);
      });
      box.hidden = false;
    }

    function fetchSuggest(q) {
      fetch('/api/stations?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (data) { items = data || []; active = -1; render(); })
        .catch(function () { hide(); });
    }

    search.addEventListener('input', function () {
      hidden.value = '';
      if (picked) picked.hidden = true;
      var q = search.value.trim();
      if (timer) clearTimeout(timer);
      if (q.length < 1) { hide(); return; }
      timer = setTimeout(function () { fetchSuggest(q); }, 180);
    });
    search.addEventListener('keydown', function (e) {
      if (box.hidden) return;
      if (e.key === 'ArrowDown') { active = Math.min(active + 1, items.length - 1); render(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
      else if (e.key === 'Enter' && active >= 0) { choose(items[active]); e.preventDefault(); }
      else if (e.key === 'Escape') { hide(); }
    });
    search.addEventListener('blur', function () { setTimeout(hide, 150); });
    if (clearBtn) clearBtn.addEventListener('click', function () {
      hidden.value = ''; search.value = ''; if (picked) picked.hidden = true;
    });
  }

  // --- Шаблоны типовых аварий ---
  var chips = document.querySelectorAll('.templates .chip[data-tpl]');
  for (var i = 0; i < chips.length; i++) {
    chips[i].addEventListener('click', function () {
      var tpl;
      try { tpl = JSON.parse(this.getAttribute('data-tpl')); } catch (e) { return; }
      Object.keys(tpl).forEach(function (k) { setVal(k, tpl[k]); });
    });
  }

  // --- Ctrl+Enter — сохранить форму аварии ---
  var form = document.querySelector('.alarm-form');
  if (form) {
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'Enter')) {
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.submit();
      }
    });
  }
})();
