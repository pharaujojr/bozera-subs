// Formulário de provedores, usado no wizard e na aba Configurações.
(() => {
  const style = document.createElement('style');
  style.textContent = `
    .prov { border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; background: var(--surface-raised); }
    .prov + .prov { margin-top: 12px; }
    .prov-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .prov-name { font-weight: 600; font-size: 0.92rem; }
    .prov-name a { color: var(--muted); font-weight: 400; font-size: 0.74rem; margin-left: 6px; }
    .prov-toggle { margin-left: auto; display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 0.8rem; cursor: pointer; }
    .prov-toggle input { width: 16px; height: 16px; accent-color: var(--accent); }
    .prov-help { color: var(--muted); font-size: 0.76rem; margin-top: 4px; }
    .prov-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; margin-top: 12px; }
    .prov-fields label { display: flex; flex-direction: column; gap: 5px; color: var(--muted); font-size: 0.76rem; }
    .prov-fields input { font: inherit; font-size: 0.85rem; color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 9px; padding: 9px 11px; outline: none; min-width: 0; width: 100%; }
    .prov-fields input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(76, 141, 255, 0.3); }
    .prov-actions { display: flex; align-items: center; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
    .prov-test { background: var(--surface); border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 7px 12px; font: inherit; font-size: 0.78rem; font-weight: 600; cursor: pointer; }
    .prov-test:hover { border-color: var(--accent); }
    .prov-test:disabled { opacity: 0.5; cursor: wait; }
    .prov-result { font-size: 0.78rem; color: var(--muted); }
    .prov-result.ok { color: #2fd27a; }
    .prov-result.fail { color: #f0525b; }
    .prov-clear { background: none; border: 0; color: var(--muted); font: inherit; font-size: 0.74rem; text-decoration: underline; cursor: pointer; padding: 0; }
  `;
  document.head.appendChild(style);

  async function send(path, method, body) {
    const res = await fetch(path, {
      method,
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Erro ${res.status}`);
    return data;
  }

  function fieldValues(card) {
    const values = {};
    card.querySelectorAll('input[data-field]').forEach(input => {
      values[input.dataset.field] = input.dataset.clear === '1' ? null : input.value;
    });
    return values;
  }

  function buildCard(provider) {
    const card = document.createElement('div');
    card.className = 'prov';
    card.dataset.provider = provider.name;

    const head = document.createElement('div');
    head.className = 'prov-head';
    const name = document.createElement('div');
    name.className = 'prov-name';
    name.textContent = provider.label;
    const link = document.createElement('a');
    link.href = provider.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = 'abrir site';
    name.appendChild(link);
    const toggle = document.createElement('label');
    toggle.className = 'prov-toggle';
    const enabled = document.createElement('input');
    enabled.type = 'checkbox';
    enabled.dataset.enabled = '1';
    enabled.checked = provider.enabled;
    toggle.append(enabled, 'Ativo');
    head.append(name, toggle);

    const help = document.createElement('div');
    help.className = 'prov-help';
    help.textContent = provider.help;

    const fields = document.createElement('div');
    fields.className = 'prov-fields';
    provider.fields.forEach(field => {
      const label = document.createElement('label');
      label.textContent = field.label;
      const input = document.createElement('input');
      input.dataset.field = field.name;
      input.type = field.secret ? 'password' : 'text';
      input.autocomplete = field.secret ? 'new-password' : 'off';
      input.spellcheck = false;
      input.value = field.secret ? '' : field.value;
      if (field.secret && field.has_value) input.placeholder = '•••••••• salvo; deixe vazio para manter';
      // Digitar já é o sinal de que a pessoa quer usar este provedor
      input.addEventListener('input', () => { if (input.value) enabled.checked = true; });
      label.appendChild(input);
      fields.appendChild(label);
    });

    const actions = document.createElement('div');
    actions.className = 'prov-actions';
    const test = document.createElement('button');
    test.type = 'button';
    test.className = 'prov-test';
    test.textContent = 'Testar';
    const result = document.createElement('span');
    result.className = 'prov-result';
    result.setAttribute('role', 'status');
    test.addEventListener('click', async () => {
      test.disabled = true;
      result.className = 'prov-result';
      result.textContent = 'Testando…';
      try {
        const values = fieldValues(card);
        Object.keys(values).forEach(key => { if (values[key] === null) values[key] = ''; });
        const answer = await send(`/api/settings/test/${provider.name}`, 'POST', { fields: values });
        result.className = 'prov-result ' + (answer.ok ? 'ok' : 'fail');
        result.textContent = answer.message;
      } catch (err) {
        result.className = 'prov-result fail';
        result.textContent = err.message;
      } finally {
        test.disabled = false;
      }
    });
    actions.append(test, result);

    if (provider.fields.some(f => f.secret && f.has_value)) {
      const clear = document.createElement('button');
      clear.type = 'button';
      clear.className = 'prov-clear';
      clear.textContent = 'Apagar dados salvos';
      clear.addEventListener('click', () => {
        card.querySelectorAll('input[data-field]').forEach(input => {
          input.value = '';
          input.dataset.clear = '1';
          input.placeholder = 'Será apagado ao salvar';
        });
        enabled.checked = false;
      });
      actions.appendChild(clear);
    }

    card.append(head, help, fields, actions);
    return card;
  }

  window.ProviderForm = {
    async mount(container) {
      const settings = await send('/api/settings', 'GET');
      container.innerHTML = '';
      settings.providers.forEach(provider => container.appendChild(buildCard(provider)));
      return settings;
    },
    collect(container) {
      const providers = {};
      container.querySelectorAll('.prov').forEach(card => {
        providers[card.dataset.provider] = {
          enabled: card.querySelector('input[data-enabled]').checked,
          fields: fieldValues(card),
        };
      });
      return providers;
    },
    save(body) {
      return send('/api/settings', 'PUT', body);
    },
    send,
  };
})();
