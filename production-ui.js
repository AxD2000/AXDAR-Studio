let confirmedCharges = null;
let serviceStatus = null;
let serviceStatusLoading = false;
let prodDraft = {
    minutes: 60,
    retries: 2,
    shotRetries: 1,
    budget: '',
    episodeBudget: '',
    monthBudget: '',
    rate: '',
    expectedWidth: '',
    expectedHeight: '',
    policy: 'manual',
    backupDrive: false,
    webhook: false,
    visual: false,
    lifecycle: 'manual',
};

const activeRemoteStatuses = [
    'waiting for connection', 'reconnecting', 'deploying',
    'starting worker', 'checking ComfyUI and requirements', 'running',
];
const terminalRemoteStatuses = [
    'finished', 'needs attention', 'stopped', 'paused', 'waiting', 'limit reached',
];

async function refreshServiceStatus() {
    if (serviceStatusLoading) return;
    serviceStatusLoading = true;
    try {
        serviceStatus = await post('/production', { action: 'service-status' });
        if (page === 3) render();
    } catch {
        serviceStatus = { driveConnected: false, webhookSaved: false, visualSaved: false };
    } finally {
        serviceStatusLoading = false;
    }
}

function productionMetrics() {
    const p = state.production || {}, money = value => '$' + Number(value || 0).toFixed(3);
    return '<section class="panel"><h2>Production costs</h2>' +
        (p.rate != null ? '<p>Rental rate ' + money(p.rate) + '/hour</p>' : '') +
        (p.instanceUptimeMinutes != null ? '<p>Vast-reported uptime ' + Number(p.instanceUptimeMinutes).toFixed(0) + ' min</p>' : '') +
        (p.elapsed != null ? '<p>Session ' + Math.round(p.elapsed / 60) + ' min · Rendering ' + Math.round((p.renderSeconds || 0) / 60) + ' min · Other time ' + Math.round((p.overheadSeconds || 0) / 60) + ' min · Retry time ' + Math.round((p.retrySeconds || 0) / 60) + ' min</p>' +
        '<p>Estimated session ' + money(p.estimate) + ' · Episode ' + money(p.projectEstimate) + ' · Month ' + money(p.monthlyEstimate) + '</p>' +
        '<p>' + Math.round(p.acceptedSeconds || 0) + ' s accepted' + (p.acceptedSecondsPerDollar ? ' · ' + Number(p.acceptedSecondsPerDollar).toFixed(1) + ' accepted seconds per estimated dollar' : '') + '</p>' : '') +
        (p.monthlyBudgetSource ? '<p>' + esc(p.monthlyBudgetSource) + '</p>' : '') +
        (confirmedCharges ? '<p>Vast confirmed charges (' + esc(confirmedCharges.month) + ' UTC) · Account ' + money(confirmedCharges.confirmedAccount) + (confirmedCharges.confirmedInstance != null ? ' · Instance ' + money(confirmedCharges.confirmedInstance) : '') + '</p>' : '') +
        '<button data-vast-charges>Read confirmed Vast charges</button></section>';
}

function remoteProductionPanel() {
    if (serviceStatus === null && !serviceStatusLoading) refreshServiceStatus();
    const p = state.production || {};
    const c = state.connection || {};
    const svc = serviceStatus || {};
    const approved = ep.shots.filter(s => s.approved).length;
    const busy = activeRemoteStatuses.includes(p.status);
    const hasRun = Boolean(p.status && p.status !== 'stopped');
    const canStart = c.state === 'Ready' && approved > 0 && !state.running && !busy && !state.workflowError;
    const isTerminal = terminalRemoteStatuses.includes(p.status);
    const lifecycle = p.lifecycle || {};
    const backup = p.backup || {};
    const notifications = Array.isArray(p.notifications) ? p.notifications : [];
    const shotsMap = p.shots || {};
    const failedShots = Object.entries(shotsMap).filter(([, st]) => st === 'error').map(([id]) => id);
    const countdownActive = lifecycle.status === 'countdown';
    const remainingMin = countdownActive && lifecycle.deadline ? Math.max(0, Math.ceil((lifecycle.deadline - Date.now() / 1000) / 60)) : null;

    const statusBlock = p.status ? (
        '<div class="panel" style="margin-top:12px">' +
        '<h3>Remote worker status · ' + esc(p.status) + (p.maintenance ? ' · maintenance: ' + esc(p.maintenance) : '') + '</h3>' +
        (p.warning ? '<p class="muted">' + esc(p.warning) + '</p>' : '') +
        (p.error ? '<p class="danger">' + esc(localize(p.error)) + '</p>' : '') +
        (p.total != null ? '<p>Shots tracked: ' + Object.keys(shotsMap).length + '/' + p.total +
            (p.acceptedTakes != null ? ' · Accepted: ' + p.acceptedTakes : '') + '</p>' : '') +
        (backup.state ? '<p>Google Drive archive · <strong>' + esc(backup.state) + '</strong>' + (backup.error ? ' · ' + esc(backup.error) : '') + '</p>' : '') +
        (lifecycle.status ? '<p>Termination lifecycle · <strong>' + esc(lifecycle.status) + '</strong>' +
            (remainingMin != null ? ' · ~' + remainingMin + ' min remaining (rehearsal)' : '') +
            (lifecycle.reason ? ' · ' + esc(lifecycle.reason) : '') + '</p>' : '') +
        '<div class="row">' +
        '<button data-prod-action="sync" ' + (!hasRun ? 'disabled' : '') + '>Sync remote status</button>' +
        '<button data-prod-action="pause" ' + (!busy ? 'disabled' : '') + '>Pause after current shot</button>' +
        '<button data-prod-action="resume" ' + (busy || !hasRun ? 'disabled' : '') + '>Resume remote run</button>' +
        '<button data-prod-action="stop" ' + (!hasRun ? 'disabled' : '') + '>Stop remote run</button>' +
        (isTerminal && !p.maintenance ? '<button data-prod-action="maintenance">Retry backup &amp; notifications</button>' : '') +
        (countdownActive ? '<button class="primary" data-prod-action="cancel-countdown">Cancel 10-min countdown</button>' : '') +
        (p.healthy === false && hasRun ? '<button data-prod-action="archive">Archive disconnected session</button>' : '') +
        '</div>' +
        (failedShots.length && isTerminal ? '<div class="row" style="margin-top:8px">' +
            failedShots.map(sid => {
                const shot = ep.shots.find(s => s.id === sid);
                return '<button data-prod-action="retry" data-prod-shot="' + esc(sid) + '">Retry failed: ' + esc(shot ? titleShot(shot) : sid.slice(0, 8)) + '</button>';
            }).join('') + '</div>' : '') +
        (notifications.length ? '<details style="margin-top:8px"><summary>Production events (' + notifications.length + ')</summary><ul>' +
            notifications.slice(-15).map(ev => '<li><strong>' + esc(ev.kind) + '</strong> · ' + esc(ev.delivery) +
                (ev.detail ? ' · ' + esc(ev.detail) : '') + (ev.error ? ' · ' + esc(ev.error) : '') + '</li>').join('') +
            '</ul></details>' : '') +
        '</div>'
    ) : '';

    return '<section class="panel" aria-labelledby="remoteProdTitle">' +
        '<h2 id="remoteProdTitle">Autonomous Vast production (Layer D)</h2>' +
        '<p>Runs detached on your Vast GPU. Closing AXDAR or restarting Windows will not interrupt the remote queue.</p>' +
        '<p>' + approved + ' approved shots in <strong>' + esc(ep.title) + '</strong> · Workflow: <strong>' + esc(state.workflow || 'None') + '</strong></p>' +
        (state.workflowError ? '<p class="danger">' + esc(state.workflowError) + '</p>' : '') +
        '<form id="remoteProdForm">' +
        '<div class="form-pair">' +
        '<label>Session limit (minutes)<input data-prod-field="minutes" type="number" min="2" max="1440" value="' + esc(prodDraft.minutes) + '" required></label>' +
        '<label>Network retries (0–5)<input data-prod-field="retries" type="number" min="0" max="5" value="' + esc(prodDraft.retries) + '" required></label>' +
        '<label>End-of-batch shot retries (0–5)<input data-prod-field="shotRetries" type="number" min="0" max="5" value="' + esc(prodDraft.shotRetries) + '" required></label>' +
        '<label>Session budget cap ($)<input data-prod-field="budget" type="number" min="0" step="0.01" placeholder="Optional" value="' + esc(prodDraft.budget) + '"></label>' +
        '<label>Episode budget cap ($)<input data-prod-field="episodeBudget" type="number" min="0" step="0.01" placeholder="Optional" value="' + esc(prodDraft.episodeBudget) + '"></label>' +
        '<label>Monthly budget cap ($)<input data-prod-field="monthBudget" type="number" min="0" step="0.01" placeholder="Optional" value="' + esc(prodDraft.monthBudget) + '"></label>' +
        '</div>' +
        '<details><summary>Advanced limits, resolution QC &amp; instance lifecycle</summary>' +
        '<div class="form-pair">' +
        '<label>Manual hourly rate ($/hr, if SSH-only)<input data-prod-field="rate" type="number" min="0" step="0.01" placeholder="Auto from Vast" value="' + esc(prodDraft.rate) + '"></label>' +
        '<label>Expected width (px)<input data-prod-field="expectedWidth" type="number" min="16" max="16384" placeholder="Optional" value="' + esc(prodDraft.expectedWidth) + '"></label>' +
        '<label>Expected height (px)<input data-prod-field="expectedHeight" type="number" min="16" max="16384" placeholder="Optional" value="' + esc(prodDraft.expectedHeight) + '"></label>' +
        '<label>When budget/time limit is reached<select data-prod-field="policy">' +
        option('manual', 'Keep instance running (manual stop)', prodDraft.policy) +
        option('stop-instance', 'Request Vast instance stop after reconciliation', prodDraft.policy) +
        '</select></label>' +
        '<label>Completion lifecycle policy<select data-prod-field="lifecycle">' +
        option('manual', 'Manual (leave instance running)', prodDraft.lifecycle) +
        option('rehearse', 'Rehearse 10-minute termination countdown', prodDraft.lifecycle) +
        '</select></label>' +
        '</div></details>' +
        '<div class="row" style="margin-top:8px">' +
        '<label><input type="checkbox" data-prod-check="backupDrive" ' + (prodDraft.backupDrive ? 'checked' : '') + ' ' + (!svc.driveConnected ? 'disabled' : '') + '> Archive takes to Google Drive' + (!svc.driveConnected ? ' (connect below)' : '') + '</label>' +
        '<label><input type="checkbox" data-prod-check="webhook" ' + (prodDraft.webhook ? 'checked' : '') + ' ' + (!svc.webhookSaved ? 'disabled' : '') + '> Send webhook notifications' + (!svc.webhookSaved ? ' (configure below)' : '') + '</label>' +
        '<label><input type="checkbox" data-prod-check="visual" ' + (prodDraft.visual ? 'checked' : '') + ' ' + (!svc.visualSaved ? 'disabled' : '') + '> External visual QC' + (!svc.visualSaved ? ' (configure below)' : '') + '</label>' +
        '</div>' +
        '<div class="row" style="margin-top:10px">' +
        '<button class="primary" type="submit" ' + (canStart ? '' : 'disabled') + '>Start Remote Production (Vast Worker)</button>' +
        '</div></form>' +
        statusBlock +
        '</section>';
}

function productionServicesPanel() {
    const svc = serviceStatus || {};
    return '<section class="panel" aria-labelledby="prodServicesTitle">' +
        '<h2 id="prodServicesTitle">Archive, review and notifications</h2>' +
        '<p role="status">Google Drive: <strong>' + (svc.driveConnected ? 'Connected (drive.file)' : 'Not connected') + '</strong> · ' +
        'Webhook: <strong>' + (svc.webhookSaved ? 'Saved in Vault' : 'Not set') + '</strong> · ' +
        'Visual reviewer: <strong>' + (svc.visualSaved ? 'Saved in Vault' : 'Not set') + '</strong></p>' +
        '<details><summary>Connect Google Drive &amp; configure HTTPS endpoints</summary>' +
        '<label>Google Desktop OAuth client JSON<input id="driveClientFile" type="file" accept=".json,application/json"></label>' +
        '<div class="row"><button type="button" data-prod-action="drive-refresh">Check Drive connection</button></div>' +
        '<form id="serviceSettingsForm" style="margin-top:10px"><div class="form-pair">' +
        '<label>Notification webhook URL (HTTPS)<input name="webhook" type="url" placeholder="https://..." autocomplete="off" dir="ltr"></label>' +
        '<label>Visual reviewer URL (HTTPS)<input name="visual" type="url" placeholder="https://..." autocomplete="off" dir="ltr"></label>' +
        '</div><button type="submit">Save endpoints in Windows Vault</button></form>' +
        '</details></section>';
}

function takeReview(v) {
    if (!v.review || v.status !== 'done') return '';
    const button = (action, label) => '<button data-take-review="' + action + '" data-review-shot="' + esc(v.id) + '" data-review-version="' + esc(v.version) + '">' + label + '</button>';
    const quality = v.quality || {};
    return '<p>Take review · ' + esc(v.review) + ' · Technical QC ' + esc(quality.technical || 'pending') +
        (quality.width ? ' · ' + quality.width + '×' + quality.height + ' · ' + quality.frames + ' decoded frames' : '') +
        ' · Visual ' + esc(quality.visual || 'review required') + (v.renderSeconds ? ' · ' + Math.round(v.renderSeconds) + ' s processing time' : '') + '</p>' +
        (quality.reason ? '<p>' + esc(quality.reason) + '</p>' : '') +
        (v.preview && !v.video ? '<video controls preload="metadata" src="' + esc(v.preview) + '"></video>' : '') +
        (!v.preview && !v.video ? button('preview', 'Load review preview') : '') +
        (quality.technical !== 'passed' ? button('recheck', 'Recheck technical QC') : '') +
        (quality.technical === 'passed' && (v.preview || v.video) && v.review !== 'accepted' ? button('accepted', 'Accept take') : '') +
        (v.review !== 'rejected' && (v.preview || v.video) ? button('rejected', 'Reject take') : '') +
        (v.video ? '<button data-prod-regenerate="' + esc(v.id) + '" data-prod-version="' + esc(v.version) + '">Regenerate from archived inputs</button>' : '');
}

function reportSubmission(result) {
    reportStatus(result.submitted + ' shot(s) queued.');
    const skipped = Object.entries(result.blocked || {});
    if (skipped.length) $('#notice').textContent = skipped.map(([id, reasons]) => {
        const shot = ep.shots.find(s => s.id === id);
        return (shot ? titleShot(shot) : id) + ': ' + reasons.join(' · ');
    }).join(' / ');
}

document.addEventListener('input', event => {
    const t = event.target;
    if (t.dataset.prodField) prodDraft[t.dataset.prodField] = t.value;
});

document.addEventListener('change', async event => {
    const t = event.target;
    if (t.dataset.prodField) prodDraft[t.dataset.prodField] = t.value;
    if (t.dataset.prodCheck) prodDraft[t.dataset.prodCheck] = t.checked;
    if (t.id === 'driveClientFile' && t.files.length) {
        const file = t.files[0];
        t.value = '';
        try {
            const client = JSON.parse((await file.text()).replace(/^\uFEFF/, ''));
            const resp = await post('/drive/connect', { client });
            window.open(resp.url, '_blank', 'noopener');
            reportStatus('Complete Google Drive authorization in the opened tab, then click Check Drive connection.');
        } catch (error) { reportError(error); }
    }
});

document.addEventListener('submit', async event => {
    const form = event.target;
    if (form.id === 'remoteProdForm') {
        event.preventDefault();
        const btn = form.querySelector('[type=submit]');
        btn.disabled = true;
        try {
            await flushEdits();
            const c = state.connection || {};
            const payload = {
                action: 'start',
                episode: ep.id,
                useConnection: true,
                instance: c.instance || undefined,
                minutes: Number(prodDraft.minutes || 60),
                retries: Number(prodDraft.retries || 2),
                shotRetries: Number(prodDraft.shotRetries || 1),
                budget: Number(prodDraft.budget || 0),
                episodeBudget: Number(prodDraft.episodeBudget || 0),
                monthBudget: Number(prodDraft.monthBudget || 0),
                rate: prodDraft.rate ? Number(prodDraft.rate) : 0,
                expectedWidth: prodDraft.expectedWidth ? Number(prodDraft.expectedWidth) : 0,
                expectedHeight: prodDraft.expectedHeight ? Number(prodDraft.expectedHeight) : 0,
                policy: prodDraft.policy,
                lifecycle: prodDraft.lifecycle,
                backupDrive: Boolean(prodDraft.backupDrive),
                webhook: Boolean(prodDraft.webhook),
                visual: Boolean(prodDraft.visual),
            };
            const result = await post('/production', payload);
            state = await (await fetch('/status')).json();
            render();
            reportSubmission(result);
        } catch (error) { reportError(error); }
        finally { if (btn.isConnected) btn.disabled = false; }
        return;
    }
    if (form.id === 'serviceSettingsForm') {
        event.preventDefault();
        const btn = form.querySelector('[type=submit]');
        btn.disabled = true;
        try {
            const data = Object.fromEntries(new FormData(form));
            const body = { action: 'service-settings' };
            if (data.webhook?.trim()) body.webhook = data.webhook.trim();
            if (data.visual?.trim()) body.visual = data.visual.trim();
            if (!body.webhook && !body.visual) throw Error('Enter at least one HTTPS endpoint URL.');
            await post('/production', body);
            form.reset();
            await refreshServiceStatus();
            reportStatus('Service endpoints saved in Windows Vault.');
        } catch (error) { reportError(error); }
        finally { if (btn.isConnected) btn.disabled = false; }
    }
});

document.addEventListener('click', async event => {
    const chargesBtn = event.target.closest('[data-vast-charges]');
    if (chargesBtn) {
        chargesBtn.disabled = true;
        try {
            confirmedCharges = await post('/production', { action: 'charges', instance: state.connection?.instance });
            render();
        } catch (error) { reportError(error); }
        finally { if (chargesBtn.isConnected) chargesBtn.disabled = false; }
        return;
    }

    const reviewBtn = event.target.closest('[data-take-review]');
    if (reviewBtn) {
        reviewBtn.disabled = true;
        try {
            const choice = reviewBtn.dataset.takeReview;
            await post('/production', {
                action: 'review', episode: ep.id, shot: reviewBtn.dataset.reviewShot,
                version: reviewBtn.dataset.reviewVersion,
                decision: ['accepted', 'rejected'].includes(choice) ? choice : null,
                recheck: choice === 'recheck',
            });
            state = await (await fetch('/status')).json();
            render();
        } catch (error) { reportError(error); }
        finally { if (reviewBtn.isConnected) reviewBtn.disabled = false; }
        return;
    }

    const regenBtn = event.target.closest('[data-prod-regenerate]');
    if (regenBtn) {
        regenBtn.disabled = true;
        try {
            const c = state.connection || {};
            await post('/production', {
                action: 'start', episode: ep.id, useConnection: true,
                instance: c.instance || undefined,
                archive: [regenBtn.dataset.prodRegenerate, regenBtn.dataset.prodVersion],
                minutes: Number(prodDraft.minutes || 60),
                retries: Number(prodDraft.retries || 2),
                shotRetries: Number(prodDraft.shotRetries || 1),
                budget: Number(prodDraft.budget || 0),
                episodeBudget: Number(prodDraft.episodeBudget || 0),
                monthBudget: Number(prodDraft.monthBudget || 0),
                rate: prodDraft.rate ? Number(prodDraft.rate) : 0,
                backupDrive: Boolean(prodDraft.backupDrive),
                webhook: Boolean(prodDraft.webhook),
                visual: Boolean(prodDraft.visual),
            });
            state = await (await fetch('/status')).json();
            render();
            reportStatus('Regeneration job submitted from archived inputs.');
        } catch (error) { reportError(error); }
        finally { if (regenBtn.isConnected) regenBtn.disabled = false; }
        return;
    }

    const prodBtn = event.target.closest('[data-prod-action]');
    if (!prodBtn) return;
    const action = prodBtn.dataset.prodAction;
    prodBtn.disabled = true;
    try {
        if (action === 'drive-refresh') {
            await refreshServiceStatus();
            reportStatus(serviceStatus?.driveConnected ? 'Google Drive is connected.' : 'Google Drive is not connected yet.');
            return;
        }
        const body = { action };
        if (action === 'retry') body.shot = prodBtn.dataset.prodShot;
        if (action === 'maintenance') {
            if (prodDraft.backupDrive) body.backupDrive = true;
            if (prodDraft.webhook) body.webhook = true;
            if (prodDraft.visual) body.visual = true;
        }
        await post('/production', body);
        state = await (await fetch('/status')).json();
        render();
        reportStatus('Production action "' + action + '" submitted.');
    } catch (error) { reportError(error); }
    finally { if (prodBtn.isConnected) prodBtn.disabled = false; }
});
