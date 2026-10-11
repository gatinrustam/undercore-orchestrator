'use strict';

const $ = id => document.getElementById(id);
let nodeOffset = 0;
let connectionOffset = 0;
let loading = false;
let management = false;
const modes = {
  active: 'Принимает подключения',
  draining: 'Без новых подключений',
  disabled: 'Отключён',
};
const states = {
  assigned: 'Назначено',
  pending_create: 'Создание не завершено',
  pending_switch: 'Переключение',
  denied: 'Доступ запрещён',
};
const reasons = {
  ok: 'Успешно', timeout: 'Таймаут', deadline: 'Срок операции истёк',
  saturated: 'Лимит нагрузки', tls: 'Ошибка TLS', network: 'Ошибка сети',
  upstream_status: 'Ответ агента', invalid_response: 'Некорректный ответ',
  identity_mismatch: 'Несовпадение узла', waiting: 'Ожидание',
  unavailable: 'Недоступно', rejected: 'Отклонено', internal_error: 'Ошибка',
  cancelled: 'Отменено', unauthorized: 'Нет авторизации', validation: 'Некорректный запрос',
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function badge(text, style = 'neutral') {
  return element('span', text, 'pill ' + style);
}

function date(value) {
  if (!value) return '—';
  return new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('ru-RU');
}

function table(id, rows, columns, empty) {
  const body = $(id);
  body.replaceChildren();
  if (!rows.length) {
    const cell = element('td', empty, 'empty');
    cell.colSpan = columns;
    const row = element('tr');
    row.append(cell);
    body.append(row);
    return;
  }
  rows.forEach(cells => {
    const row = element('tr');
    cells.forEach(value => {
      const cell = element('td');
      cell.append(value instanceof Node ? value : document.createTextNode(String(value ?? '—')));
      row.append(cell);
    });
    body.append(row);
  });
}

function showLogin() {
  $('dashboard').hidden = true;
  $('login').hidden = false;
  $('operator-dialog').close();
  $('operator-fields').replaceChildren();
  ['nodes-body', 'connections-body', 'events-body', 'stats'].forEach(id => $(id).replaceChildren());
}

async function request(path, options = {}) {
  const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options});
  if (response.status === 401) {
    showLogin();
    throw Error('Введите ключ доступа к панели.');
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    if (detail.detail && path === '/admin/v1/commands') {
      const error = Error('Операция не завершена: ' + detail.detail + '. Проверьте журнал перед повтором.');
      error.code = detail.detail;
      throw error;
    }
    throw Error(response.status === 429
      ? 'Слишком много попыток. Подождите минуту.'
      : 'Не удалось получить данные. Проверьте, запущен ли оркестратор и доступен ли журнал.');
  }
  return response.json();
}

function pagination(kind, offset, total, size) {
  $(kind + '-range').textContent = total
    ? `${Math.min(offset + 1, total)}–${Math.min(offset + size, total)} из ${total}`
    : 'Нет записей';
  $(kind + '-prev').disabled = offset === 0;
  $(kind + '-next').disabled = offset + size >= total;
}

function leaseBadge(node) {
  if (!node.lease_enabled) return badge('Не используется');
  if (node.fenced) return badge('Выдача заблокирована', 'warning');
  if (!node.lease_verified) return badge('Не подтверждено');
  if (node.lease_valid_until * 1000 <= Date.now()) return badge('Истекло');
  return badge('Подтверждено');
}

function render(data) {
  $('stats').replaceChildren();
  const stats = [
    ['Серверов', data.nodes_total],
    ['Назначений', data.connections_total],
    ['Незавершённых операций', data.pending_creates + data.pending_switches],
    ['Запретов доступа', data.denied],
  ];
  stats.forEach(([label, value]) => {
    const card = element('div', undefined, 'stat');
    card.append(element('span', label), element('strong', value));
    $('stats').append(card);
  });
  table('nodes-body', data.nodes.map(node => [
    node.id,
    `${node.region} / ${node.protocol}`,
    badge(modes[node.mode]),
    `${node.assignments} / ${node.capacity}${node.pending_targets ? ' · целей: ' + node.pending_targets : ''}`,
    leaseBadge(node),
  ]), 5, 'Серверов пока нет. Добавьте узел через локальный CLI.');
  table('connections-body', data.connections.map(connection => [
    connection.connection_id,
    connection.node_id,
    badge(states[connection.state], connection.state === 'denied' ? 'warning' : 'neutral'),
    connection.revision,
    connection.last_operation
      ? `${connection.last_operation} · ${connection.last_outcome === 'ok' ? 'успешно' : connection.last_outcome === 'error' ? 'ошибка' : '—'}`
      : '—',
    date(connection.checked_at),
  ]), 6, 'Подключений пока нет. Новые назначения появятся здесь автоматически.');
  table('events-body', data.events.map(event => [
    date(event.timestamp),
    `${event.stage} / ${event.action}`,
    badge(reasons[event.reason] || event.reason, event.reason === 'ok' ? 'neutral' : 'warning'),
    `${Math.round(event.duration_ms)} мс`,
    event.request_id,
  ]), 5, 'Событий пока нет.');
  $('event-notice').textContent = data.events_available
    ? 'Ограниченная выборка событий процессов API и worker. Длительность относится к этапу, а не ко всему подключению.'
    : 'Журнал событий пока недоступен. Сводка базы отображается независимо от него.';
  pagination('nodes', data.node_offset, data.nodes_total, data.page_size);
  pagination('connections', data.connection_offset, data.connections_total, data.page_size);
  $('updated').textContent = 'Снимок журнала · ' + date(data.generated_at);
  renderManagement(data, management);
  $('dashboard').hidden = false;
  $('login').hidden = true;
}

async function refresh() {
  if (loading) return;
  loading = true;
  $('refresh').disabled = true;
  try {
    const [data, capabilities] = await Promise.all([
      request(`/admin/v1/overview?node_offset=${nodeOffset}&connection_offset=${connectionOffset}`),
      request('/admin/v1/capabilities'),
    ]);
    management = capabilities.management;
    render(data);
    $('message').textContent = '';
  } catch (error) {
    $('message').textContent = error.message;
    if (!$('dashboard').hidden) $('updated').textContent = 'Данные устарели · обновление не удалось';
  } finally {
    loading = false;
    $('refresh').disabled = false;
  }
}

$('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const value = $('token').value;
  $('token').value = '';
  try {
    await request('/session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: value}),
    });
    nodeOffset = connectionOffset = 0;
    await refresh();
  } catch (error) {
    $('message').textContent = error.message;
  }
});
$('refresh').addEventListener('click', refresh);
$('logout').addEventListener('click', async () => {
  if (loading) return;
  loading = true;
  try {
    await request('/admin/v1/logout', {method: 'POST'});
    showLogin();
    $('message').textContent = '';
  } catch (error) {
    $('message').textContent = error.message;
  } finally {
    loading = false;
  }
});
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-tab]').forEach(other => {
    other.setAttribute('aria-pressed', String(other === button));
  });
  document.querySelectorAll('.tab').forEach(section => {
    section.hidden = section.id !== button.dataset.tab;
  });
}));
for (const kind of ['nodes', 'connections']) {
  for (const direction of ['prev', 'next']) {
    $(kind + '-' + direction).addEventListener('click', () => {
      if (loading) return;
      const delta = direction === 'next' ? 50 : -50;
      if (kind === 'nodes') nodeOffset = Math.max(0, nodeOffset + delta);
      else connectionOffset = Math.max(0, connectionOffset + delta);
      refresh();
    });
  }
}
setInterval(() => {
  if (!document.hidden && !$('dashboard').hidden) refresh();
}, 15000);
refresh();
