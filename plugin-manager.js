(function() {
  'use strict';

  // Helper to create elements with optional text
  function create(tag, opts = {}) {
    const el = document.createElement(tag);
    if (opts.text) el.textContent = opts.text;
    if (opts.class) el.className = opts.class;
    if (opts.attrs) {
      Object.entries(opts.attrs).forEach(([k, v]) => el.setAttribute(k, v));
    }
    return el;
  }

  // Request tracking to avoid overlapping requests
  const pending = new Set();

  function keyFor(op, id, version, action) {
    return action ? `${op}:${id}:${version}:${action}` : `${op}:${id}:${version}`;
  }

  function addPending(op, id, version, action) {
    pending.add(keyFor(op, id, version, action));
  }

  function removePending(op, id, version, action) {
    pending.delete(keyFor(op, id, version, action));
  }

  function canProceed(op, id, version, action) {
    return !pending.has(keyFor(op, id, version, action));
  }

  // Main entry: wait for DOM ready
  document.addEventListener('DOMContentLoaded', () => {
    const main = document.querySelector('main');
    if (!main) return;
    const app = document.querySelector('#app');
    if (!app) return;

    // Create the plugin manager details element
    const details = create('details', { attrs: { id: 'plugin-manager' } });
    const summary = create('summary', { text: 'Plugin Manager' });
    details.appendChild(summary);

    // Container for dynamic content
    const container = create('div', { attrs: { class: 'pm-container' } });
    details.appendChild(container);

    // Insert after #app
    main.insertBefore(details, app.nextSibling || null);

    // Load list when opened
    details.addEventListener('toggle', () => {
      if (details.open) {
        loadList(container);
      }
    });
  });

  // Fetch plugin list and render
  async function loadList(container) {
    container.innerHTML = '';
    const loading = create('p', { text: 'Loading plugin list...' });
    container.appendChild(loading);

    try {
      const resp = await post('/plugins', { op: 'list' });
      if (!resp.ok) throw resp.error;
      const data = resp.data;
      renderList(container, data);
    } catch (e) {
      container.innerHTML = '';
      const err = create('p', { text: `Error: ${e.code || e.message || e}` });
      err.style.color = 'red';
      container.appendChild(err);
    }
  }

  // Render catalog and installed sections
  function renderList(container, data) {
    container.innerHTML = '';

    // Catalog section
    const catalogSection = create('section');
    catalogSection.appendChild(create('h2', { text: 'Catalog' }));
    const catalogList = create('ul');
    catalogSection.appendChild(catalogList);
    data.catalog.forEach(entry => {
      const li = create('li');
      li.dataset.id = entry.id;
      li.dataset.version = entry.version;

      const idSpan = create('span', { text: `ID: ${entry.id}` });
      const verSpan = create('span', { text: `  Version: ${entry.version}` });
      const shaSpan = create('span', { text: `  SHA: ${entry.digest}` });

      li.appendChild(idSpan);
      li.appendChild(verSpan);
      li.appendChild(shaSpan);

      const installBtn = create('button', { text: 'Install', attrs: { 'data-id': entry.id, 'data-version': entry.version } });
      installBtn.addEventListener('click', () => handleInstall(entry, installBtn, container));
      li.appendChild(installBtn);

      catalogList.appendChild(li);
    });
    container.appendChild(catalogSection);

    // Installed section
    const installedSection = create('section');
    installedSection.appendChild(create('h2', { text: 'Installed' }));
    const installedList = create('ul');
    installedSection.appendChild(installedList);

    data.installed.forEach(entry => {
      const li = create('li');
      li.dataset.id = entry.id;
      li.dataset.version = entry.version;

      const idSpan = create('span', { text: `ID: ${entry.id}` });
      const verSpan = create('span', { text: `  Version: ${entry.version}` });
      const shaSpan = create('span', { text: `  SHA: ${entry.digest}` });
      const activeSpan = create('span', { text: `  Active: ${entry.active}` });

      li.appendChild(idSpan);
      li.appendChild(verSpan);
      li.appendChild(shaSpan);
      li.appendChild(activeSpan);

      // Buttons
      const enableBtn = create('button', { text: 'Enable', attrs: { 'data-id': entry.id, 'data-version': entry.version } });
      const disableBtn = create('button', { text: 'Disable', attrs: { 'data-id': entry.id, 'data-version': entry.version } });
      const rollbackBtn = create('button', { text: 'Rollback', attrs: { 'data-id': entry.id, 'data-version': entry.version } });
      const removeBtn = create('button', { text: 'Remove', attrs: { 'data-id': entry.id, 'data-version': entry.version } });
      const refreshBtn = create('button', { text: 'Refresh', attrs: { 'data-id': entry.id, 'data-version': entry.version } });
      enableBtn.disabled = entry.active;
      disableBtn.disabled = !entry.active;
      rollbackBtn.disabled = !entry.active;
      removeBtn.disabled = entry.active;

      enableBtn.addEventListener('click', () => handleEnable(entry, enableBtn, container));
      disableBtn.addEventListener('click', () => handleDisable(entry, disableBtn, container));
      rollbackBtn.addEventListener('click', () => handleRollback(entry, rollbackBtn, container));
      removeBtn.addEventListener('click', () => handleRemove(entry, removeBtn, container));
      refreshBtn.addEventListener('click', () => loadList(container));

      li.appendChild(enableBtn);
      li.appendChild(disableBtn);
      li.appendChild(rollbackBtn);
      li.appendChild(removeBtn);
      li.appendChild(refreshBtn);

      // Actions for active plugins
      if (entry.active && Array.isArray(entry.actions)) {
        const actionsDiv = create('div', { attrs: { class: 'pm-actions' } });
        entry.actions.forEach(act => {
          const actDiv = create('div', { attrs: { class: 'pm-action' } });
          const label = create('label', { text: act.label });
          label.setAttribute('for', `act-input-${entry.id}-${entry.version}-${act.id}`);
          const input = create('input', { attrs: { type: 'text', id: `act-input-${entry.id}-${entry.version}-${act.id}` } });
          const runBtn = create('button', { text: 'Run', attrs: { 'data-id': entry.id, 'data-version': entry.version, 'data-action': act.id } });
          const result = create('span', { attrs: { class: 'pm-result', 'data-id': entry.id, 'data-version': entry.version, 'data-action': act.id } });

          runBtn.addEventListener('click', () => handleInvoke(entry, act, input, result, runBtn, container));

          actDiv.appendChild(label);
          actDiv.appendChild(input);
          actDiv.appendChild(runBtn);
          actDiv.appendChild(result);
          actionsDiv.appendChild(actDiv);
        });
        li.appendChild(actionsDiv);
      }

      installedList.appendChild(li);
    });

    container.appendChild(installedSection);
  }

  // Action handlers
  async function handleInstall(entry, button, container) {
    if (!canProceed('install', entry.id, entry.version)) return;
    addPending('install', entry.id, entry.version);
    button.disabled = true;
    try {
      const resp = await post('/plugins', { op: 'install', id: entry.id, version: entry.version, digest: entry.digest });
      if (!resp.ok) throw resp.error;
      await loadList(container);
    } catch (e) {
      showError(button, e);
    } finally {
      removePending('install', entry.id, entry.version);
      button.disabled = false;
    }
  }

  async function handleEnable(entry, button, container) {
    if (!confirm(`Allow ${entry.id}@${entry.version} to execute local code as your user?\nSHA-256: ${entry.digest}`)) return;
    if (!canProceed('enable', entry.id, entry.version)) return;
    addPending('enable', entry.id, entry.version);
    button.disabled = true;
    try {
      const resp = await post('/plugins', { op: 'enable', id: entry.id, version: entry.version, digest: entry.digest, approved: true });
      if (!resp.ok) throw resp.error;
      await loadList(container);
    } catch (e) {
      showError(button, e);
    } finally {
      removePending('enable', entry.id, entry.version);
      button.disabled = false;
    }
  }

  async function handleDisable(entry, button, container) {
    if (!canProceed('disable', entry.id, entry.version)) return;
    addPending('disable', entry.id, entry.version);
    button.disabled = true;
    try {
      const resp = await post('/plugins', { op: 'disable', id: entry.id, version: entry.version, digest: entry.digest });
      if (!resp.ok) throw resp.error;
      await loadList(container);
    } catch (e) {
      showError(button, e);
    } finally {
      removePending('disable', entry.id, entry.version);
      button.disabled = false;
    }
  }

  async function handleRollback(entry, button, container) {
    if (!canProceed('rollback', entry.id, entry.version)) return;
    addPending('rollback', entry.id, entry.version);
    button.disabled = true;
    try {
      const resp = await post('/plugins', { op: 'rollback', id: entry.id, version: entry.version, digest: entry.digest });
      if (!resp.ok) throw resp.error;
      await loadList(container);
    } catch (e) {
      showError(button, e);
    } finally {
      removePending('rollback', entry.id, entry.version);
      button.disabled = false;
    }
  }

  async function handleRemove(entry, button, container) {
    if (!confirm(`Remove ${entry.id}@${entry.version} permanently?`)) return;
    if (!canProceed('remove', entry.id, entry.version)) return;
    addPending('remove', entry.id, entry.version);
    button.disabled = true;
    try {
      const resp = await post('/plugins', { op: 'remove', id: entry.id, version: entry.version, digest: entry.digest });
      if (!resp.ok) throw resp.error;
      await loadList(container);
    } catch (e) {
      showError(button, e);
    } finally {
      removePending('remove', entry.id, entry.version);
      button.disabled = false;
    }
  }

  async function handleInvoke(entry, action, input, result, button, container) {
    const actionId = action.id;
    if (!canProceed('invoke', entry.id, entry.version, actionId)) return;
    addPending('invoke', entry.id, entry.version, actionId);
    button.disabled = true;
    result.textContent = '';
    try {
      const resp = await post('/plugins', { op: 'invoke', id: entry.id, version: entry.version, digest: entry.digest, action: actionId, text: input.value });
      if (!resp.ok) throw resp.error;
      result.textContent = resp.data.text;
    } catch (e) {
      result.textContent = `Error: ${e.code || e.message || e}`;
    } finally {
      removePending('invoke', entry.id, entry.version, actionId);
      button.disabled = false;
    }
  }

  function showError(element, err) {
    const msg = `Error: ${err.code || err.message || err}`;
    const existing = element.parentElement.querySelector('.pm-error');
    if (existing) existing.textContent = msg;
    else {
      const span = create('span', { attrs: { class: 'pm-error' }, text: msg });
      span.style.color = 'red';
      element.parentElement.appendChild(span);
    }
  }

})();
