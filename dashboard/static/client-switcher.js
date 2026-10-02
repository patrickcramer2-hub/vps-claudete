/**
 * Claudete — barra multi-cliente
 * Injetar em cada página HTML com:
 *   <script src="/static/client-switcher.js"></script>
 * Nenhuma dependência. Funciona sobre qualquer layout existente.
 */
(function () {
  'use strict';

  var CLIENTS = [
    { id: 'ccn',      label: 'CCN',      href: '/ccn',   color: '#059669' },
    { id: 'sefas',    label: 'SEFAS',    href: '/sefas', color: '#7C3AED' },
    { id: 'prosaude', label: 'Prosaúde', href: null,     color: '#9CA3AF', disabled: true },
  ];

  var BAR_H = 40; // px

  function activeClient() {
    var path = window.location.pathname;
    if (path === '/sefas' || path.indexOf('/sefas/') === 0) return 'sefas';
    return 'ccn';
  }

  function css(el, rules) {
    Object.assign(el.style, rules);
  }

  function buildBar() {
    var active = activeClient();

    var bar = document.createElement('div');
    bar.id = 'clt-bar';
    css(bar, {
      position:     'fixed',
      top:          '0',
      left:         '0',
      right:        '0',
      height:       BAR_H + 'px',
      background:   '#161B22',
      borderBottom: '1px solid #30363D',
      display:      'flex',
      alignItems:   'center',
      padding:      '0 16px',
      gap:          '6px',
      zIndex:       '99999',
      fontFamily:   'system-ui, -apple-system, sans-serif',
      fontSize:     '12px',
      boxSizing:    'border-box',
    });

    // Logo produto
    var brand = document.createElement('span');
    brand.textContent = 'Claudete';
    css(brand, { fontWeight: '700', color: '#8B949E', letterSpacing: '.04em', marginRight: '6px' });
    bar.appendChild(brand);

    var sep = document.createElement('span');
    sep.textContent = '|';
    css(sep, { color: '#30363D' });
    bar.appendChild(sep);

    // Chips de cliente
    CLIENTS.forEach(function (c) {
      var chip = document.createElement(c.href && !c.disabled ? 'a' : 'span');
      chip.textContent = c.label;
      if (chip.tagName === 'A') {
        chip.href = c.href;
        chip.setAttribute('title', c.label);
      }
      var isActive = c.id === active;
      css(chip, {
        display:        'inline-flex',
        alignItems:     'center',
        padding:        '3px 10px',
        borderRadius:   '6px',
        fontWeight:     '600',
        textDecoration: 'none',
        cursor:         c.disabled ? 'default' : 'pointer',
        transition:     'opacity .15s',
        border:         '1px solid transparent',
        background: isActive
          ? c.color
          : c.disabled ? '#1C2128' : 'transparent',
        color: isActive
          ? '#fff'
          : c.disabled ? '#484F58' : c.color,
        borderColor: isActive || c.disabled
          ? 'transparent'
          : c.color + '55',
        opacity: c.disabled ? '0.5' : '1',
      });
      if (!c.disabled) {
        chip.addEventListener('mouseenter', function () { chip.style.opacity = '0.8'; });
        chip.addEventListener('mouseleave', function () { chip.style.opacity = '1'; });
      }
      bar.appendChild(chip);
    });

    // Espaçador
    var spacer = document.createElement('div');
    spacer.style.flex = '1';
    bar.appendChild(spacer);

    // Botão visão global (só Patrick)
    var global = document.createElement('a');
    global.href  = '/admin';
    global.textContent = '⊕ Visão Global';
    css(global, {
      fontSize:       '11px',
      color:          '#8B949E',
      textDecoration: 'none',
      padding:        '3px 10px',
      border:         '1px solid #30363D',
      borderRadius:   '6px',
    });
    bar.appendChild(global);

    return bar;
  }

  function inject() {
    if (document.getElementById('clt-bar')) return;

    var bar = buildBar();
    document.body.insertBefore(bar, document.body.firstChild);

    // Empurra o conteúdo para não ficar atrás da barra
    var style = document.createElement('style');
    style.textContent = 'body { padding-top: ' + BAR_H + 'px !important; }';
    document.head.appendChild(style);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
