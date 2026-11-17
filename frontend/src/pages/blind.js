import {blind} from '../api/blind.js';
import {datasets} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc, fmt, date as fmtTime, notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const RULE_TEXT = {current: '现行规则', candidate: '候选规则'};
const canWrite = () => session.user.role !== 'viewer';
const isAdmin = () => session.user.role === 'admin';

function dayState(d) {
  if (d.actual === null || d.actual === undefined) return {cls: 'pending', text: '待收齐实领'};
  if (d.excluded_reason) return {cls: 'excluded', text: d.excluded_reason};
  if (d.scored) return {cls: 'scored', text: '已评分 v' + d.score_version};
  return {cls: 'pending', text: '未参评'};
}

export async function blindPage() {
  workspace.datasets = await datasets.list();
  const campaigns = await blind.listCampaigns();
  workspace.blindCampaigns = campaigns;
  const selectedId = workspace.blindSelected || campaigns[0]?.id;
  const detail = selectedId ? await blind.getCampaign(selectedId) : null;
  workspace.blindSelected = selectedId;

  const createCard = `
  <section class="panel">
    <div class="section-title"><h2>发起盲评考核</h2><span>登记双方参数与晋级门槛</span></div>
    <form id="campaign-form">
      <label>考核名称<input name="name" required minlength="2" value="候选规则盲评 · 批次考核"></label>
      <label>数据批次<select name="dataset_id" required>
        ${workspace.datasets.map(d => `<option value="${d.id}">${esc(d.name)} · ${d.rows} 条</option>`).join('')}
      </select></label>
      <div class="parameter-grid">
        <label>考核开始日<input name="start_date" type="date" required></label>
        <label>考核结束日<input name="end_date" type="date" required></label>
      </div>
      <div class="parameter-grid">
        <label>开餐锁报时刻(点)<input name="lock_hour" type="number" min="0" max="23" value="10"></label>
        <label>候选 α<input name="alpha" type="number" step="any" min="0.1" max="0.9" value="0.35"></label>
      </div>
      <div class="parameter-grid">
        <label>门槛指标<select name="metric">
          <option value="wape">WAPE（加权百分比误差）</option>
          <option value="mae">MAE（平均绝对误差）</option>
        </select></label>
        <label>最小改善(%)<input name="min_improve_pct" type="number" step="any" min="0" max="100" value="5"></label>
      </div>
      <label>最少评分天数<input name="min_scored_days" type="number" min="1" value="7"></label>
      <p class="hint">现行规则＝上周同日（无可调参数）；候选＝周期平滑（β=0.15、γ=0.20）。
      登记即冻结参数快照，之后评分与晋级只认快照。迟于开餐时刻锁报的预测不参评。</p>
      <button type="submit" class="primary" ${canWrite() ? '' : 'disabled'}>登记并生成考核日 ↗</button>
    </form>
  </section>`;

  if (!detail) {
    return heading('06 / BLIND EVALUATION', '候选规则盲评晋级',
      '先盲评、达标再接替现行规则；实领补齐才评分，更正后旧评分失效重算。') +
      `<div class="blind-grid">${createCard}<section class="panel empty">尚无考核单。先在左侧登记一场盲评考核。</section></div>`;
  }

  const cur = detail.current_rule, cand = detail.candidate_rule;
  const latestVersion = [...detail.score_versions].reverse().find(v => v.status === 'current');
  const score = latestVersion?.result?.score;
  const decision = latestVersion?.decision;

  const campaignHead = `
  <section class="panel blind-summary">
    <div class="section-title">
      <h2>${esc(detail.name)}</h2>
      <span class="status-badge ${esc(detail.active_rule)}">现行：${esc(detail.active_rule_label)}</span>
    </div>
    <div class="blind-meta">
      <div><span>考核期</span><strong>${esc(detail.start_date)} → ${esc(detail.end_date)}</strong></div>
      <div><span>开餐锁报</span><strong>每日 ${detail.lock_hour}:00 前</strong></div>
      <div><span>状态</span><strong>${detail.status === 'closed' ? '已关闭' : '进行中'}</strong></div>
      <div><span>待收齐实领</span><strong>${detail.pending_actual_days.length} 天</strong></div>
    </div>
    <div class="rule-snapshots">
      <div class="snapshot ${detail.active_rule === 'current' ? 'active-rule' : ''}">
        <p class="eyebrow">${esc(cur.label)}</p>
        <pre>${esc(JSON.stringify(cur.params, null, 2))}</pre>
      </div>
      <div class="snapshot ${detail.active_rule === 'candidate' ? 'active-rule' : ''}">
        <p class="eyebrow">${esc(cand.label)}</p>
        <pre>${esc(JSON.stringify(cand.params, null, 2))}</pre>
      </div>
      <div class="snapshot threshold">
        <p class="eyebrow">晋级门槛（冻结）</p>
        <pre>${esc(JSON.stringify(detail.threshold, null, 2))}</pre>
      </div>
    </div>
    <div class="blind-actions">
      <button class="button" id="btn-prelock" ${canWrite() ? '' : 'disabled'}>开餐前预锁整窗双方预测 ↻</button>
      <button class="button" id="btn-rescore" ${canWrite() ? '' : 'disabled'}>中断恢复 · 幂等重算</button>
      <button class="button primary" id="btn-promote" ${isAdmin() ? '' : 'disabled'}>按当前评分手动晋级</button>
      <button class="button" id="btn-close" ${isAdmin() ? '' : 'disabled'}>关闭考核</button>
    </div>
  </section>`;

  const scoreCard = score ? `
  <section class="panel">
    <div class="section-title"><h2>当前有效评分</h2><span>版本 v${latestVersion.version} · 同份实领同口径</span></div>
    <div class="metrics">
      <div><span>已评分天数</span><strong>${score.scored_days}</strong></div>
      <div><span>现行 WAPE</span><strong>${fmt(score.current.wape)}<small>%</small></strong></div>
      <div><span>候选 WAPE</span><strong>${fmt(score.candidate.wape)}<small>%</small></strong></div>
      <div><span>现行 MAE</span><strong>${fmt(score.current.mae)}<small>份</small></strong></div>
      <div><span>候选 MAE</span><strong>${fmt(score.candidate.mae)}<small>份</small></strong></div>
    </div>
    <p class="decision-line ${decision?.would_promote ? 'pass' : 'fail'}">
      达标判定：${esc(decision?.reason || '暂无')}
    </p>
  </section>` : `<section class="panel empty">实领未收齐或无有效评分天，暂不能判定晋级。补齐实领后自动出分。</section>`;

  const daysRows = detail.days.map(d => {
    const st = dayState(d);
    return `<tr>
      <td>${esc(d.day)}</td>
      <td>${d.current_forecast === null ? '—' : fmt(d.current_forecast)}${d.current_late ? ' <em class="late">迟</em>' : ''}</td>
      <td>${d.candidate_forecast === null ? '—' : fmt(d.candidate_forecast)}${d.candidate_late ? ' <em class="late">迟</em>' : ''}</td>
      <td>${d.actual === null || d.actual === undefined ? '—' : fmt(d.actual)}</td>
      <td><span class="day-state ${st.cls}">${esc(st.text)}</span></td>
      <td class="row-actions">
        <button class="text-button" data-lock="${esc(d.day)}" ${canWrite() && d.current_forecast === null ? '' : 'disabled'}>锁双方</button>
        <button class="text-button" data-actual="${esc(d.day)}" ${canWrite() ? '' : 'disabled'}>${d.actual === null ? '补实领' : '更正实领'}</button>
      </td>
    </tr>`;
  }).join('');

  const daysCard = `
  <section class="panel">
    <div class="section-title"><h2>考核日追查</h2><span>${detail.days.length} 天 · 迟报不参评</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>日期</th><th>现行预测</th><th>候选预测</th><th>实领</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${daysRows}</tbody>
    </table></div>
  </section>`;

  const promoRows = detail.promotions.map(p => `
    <tr><td>${fmtTime(p.at)}</td><td><span class="promo-action ${esc(p.action)}">${p.action === 'promoted' ? '晋级' : '回退'}</span></td>
    <td>${esc(RULE_TEXT[p.from_rule] || p.from_rule)} → ${esc(RULE_TEXT[p.to_rule] || p.to_rule)}</td>
    <td>评分版本 #${p.score_version_id ?? '—'}</td><td class="reason-cell">${esc(p.reason)}</td><td>${esc(p.actor)}</td></tr>`).join('');
  const promoCard = `
  <section class="panel">
    <div class="section-title"><h2>晋级 / 回退流水</h2><span>每次采用依据可追查</span></div>
    ${detail.promotions.length ? `<div class="table-wrap"><table>
      <thead><tr><th>时间</th><th>动作</th><th>规则切换</th><th>依据版本</th><th>采用理由</th><th>操作者</th></tr></thead>
      <tbody>${promoRows}</tbody></table></div>` : '<p class="empty">尚未发生规则切换。候选达标后自动晋级，更正后不达标自动回退。</p>'}
  </section>`;

  const versionRows = detail.score_versions.slice().reverse().map(v => `
    <tr><td>v${v.version}</td><td>${v.status === 'current' ? '当前有效' : '已作废'}</td>
    <td>${v.result?.score?.scored_days ?? 0} 天</td>
    <td class="reason-cell">${esc(v.decision?.reason || '')}</td>
    <td class="reason-cell">${esc(JSON.stringify(v.basis))}</td><td>${fmtTime(v.created_at)}</td></tr>`).join('');
  const versionCard = `
  <section class="panel">
    <div class="section-title"><h2>评分版本留痕</h2><span>更正后旧版失效、全量重算</span></div>
    ${detail.score_versions.length ? `<div class="table-wrap"><table>
      <thead><tr><th>版本</th><th>状态</th><th>评分天</th><th>判定理由</th><th>触发依据</th><th>时间</th></tr></thead>
      <tbody>${versionRows}</tbody></table></div>` : '<p class="empty">补齐首个实领后生成第一版评分。</p>'}
  </section>`;

  const trialRows = detail.trials.map(t => `
    <tr><td>${esc(t.name)}</td><td>${esc(t.target_day)}</td>
    <td><span class="status-badge ${esc(t.bound_rule)}">${esc(t.bound_rule_label)}</span></td>
    <td>${esc({queued: '排队中', running: '计算中', done: '已完成', failed: '失败'}[t.status] || t.status)}</td>
    <td>${t.result ? fmt(t.result.forecast) : '—'}</td>
    <td class="row-actions">${t.status === 'failed' ? `<button class="text-button" data-retry="${t.id}">重试</button>` : '—'}</td></tr>`).join('');
  const trialCard = `
  <section class="panel">
    <div class="section-title"><h2>试算绑定</h2><span>创建即冻结当时现行规则，切换不改排队试算</span></div>
    <form id="trial-form" class="inline-form">
      <input name="name" required minlength="2" placeholder="试算名称" value="规则试算">
      <input name="target_day" type="date" required>
      <button class="button primary" ${canWrite() ? '' : 'disabled'}>新建试算</button>
    </form>
    ${detail.trials.length ? `<div class="table-wrap"><table>
      <thead><tr><th>名称</th><th>目标日</th><th>绑定规则</th><th>状态</th><th>预测</th><th>操作</th></tr></thead>
      <tbody>${trialRows}</tbody></table></div>` : '<p class="empty">尚无试算。</p>'}
  </section>`;

  const switcher = `
  <section class="panel">
    <div class="section-title"><h2>考核单</h2><span>${campaigns.length} 场</span></div>
    <div class="campaign-switcher">
      ${campaigns.map(c => `<button class="batch-item ${c.id === detail.id ? 'selected' : ''}" data-campaign="${c.id}">
        <strong>${esc(c.name)}</strong><span>${esc(c.start_date)}→${esc(c.end_date)} · 现行：${esc(c.active_rule_label)}</span>
      </button>`).join('')}
    </div>
  </section>`;

  return heading('06 / BLIND EVALUATION', '候选规则盲评晋级',
    '先盲评、达标再接替现行规则；迟报不参评，实领补齐才评分，更正失效重算、不达标自动回退。') +
    `<div class="blind-grid">${createCard}
      <div>${switcher}${campaignHead}${scoreCard}${daysCard}${promoCard}${versionCard}${trialCard}</div>
    </div>`;
}

export function bindBlind(refresh) {
  const form = document.querySelector('#campaign-form');
  if (form) submit(form, async f => {
    const payload = {
      name: f.get('name'), dataset_id: Number(f.get('dataset_id')),
      start_date: f.get('start_date'), end_date: f.get('end_date'),
      lock_hour: Number(f.get('lock_hour')),
      candidate_params: {alpha: Number(f.get('alpha'))},
      threshold: {metric: f.get('metric'), min_improve_pct: Number(f.get('min_improve_pct')),
        min_scored_days: Number(f.get('min_scored_days'))},
    };
    const c = await blind.createCampaign(payload);
    workspace.blindSelected = c.id;
    notify('考核已登记，考核日与参数快照已冻结');
    await refresh();
  });

  const cid = workspace.blindSelected;
  if (!cid) return;

  document.querySelector('#btn-prelock')?.addEventListener('click', async () => {
    try { await blind.prelockWindow(cid); notify('整窗双方盲预测已锁定（已锁日期保持不变）'); await refresh(); }
    catch (e) { notify(e.message, true); }
  });
  document.querySelector('#btn-rescore')?.addEventListener('click', async () => {
    try { await blind.rescore(cid); notify('已幂等重算，未重复计分或晋级'); await refresh(); }
    catch (e) { notify(e.message, true); }
  });
  document.querySelector('#btn-promote')?.addEventListener('click', async () => {
    try { await blind.promote(cid); notify('已按当前有效评分晋级'); await refresh(); }
    catch (e) { notify(e.message, true); }
  });
  document.querySelector('#btn-close')?.addEventListener('click', async () => {
    try { await blind.closeCampaign(cid); notify('考核已关闭'); await refresh(); }
    catch (e) { notify(e.message, true); }
  });

  document.querySelectorAll('[data-campaign]').forEach(b => b.addEventListener('click', async () => {
    workspace.blindSelected = Number(b.dataset.campaign); await refresh();
  }));

  document.querySelectorAll('[data-lock]').forEach(b => b.addEventListener('click', async () => {
    try { await blind.lockForecast(cid, {day: b.dataset.lock}); notify('双方预测已锁定'); await refresh(); }
    catch (e) { notify(e.message, true); }
  }));

  document.querySelectorAll('[data-actual]').forEach(b => b.addEventListener('click', async () => {
    const value = prompt('请输入 ' + b.dataset.actual + ' 的实领份数（再次提交即更正，旧评分将失效重算）');
    if (value === null) return;
    try {
      await blind.reportActual(cid, {day: b.dataset.actual, value: Number(value)});
      notify('实领已记录，评分已按同份实领主算'); await refresh();
    } catch (e) { notify(e.message, true); }
  }));

  document.querySelectorAll('[data-retry]').forEach(b => b.addEventListener('click', async () => {
    try { await blind.retryTrial(b.dataset.retry); notify('试算已重新执行'); await refresh(); }
    catch (e) { notify(e.message, true); }
  }));

  const trialForm = document.querySelector('#trial-form');
  if (trialForm) submit(trialForm, async f => {
    await blind.createTrial({campaign_id: cid, name: f.get('name'), target_day: f.get('target_day')});
    notify('试算已创建并绑定创建时现行规则'); await refresh();
  });
}
