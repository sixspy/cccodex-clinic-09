import { api, chip, escapeHtml } from '../api.js';

export async function renderBackfill(el) {
  const [jobs, gapRows, deadLetters] = await Promise.all([
    api('/api/jobs'),
    api('/api/rows?status=' + encodeURIComponent('待补采')),
    api('/api/dead-letters'),
  ]);
  const summary = jobs.summary || {};
  const dlSummary = deadLetters.summary || {};
  const guard = deadLetters.guard || {};

  el.innerHTML = `
    <div class="card">
      <h2>缺口补采队列</h2>
      <p class="muted">对待补采/缺口访视行排队补采；执行成功会把载荷补齐、质量改合格并关闭关联质控发现。</p>
      <div class="grid">
        <div class="stat"><div class="n">${summary['排队'] || 0}</div><div class="l">排队</div></div>
        <div class="stat"><div class="n">${summary['执行中'] || 0}</div><div class="l">执行中</div></div>
        <div class="stat"><div class="n">${summary['成功'] || 0}</div><div class="l">成功</div></div>
        <div class="stat"><div class="n">${summary['失败'] || 0}</div><div class="l">失败</div></div>
      </div>
      <div class="row">
        <select id="statusFilter">
          <option value="">全部任务</option>
          <option>排队</option>
          <option>执行中</option>
          <option>成功</option>
          <option>失败</option>
        </select>
        <button id="reload" class="ghost">刷新</button>
      </div>
      <div id="msg" class="muted"></div>
    </div>
    <div class="card">
      <h2>死信回炉</h2>
      <p class="muted">补采连续失败到上限后当场冻成不可变快照并钉死回炉键；读侧只认这份快照。回炉会另起补采任务，不改快照本身。</p>
      <div class="grid">
        <div class="stat"><div class="n">${dlSummary.rework_badge || 0}</div><div class="l">回炉徽标（死信封存数）</div></div>
        <div class="stat"><div class="n">${dlSummary.dead_letter_count || 0}</div><div class="l">死信条数</div></div>
        <div class="stat"><div class="n">${dlSummary.pending_rework || 0}</div><div class="l">待回炉</div></div>
        <div class="stat"><div class="n">${dlSummary.landing_rows || 0}</div><div class="l">回炉落点行次</div></div>
      </div>
      ${guard.rejected
        ? `<div class="detail-box err">死信静默已挡：回炉徽标 ${guard.rework_badge} 对照死信条数 ${guard.dead_letter_count}，落点行 ${guard.landing_rows}。${guard.consistent ? '' : '徽标与条数拧花，已驳回；'}${guard.silent_blocked ? '有死信却无回炉落点行，已驳回。' : ''}</div>`
        : '<div class="detail-box">回炉徽标与死信条数对齐，回炉落点行已入表。</div>'}
      <div class="row">
        <select id="dlStatusFilter">
          <option value="">全部死信</option>
          <option value="封存">仅封存</option>
          <option value="已回炉">仅已回炉</option>
        </select>
        <button id="dlReload" class="ghost">刷新死信</button>
      </div>
      <table>
        <thead>
          <tr><th>ID</th><th>钉死键</th><th>行</th><th>中心批次</th><th>状态</th><th>回炉落点</th><th>指纹</th><th>操作</th></tr>
        </thead>
        <tbody id="deadBody">
          ${(deadLetters.items || []).map(deadRow).join('') || '<tr><td colspan="8" class="muted">暂无死信</td></tr>'}
        </tbody>
      </table>
      <div id="deadDetail" class="detail-box" style="margin-top:10px">点击「详情」查看已封存快照。</div>
    </div>
    <div class="card">
      <h2>待补采访视行（可一键排队）</h2>
      <table>
        <thead><tr><th>行ID</th><th>批次</th><th>中心</th><th>受试者访视</th><th>质量</th><th>操作</th></tr></thead>
        <tbody>
          ${(gapRows.items || []).map((r) => `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.batch_code)}</td>
              <td>${escapeHtml(r.site_name || '')}</td>
              <td>${escapeHtml(r.subject_key)} / ${escapeHtml(r.visit_code)}</td>
              <td>${chip(r.quality)}</td>
              <td><button data-enq="${r.id}">加入补采队列</button></td>
            </tr>`).join('') || '<tr><td colspan="6" class="muted">当前没有待补采行</td></tr>'}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>补采任务</h2>
      <table>
        <thead>
          <tr>
            <th>任务号</th><th>行</th><th>中心批次</th><th>状态</th>
            <th>尝试</th><th>消息</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="jobBody">
          ${(jobs.items || []).map(jobRow).join('') || '<tr><td colspan="7" class="muted">暂无任务</td></tr>'}
        </tbody>
      </table>
    </div>`;

  function jobRow(j) {
    return `
      <tr>
        <td>${escapeHtml(j.job_code)}</td>
        <td>#${j.row_id} ${escapeHtml(j.subject_key)}/${escapeHtml(j.visit_code)}</td>
        <td>${escapeHtml(j.site_name || '')}<div class="muted">${escapeHtml(j.batch_code || '')}</div></td>
        <td>${chip(j.status)}</td>
        <td>${j.attempt}</td>
        <td class="muted">${escapeHtml(j.message || '')}</td>
        <td class="row">
          ${j.status === '排队' || j.status === '失败' ? `<button data-run="${j.id}">执行成功</button>` : ''}
          ${j.status === '排队' ? `<button data-fail="${j.id}" class="danger">模拟失败</button>` : ''}
          ${j.status === '失败' ? `<button data-retry="${j.id}" class="warn">重试排队</button>` : ''}
          <button data-view="${j.id}" class="ghost">详情</button>
        </td>
      </tr>`;
  }

  const msg = el.querySelector('#msg');
  const jobBody = el.querySelector('#jobBody');
  const deadBody = el.querySelector('#deadBody');
  const deadDetail = el.querySelector('#deadDetail');

  function deadRow(d) {
    const snap = d.snapshot || {};
    return `
      <tr>
        <td>${d.id}</td>
        <td><code>${escapeHtml(d.pin_key)}</code></td>
        <td>#${d.row_id} ${escapeHtml(snap.subject_key || '')}/${escapeHtml(snap.visit_code || '')}</td>
        <td>${escapeHtml(snap.site_name || '')}<div class="muted">${escapeHtml(snap.batch_code || '')}</div></td>
        <td>${chip(d.status)}</td>
        <td>${d.rework_job_id ? `#${d.rework_job_id}` : '<span class="muted">未回炉</span>'}</td>
        <td>${d.fingerprint_ok ? chip('指纹一致') : '<span class="chip bad">指纹不符</span>'}<div class="muted">${escapeHtml((d.fingerprint || '').slice(0, 10))}…</div></td>
        <td class="row">
          ${d.status === '封存'
            ? `<button data-rework="${d.id}" class="warn">回炉</button>`
            : `<button disabled class="ghost">已回炉</button>`}
          <button data-dlview="${d.id}" class="ghost">详情</button>
        </td>
      </tr>`;
  }

  function bindDeadLetters() {
    el.querySelectorAll('[data-rework]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/dead-letters/${btn.dataset.rework}/rework`, {
            method: 'POST',
            body: '{}',
          });
          await renderBackfill(el);
        } catch (e) {
          msg.className = 'err';
          msg.textContent = e.message;
        }
      };
    });
    el.querySelectorAll('[data-dlview]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          const d = await api(`/api/dead-letters/${btn.dataset.dlview}`);
          deadDetail.classList.remove('err');
          deadDetail.textContent = JSON.stringify(d, null, 2);
        } catch (e) {
          deadDetail.className = 'detail-box err';
          deadDetail.textContent = e.message;
        }
      };
    });
  }

  async function reloadDeadLetters() {
    const status = el.querySelector('#dlStatusFilter').value;
    const url = status
      ? `/api/dead-letters?status=${encodeURIComponent(status)}`
      : '/api/dead-letters';
    const data = await api(url);
    deadBody.innerHTML = (data.items || []).map(deadRow).join('') ||
      '<tr><td colspan="8" class="muted">暂无死信</td></tr>';
    bindDeadLetters();
  }

  async function reloadJobs() {
    const status = el.querySelector('#statusFilter').value;
    const url = status ? `/api/jobs?status=${encodeURIComponent(status)}` : '/api/jobs';
    const data = await api(url);
    jobBody.innerHTML = (data.items || []).map(jobRow).join('') || '<tr><td colspan="7" class="muted">暂无任务</td></tr>';
    bindJobs();
  }

  function bindJobs() {
    el.querySelectorAll('[data-run]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/jobs/${btn.dataset.run}/run`, {
            method: 'POST',
            body: JSON.stringify({ succeed: true }),
          });
          msg.className = 'muted';
          msg.textContent = `任务 ${btn.dataset.run} 执行成功`;
          await renderBackfill(el);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-fail]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/jobs/${btn.dataset.fail}/run`, {
            method: 'POST',
            body: JSON.stringify({ succeed: false }),
          });
          await renderBackfill(el);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-retry]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/jobs/${btn.dataset.retry}/retry`, { method: 'POST', body: '{}' });
          await renderBackfill(el);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
    el.querySelectorAll('[data-view]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          const j = await api(`/api/jobs/${btn.dataset.view}`);
          msg.className = 'muted';
          msg.textContent = JSON.stringify(j, null, 2);
        } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
      };
    });
  }

  el.querySelectorAll('[data-enq]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await api('/api/jobs', {
          method: 'POST',
          body: JSON.stringify({ row_id: Number(btn.dataset.enq) }),
        });
        await renderBackfill(el);
      } catch (e) { msg.textContent = e.message; msg.className = 'err'; }
    };
  });
  el.querySelector('#reload').onclick = () => reloadJobs().catch((e) => { msg.textContent = e.message; msg.className = 'err'; });
  el.querySelector('#statusFilter').onchange = () => reloadJobs();
  el.querySelector('#dlReload').onclick = () => reloadDeadLetters().catch((e) => { msg.textContent = e.message; msg.className = 'err'; });
  el.querySelector('#dlStatusFilter').onchange = () => reloadDeadLetters();
  bindJobs();
  bindDeadLetters();
}
