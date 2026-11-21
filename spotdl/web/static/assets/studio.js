/* Local interface: no player, remote scripts or frontend build step. */
'use strict';
(() => {
  const $ = (selector) => document.querySelector(selector);
  const terminal = new Set(['completed', 'failed', 'cancelled', 'partial']);
  const statusNames = {queued: 'Na fila', running: 'Baixando', completed: 'Concluído', failed: 'Falhou', cancelled: 'Cancelado', partial: 'Com falhas'};
  const kinds = {video: 'Vídeo', playlist: 'Playlist', album: 'Álbum', track: 'Música'};
  const state = {preview: null, jobs: [], filter: 'all', sequence: 0, controller: null, outputLoaded: false};
  const nodes = new Map();
  const statuses = new Map();
  let previewTimer;
  let toastTimer;
  let owner;
  try {
    owner = localStorage.getItem('md-owner');
    if (!/^[A-Za-z0-9_-]{16,64}$/.test(owner || '')) {
      owner = crypto.randomUUID().replaceAll('-', '');
      localStorage.setItem('md-owner', owner);
    }
  } catch (_) {
    owner = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  }

  async function api(path, options = {}) {
    const response = await fetch(`/api/gui${path}`, {...options, headers: {'Content-Type': 'application/json', ...options.headers}});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === 'string' ? body.detail : `Não foi possível concluir a solicitação (${response.status}).`);
    }
    return response.json();
  }

  function toast(message) {
    $('#toast').textContent = message;
    $('#toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4500);
  }

  function formError(message = '') {
    $('#form-error').textContent = message;
    $('#form-error').hidden = !message;
  }

  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $('#theme-select').value = theme;
    const target = theme === 'dark' ? 'claro' : 'escuro';
    $('#theme-toggle').setAttribute('aria-label', `Ativar tema ${target}`);
    $('#theme-toggle').title = `Ativar tema ${target}`;
    $('#theme-toggle use').setAttribute('href', theme === 'dark' ? '#i-sun' : '#i-moon');
    try { localStorage.setItem('md-theme', theme); } catch (_) {}
  }
  setTheme(document.documentElement.dataset.theme);
  $('#theme-toggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  $('#theme-select').addEventListener('change', event => setTheme(event.target.value));
  $('#settings-button').addEventListener('click', () => $('#settings-dialog').showModal());
  $('#close-settings').addEventListener('click', () => $('#settings-dialog').close());
  $('#settings-dialog').addEventListener('click', event => { if (event.target === $('#settings-dialog')) { const bounds = event.target.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.target.close(); } });

  function safeImage(image, url, fallback) {
    let allowed = '';
    try { if (new URL(url).protocol === 'https:') allowed = url; } catch (_) {}
    if (image.getAttribute('src') === allowed) return;
    image.hidden = !allowed;
    if (fallback) fallback.hidden = !!allowed;
    image.onerror = () => { image.hidden = true; if (fallback) fallback.hidden = false; };
    if (allowed) image.src = allowed;
    else image.removeAttribute('src');
  }

  function updateFormats() {
    const spotify = state.preview?.source === 'spotify' || (() => { try { return new URL($('#url').value).hostname === 'open.spotify.com'; } catch (_) { return false; } })();
    $('#format option[value="mp4"]').disabled = spotify;
    if (spotify && $('#format').value === 'mp4') $('#format').value = 'mp3';
    const video = $('#format').value === 'mp4';
    $('#quality-field').hidden = !video;
    $('#audio-note').hidden = video;
    $('#audio-note').textContent = {mp3: 'Áudio MP3 a 192 kbps. A qualidade depende da fonte.', m4a: 'Áudio AAC no formato M4A. A qualidade depende da fonte.', flac: 'FLAC preserva o áudio recebido. Não recupera qualidade perdida na fonte.'}[$('#format').value] || '';
  }
  $('#format').addEventListener('change', updateFormats);
  updateFormats();

  function scopeValue() { return $('#scope-field').hidden ? 'auto' : $('#scope').value; }

  function invalidatePreview() {
    state.sequence += 1;
    state.controller?.abort();
    state.preview = null;
    $('#preview-content').hidden = true;
    $('#preview-empty').hidden = false;
    $('#preview-empty strong').textContent = 'Seu próximo favorito aparece aqui';
    $('#preview-button').disabled = false;
    $('#preview-button').firstChild.textContent = 'Carregar prévia';
    $('.preview-panel').setAttribute('aria-busy', 'false');
    formError();
  }

  $('#url').addEventListener('input', () => {
    invalidatePreview();
    let mixed = false;
    try {
      const url = new URL($('#url').value.trim());
      mixed = ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'].includes(url.hostname) && url.searchParams.has('list') && (url.searchParams.has('v') || url.hostname.endsWith('youtu.be'));
    } catch (_) {}
    $('#scope-field').hidden = !mixed;
    updateFormats();
    clearTimeout(previewTimer);
    if ($('#url').validity.valid && $('#url').value) previewTimer = setTimeout(loadPreview, 850);
  });
  $('#scope').addEventListener('change', () => { invalidatePreview(); loadPreview(); });
  $('#preview-form').addEventListener('submit', event => { event.preventDefault(); clearTimeout(previewTimer); loadPreview(); });

  async function loadPreview() {
    const url = $('#url').value.trim();
    if (!url || !$('#url').reportValidity()) return;
    state.controller?.abort();
    const controller = new AbortController();
    state.controller = controller;
    const sequence = ++state.sequence;
    state.preview = null;
    $('#preview-content').hidden = true;
    $('#preview-empty').hidden = false;
    $('#preview-empty strong').textContent = 'Buscando miniatura…';
    $('#preview-button').disabled = true;
    $('#preview-button').firstChild.textContent = 'Carregando…';
    $('.preview-panel').setAttribute('aria-busy', 'true');
    formError();
    try {
      const preview = await api(`/preview?${new URLSearchParams({url, scope: scopeValue()})}`, {signal: controller.signal});
      if (sequence !== state.sequence) return;
      state.preview = preview;
      safeImage($('#preview-image'), preview.thumbnail, $('#preview-fallback'));
      $('#preview-image').alt = `Miniatura: ${preview.title}`;
      $('#preview-title').textContent = preview.title;
      $('#preview-author').textContent = preview.author + (preview.count ? ` · ${preview.count} ${preview.count === 1 ? 'item' : 'itens'}` : '');
      $('#preview-kind').textContent = kinds[preview.kind] || preview.kind;
      $('#preview-source').textContent = preview.source === 'youtube' ? 'YouTube' : 'Spotify';
      $('#preview-content').hidden = false;
      $('#preview-empty').hidden = true;
      updateFormats();
    } catch (error) {
      if (error.name !== 'AbortError' && sequence === state.sequence) {
        formError(error.message);
        $('#preview-empty strong').textContent = 'Não foi possível carregar a prévia';
      }
    } finally {
      if (sequence === state.sequence) {
        $('#preview-button').disabled = false;
        $('#preview-button').firstChild.textContent = 'Carregar prévia';
        $('.preview-panel').setAttribute('aria-busy', 'false');
      }
    }
  }

  $('#download-button').addEventListener('click', async () => {
    if (!state.preview) return;
    const preview = state.preview;
    $('#download-button').disabled = true;
    formError();
    try {
      await api('/jobs', {method: 'POST', body: JSON.stringify({owner, url: preview.url, scope: scopeValue(), format: $('#format').value, quality: $('#quality').value, title: preview.title, thumbnail: preview.thumbnail})});
      toast('Adicionado à fila. Você já pode carregar outro link.');
      setFilter('all');
      await refreshJobs();
    } catch (error) { formError(error.message); }
    finally { $('#download-button').disabled = false; }
  });

  function setFilter(filter) {
    state.filter = filter;
    document.querySelectorAll('[data-filter]').forEach(button => {
      button.classList.toggle('selected', button.dataset.filter === filter);
      button.setAttribute('aria-pressed', String(button.dataset.filter === filter));
    });
    const nav = filter === 'history' ? '#nav-history' : filter === 'active' ? '#nav-queue' : '#nav-home';
    document.querySelectorAll('.sidebar nav button').forEach(button => {
      button.classList.toggle('active', button === $(nav));
      if (button === $(nav)) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    });
    $('#queue-title').textContent = filter === 'history' ? 'Histórico de downloads' : 'Seus downloads';
    renderJobs();
  }
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => setFilter(button.dataset.filter)));
  $('#nav-home').addEventListener('click', () => { setFilter('all'); window.scrollTo({top: 0, behavior: 'smooth'}); });
  $('#nav-queue').addEventListener('click', () => { setFilter('active'); $('#queue-section').scrollIntoView({behavior: 'smooth'}); });
  $('#nav-history').addEventListener('click', () => { setFilter('history'); $('#queue-section').scrollIntoView({behavior: 'smooth'}); });

  function renderJobs() {
    const active = state.jobs.filter(job => !terminal.has(job.status));
    $('#stat-active').textContent = active.length;
    $('#nav-count').textContent = active.length;
    $('#stat-completed').textContent = state.jobs.filter(job => job.status === 'completed').length;
    $('#stat-files').textContent = new Set(state.jobs.flatMap(job => job.files)).size;
    const visible = state.jobs.filter(job => state.filter === 'all' || (state.filter === 'active' ? !terminal.has(job.status) : terminal.has(job.status)));
    const visibleIds = new Set(visible.map(job => job.id));
    for (const [id, node] of nodes) { node.hidden = !visibleIds.has(id); }
    $('#queue-empty').hidden = visible.length > 0;
    $('#queue-empty h3').textContent = state.filter === 'history' ? 'Seu histórico começa no primeiro download' : state.filter === 'active' ? 'Nenhum download em andamento' : 'Sua fila está pronta para começar';
    $('#queue-empty p').textContent = state.filter === 'active' ? 'Adicione outro link ou confira seus arquivos em Finalizados.' : 'Adicione seu primeiro link e seus downloads aparecerão aqui.';
    for (const job of visible) {
      let node = nodes.get(job.id);
      if (!node) {
        node = $('#job-template').content.firstElementChild.cloneNode(true);
        nodes.set(job.id, node);
        // New jobs are returned first; retain existing nodes and open details.
        const next = visible.slice(visible.indexOf(job) + 1).map(item => nodes.get(item.id)).find(Boolean);
        $('#job-list').insertBefore(node, next || null);
      }
      node.hidden = false;
      node.dataset.status = job.status;
      node.querySelector('h3').textContent = job.title;
      node.querySelector('h3').title = job.title;
      safeImage(node.querySelector('img'), job.thumbnail);
      node.querySelector('.job-badge').textContent = statusNames[job.status] || job.status;
      node.querySelector('.job-meta').textContent = `${job.source === 'youtube' ? 'YouTube' : 'Spotify'} · ${kinds[job.kind]} · ${job.format.toUpperCase()}${job.total ? ` · ${job.completed}/${job.total} itens` : ''}`;
      const progress = node.querySelector('progress');
      progress.value = job.progress;
      progress.setAttribute('aria-label', `Progresso de ${job.title}`);
      node.querySelector('.job-message').textContent = job.message;
      node.querySelector('.job-percent').textContent = `${Math.round(job.progress)}%`;
      const action = node.querySelector('.job-action');
      action.hidden = job.status === 'completed';
      action.textContent = terminal.has(job.status) ? 'Tentar novamente' : 'Cancelar';
      action.onclick = async () => {
        action.disabled = true;
        try {
          if (terminal.has(job.status)) {
            await api('/jobs', {method: 'POST', body: JSON.stringify({owner, url: job.url, scope: job.kind === 'playlist' && job.source === 'youtube' ? 'playlist' : 'auto', format: job.format, quality: job.quality, title: job.title, thumbnail: job.thumbnail})});
            toast('Nova tentativa adicionada à fila. Arquivos existentes serão aproveitados.');
          } else {
            await api(`/jobs/${job.id}/cancel?${new URLSearchParams({owner})}`, {method: 'POST'});
            toast('Cancelamento solicitado. A operação atual pode levar alguns instantes para parar.');
          }
          await refreshJobs();
        } catch (error) { toast(error.message); }
        finally { action.disabled = false; }
      };
      const files = node.querySelector('.job-files');
      files.hidden = !job.files.length;
      files.querySelector('summary').textContent = `${job.files.length} arquivo(s) salvo(s)`;
      if (files.dataset.count !== String(job.files.length)) {
        files.querySelector('ul').replaceChildren(...job.files.map((file, index) => {
          const li = document.createElement('li');
          const link = document.createElement('a');
          link.textContent = file.split(/[\\/]/).pop();
          link.href = `/api/gui/jobs/${job.id}/files/${index}?${new URLSearchParams({owner})}`;
          link.setAttribute('download', '');
          li.append(link);
          return li;
        }));
        files.dataset.count = String(job.files.length);
      }
      const errors = node.querySelector('.job-errors');
      errors.hidden = !job.errors.length;
      if (errors.dataset.count !== String(job.errors.length)) {
        errors.querySelector('ul').replaceChildren(...job.errors.map(error => { const li = document.createElement('li'); li.textContent = `Item ${error.item}: ${error.error}`; return li; }));
        errors.dataset.count = String(job.errors.length);
      }
    }
  }

  async function refreshJobs() {
    state.jobs = await api(`/jobs?${new URLSearchParams({owner})}`);
    for (const job of state.jobs) {
      if (statuses.has(job.id) && statuses.get(job.id) !== job.status && terminal.has(job.status)) {
        $('#announcer').textContent = `${job.title}: ${statusNames[job.status]}`;
      }
      statuses.set(job.id, job.status);
    }
    renderJobs();
  }

  async function poll() {
    try {
      if (!state.outputLoaded) {
        const config = await api('/config');
        $('#output-path').textContent = config.output;
        $('#settings-output').textContent = config.output;
        state.outputLoaded = true;
      }
      await refreshJobs();
      $('#connection').classList.remove('offline');
      $('#connection').lastChild.textContent = 'Servidor local ativo';
    } catch (_) {
      $('#connection').classList.add('offline');
      $('#connection').lastChild.textContent = 'Sem conexão · reconectando';
    } finally { setTimeout(poll, document.hidden ? 5000 : 1500); }
  }
  poll();
})();
