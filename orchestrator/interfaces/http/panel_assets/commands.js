'use strict';

// Native controls; no agent credential is persisted or included in a result.
let submitOperation;
let operatorBusy = false;

function inputField(name, label, value = '', type = 'text') {
  const wrapper = element('div', undefined, 'field');
  const input = element('input');
  input.name = name;
  input.id = 'field-' + name;
  input.type = type;
  input.value = value;
  input.required = true;
  if (type === 'password') input.autocomplete = 'new-password';
  const caption = element('label', label);
  caption.htmlFor = input.id;
  wrapper.append(caption, input);
  $('operator-fields').append(wrapper);
  return input;
}

function choiceField(name, label, choices, selected) {
  const wrapper = element('div', undefined, 'field');
  const select = element('select');
  select.name = name;
  select.id = 'field-' + name;
  choices.forEach(([value, text]) => {
    const option = element('option', text);
    option.value = value;
    select.append(option);
  });
  select.value = selected;
  const caption = element('label', label);
  caption.htmlFor = select.id;
  wrapper.append(caption, select);
  $('operator-fields').append(wrapper);
}

function openOperator(title, notice, submit) {
  if (operatorBusy) return false;
  $('operator-fields').replaceChildren();
  $('operator-title').textContent = title;
  $('operator-notice').textContent = notice;
  $('operator-error').textContent = '';
  $('operator-submit').hidden = !submit;
  $('operator-submit').disabled = false;
  $('operator-cancel').disabled = false;
  submitOperation = submit;
  $('operator-dialog').showModal();
  return true;
}

async function sendCommand(action, target, data) {
  // Each explicit submission is distinct; retries after ambiguous outcomes require review.
  return request('/admin/v1/commands', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({operation_id: crypto.randomUUID(), action, target, data}),
  });
}

function editNode(node) {
  if (!openOperator('Сервер ' + node.id,
    '«Без новых подключений» сохраняет текущие назначения. «Отключён» запрещает новые выдачи параметров, но не отзывает существующие туннели.', async form => {
      return sendCommand('node.mode', node.id, {
        expected_revision: node.revision, mode: form.get('mode'), capacity: Number(form.get('capacity')),
      });
    })) return;
  choiceField('mode', 'Режим', Object.entries(modes), node.mode);
  const capacity = inputField('capacity', 'Лимит назначений', node.capacity, 'number');
  capacity.min = 1;
  capacity.max = 100000;
}

function revokeConnection(connection) {
  openOperator('Отозвать доступ ' + connection.connection_id,
    'Будет сохранён запрет доступа и запрошено отключение на узле. Если узел недоступен, отключение может ожидать восстановления или истечения разрешения. Подтвердите действие.',
    () => sendCommand('connection.disable', connection.connection_id, {}));
}

function renderManagement(data, enabled) {
  $('management').hidden = !enabled;
  document.querySelector('.read-only').textContent = enabled ? 'Управление' : 'Только просмотр';
  if (!enabled) return;
  data.nodes.forEach((node, index) => {
    const button = element('button', 'Изменить', 'secondary row-action');
    button.addEventListener('click', () => editNode(node));
    $('nodes-body').children[index].firstElementChild.append(button);
  });
  data.connections.forEach((connection, index) => {
    const button = element('button', 'Отозвать доступ', 'secondary row-action');
    button.disabled = connection.state === 'denied';
    button.addEventListener('click', () => revokeConnection(connection));
    $('connections-body').children[index].firstElementChild.append(button);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  $('operator-cancel').addEventListener('click', () => {
    if (!operatorBusy) {
      $('operator-dialog').close();
      $('operator-fields').replaceChildren();
    }
  });
  $('operator-dialog').addEventListener('cancel', event => {
    if (operatorBusy) event.preventDefault();
    else $('operator-fields').replaceChildren();
  });
  $('operator-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (operatorBusy || !submitOperation) return;
    operatorBusy = true;
    $('operator-submit').disabled = true;
    $('operator-cancel').disabled = true;
    try {
      const result = await submitOperation(new FormData(event.target));
      $('operator-dialog').close();
      $('operator-fields').replaceChildren();
      await refresh();
      $('message').textContent = result.status === 'restart_required'
        ? 'Политики сохранены. Перезапустите API и фоновые службы через CLI, чтобы применить изменения.'
        : result.status === 'disable_requested'
          ? 'Запрет доступа сохранён; запрос отключения обработан.'
          : 'Изменения сохранены.';
    } catch (error) {
      $('operator-error').textContent = error.message;
      // Do not silently issue a new operation after an uncertain result.
      $('operator-submit').hidden = true;
    } finally {
      event.target.querySelectorAll('input[type=password]').forEach(input => input.value = '');
      operatorBusy = false;
      $('operator-cancel').disabled = false;
    }
  });
  $('add-node').addEventListener('click', () => {
    if (!openOperator('Добавить VPN-сервер',
      'Нужен уже установленный агент с HTTPS. Оркестратор проверит идентичность сервера. VPN и агент эта форма не устанавливает.', async form => {
        const id = form.get('id');
        return sendCommand('node.save', id, {
          id, expected_revision: 0, api_url: form.get('api_url'), api_key: form.get('api_key'),
          server_id: form.get('server_id'), region: form.get('region'), capacity: Number(form.get('capacity')),
          protocol: 'amneziawg', mode: 'draining', lease_enabled: form.get('lease_enabled') === 'true',
        });
      })) return;
    inputField('id', 'ID узла');
    inputField('api_url', 'HTTPS URL агента', '', 'url');
    inputField('server_id', 'Server ID агента');
    inputField('api_key', 'Ключ агента (только для записи)', '', 'password');
    inputField('region', 'Регион', 'nl');
    const capacity = inputField('capacity', 'Лимит назначений', 100, 'number');
    capacity.min = 1;
    capacity.max = 100000;
    choiceField('lease_enabled', 'Короткие разрешения — агент должен поддерживать их',
      [['false', 'Не включать'], ['true', 'Включить (отменить включение нельзя)']], 'false');
  });
  $('edit-policy').addEventListener('click', async () => {
    try {
      const value = await request('/admin/v1/policy');
      if (!openOperator('Политики оркестратора',
        'Сохраняются с проверкой ревизии. Текущие процессы продолжат использовать прежние значения до перезапуска. Единицы указаны в именах параметров.', async form => {
          const policy = structuredClone(value.policy);
          for (const [group, entries] of Object.entries(policy)) {
            for (const key of Object.keys(entries)) policy[group][key] = Number(form.get(group + '.' + key));
          }
          return sendCommand('policy.set', 'runtime', {expected_revision: value.revision, policy});
        })) return;
      for (const [group, entries] of Object.entries(value.policy)) {
        $('operator-fields').append(element('h3', group));
        for (const [key, number] of Object.entries(entries)) inputField(group + '.' + key, key, number, 'number');
      }
    } catch (error) { $('message').textContent = error.message; }
  });
  $('show-audit').addEventListener('click', async () => {
    try {
      const value = await request('/admin/v1/audit');
      if (!openOperator('Журнал действий',
        'Последние 100 команд. started — результат неизвестен, failed — нужна проверка; это не доказательство отсутствия изменений. cli/panel обозначает канал, а не отдельного человека.', null)) return;
      value.entries.forEach(entry => {
        const item = element('article', undefined, 'audit-entry');
        item.append(element('strong', entry.action + ' · ' + entry.target),
          element('p', `${date(entry.started_at)} · ${entry.actor} · ${entry.outcome}`),
          element('code', entry.operation_id));
        $('operator-fields').append(item);
      });
      if (!value.entries.length) $('operator-fields').textContent = 'Команд пока нет.';
    } catch (error) { $('message').textContent = error.message; }
  });
});
