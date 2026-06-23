"""
Gold Scorecard Editor
=====================
A local web editor for reviewing and correcting gold_scorecards.json.
Shows the original scorecard image side-by-side with parsed JSON fields for manual verification.

Usage:
    uv run python malayan_interstate_cricket/gold_editor.py
    Open http://localhost:8765 in your browser
"""

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).parent
GOLD_PATH = BASE_DIR / "gold_scorecards.json"
SCORECARDS_PATH = BASE_DIR / "site" / "scorecards.json"
PAGES_DIR = BASE_DIR / "pages"
PORT = 8765

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold Scorecard Editor</title>
<style>
:root {
  --bg: #f5f5f0;
  --card: #fff;
  --border: #d0c8b8;
  --accent: #2d5016;
  --accent-light: #e8f0e0;
  --danger: #8b1a1a;
  --danger-light: #fce8e8;
  --text: #1a1a1a;
  --muted: #6b6b6b;
  --verified: #2d7d2d;
  --source-bg: #fefef6;
  --mono: 'SF Mono', 'Consolas', 'Monaco', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }

/* Header */
.header { background: var(--accent); color: #fff; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 18px; font-weight: 600; }
.header .stats { font-size: 14px; opacity: 0.85; }

/* Layout */
.container { max-width: 1600px; margin: 0 auto; padding: 16px; }

/* Match list */
.match-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
.match-item {
  display: flex; align-items: center; gap: 12px; padding: 10px 14px;
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  cursor: pointer; transition: all 0.15s;
}
.match-item:hover { border-color: var(--accent); background: var(--accent-light); }
.match-item.active { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent); }
.match-item.verified { border-left: 4px solid var(--verified); }
.match-item .page { font-family: var(--mono); font-size: 13px; color: var(--muted); min-width: 50px; }
.match-item .teams { font-weight: 500; flex: 1; }
.match-item .date { font-size: 13px; color: var(--muted); }
.match-item .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.match-item .badge.verified { background: #e0f0e0; color: var(--verified); }
.match-item .badge.unverified { background: #f0e8d8; color: #8b6914; }

/* Editor */
.editor { display: none; }
.editor.active { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

.panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.panel-header { padding: 10px 16px; font-weight: 600; font-size: 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
.panel-header.source { background: var(--source-bg); }

/* Source panel */
.source-frame-wrap {
  background: var(--source-bg); height: calc(100vh - 200px); overflow: hidden;
  display: flex; flex-direction: column;
}
.source-frame-wrap iframe {
  flex: 1; width: 100%; border: none;
}
.source-tabs {
  display: flex; gap: 0; border-bottom: 1px solid var(--border); background: #f0ede4;
}
.source-tab {
  padding: 6px 16px; font-size: 13px; cursor: pointer; border-bottom: 2px solid transparent;
}
.source-tab:hover { background: var(--accent-light); }
.source-tab.active { border-bottom-color: var(--accent); font-weight: 600; }
.source-view { display: none; flex: 1; overflow: hidden; }
.source-view.active { display: flex; flex-direction: column; }
.source-text {
  flex: 1; padding: 16px; font-family: var(--mono); font-size: 13px; line-height: 1.8;
  white-space: pre-wrap; word-break: break-word; background: var(--source-bg);
  overflow-y: auto;
}

/* Form */
.form-panel { max-height: calc(100vh - 200px); overflow-y: auto; }
.section { border-bottom: 1px solid var(--border); }
.section-header {
  padding: 8px 16px; font-size: 13px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.5px; color: var(--accent); background: var(--accent-light);
  cursor: pointer; display: flex; align-items: center; justify-content: space-between;
}
.section-header:hover { background: #d8e8d0; }
.section-body { padding: 12px 16px; }

.field { margin-bottom: 10px; }
.field label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.3px; }
.field input, .field select {
  width: 100%; padding: 6px 10px; border: 1px solid var(--border); border-radius: 4px;
  font-size: 14px; font-family: var(--mono);
}
.field input:focus, .field select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px rgba(45,80,22,0.15); }
.field input.changed { background: #fffde8; border-color: #c0a020; }

.row { display: flex; gap: 10px; }
.row > .field { flex: 1; }

/* Innings tabs */
.innings-tabs { display: flex; gap: 4px; padding: 8px 16px; background: #f8f8f4; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.innings-tab {
  padding: 5px 12px; font-size: 13px; border-radius: 4px; cursor: pointer;
  border: 1px solid var(--border); background: var(--card);
}
.innings-tab:hover { background: var(--accent-light); }
.innings-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.innings-content { display: none; }
.innings-content.active { display: block; }

/* Batting table */
table.batting { width: 100%; border-collapse: collapse; font-size: 13px; }
table.batting th { text-align: left; padding: 4px 6px; font-size: 11px; color: var(--muted); text-transform: uppercase; border-bottom: 2px solid var(--border); }
table.batting td { padding: 3px 6px; border-bottom: 1px solid #eee; }
table.batting input { width: 100%; border: 1px solid transparent; padding: 3px 6px; font-family: var(--mono); font-size: 13px; background: transparent; border-radius: 3px; }
table.batting input:hover { border-color: var(--border); }
table.batting input:focus { border-color: var(--accent); background: #fff; outline: none; }
table.batting input[type="number"] { width: 60px; }
table.batting input[type="checkbox"] { width: 18px; height: 18px; }
.batting-name { min-width: 140px; }
.batting-dismissal { min-width: 180px; }
.batting-runs { width: 60px; }

/* Bowling table */
table.bowling { width: 100%; border-collapse: collapse; font-size: 13px; }
table.bowling th { text-align: left; padding: 4px 6px; font-size: 11px; color: var(--muted); text-transform: uppercase; border-bottom: 2px solid var(--border); }
table.bowling td { padding: 3px 6px; border-bottom: 1px solid #eee; }
table.bowling input { width: 100%; border: 1px solid transparent; padding: 3px 6px; font-family: var(--mono); font-size: 13px; background: transparent; border-radius: 3px; }
table.bowling input:hover { border-color: var(--border); }
table.bowling input:focus { border-color: var(--accent); background: #fff; outline: none; }

/* Buttons */
.btn { padding: 8px 16px; border: none; border-radius: 5px; font-size: 14px; font-weight: 500; cursor: pointer; transition: all 0.15s; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: #1e3a0e; }
.btn-danger { background: var(--danger); color: #fff; }
.btn-danger:hover { background: #6b0e0e; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
.btn-outline:hover { background: #f0f0e8; }

.toolbar { display: flex; gap: 8px; padding: 12px 16px; background: #f8f8f4; border-top: 1px solid var(--border); position: sticky; bottom: 0; }

/* Add row */
.add-row { padding: 6px 16px; }
.add-row button { font-size: 12px; color: var(--accent); background: none; border: 1px dashed var(--accent); padding: 4px 12px; border-radius: 4px; cursor: pointer; }
.add-row button:hover { background: var(--accent-light); }

/* Delete button */
.del-btn { color: var(--danger); background: none; border: none; cursor: pointer; font-size: 16px; padding: 2px 6px; border-radius: 3px; }
.del-btn:hover { background: var(--danger-light); }

/* Toast */
.toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 20px; background: var(--accent); color: #fff; border-radius: 6px; font-size: 14px; z-index: 200; opacity: 0; transition: opacity 0.3s; pointer-events: none; }
.toast.show { opacity: 1; }

/* Validation */
.validation-issues { padding: 8px 16px; background: var(--danger-light); border-bottom: 1px solid #e0c0c0; font-size: 13px; }
.validation-issues ul { margin-left: 20px; }
.validation-issues li { margin: 2px 0; }

/* Back btn */
.back-btn { display: none; margin-bottom: 12px; }
.back-btn.active { display: inline-flex; align-items: center; gap: 6px; }

/* FOW */
.fow-input { font-family: var(--mono); font-size: 13px; padding: 6px 10px; width: 100%; }

/* Responsive */
@media (max-width: 1100px) {
  .editor.active { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<div class="header">
  <h1>Gold Scorecard Editor</h1>
  <div class="stats" id="stats"></div>
</div>

<div class="container">
  <button class="btn btn-outline back-btn" id="backBtn" onclick="showList()">&#8592; Back to list</button>

  <div class="match-list" id="matchList"></div>

  <div class="editor" id="editor">
    <div class="panel">
      <div class="panel-header source">Original Scorecard (page <span id="pageNum"></span>)</div>
      <div class="source-frame-wrap">
        <div class="source-tabs">
          <div class="source-tab active" onclick="switchSource('page')" id="srcTabPage">FlippingBook</div>
          <div class="source-tab" onclick="switchSource('text')" id="srcTabText">Raw Text</div>
        </div>
        <div class="source-view active" id="srcViewPage">
          <iframe id="sourceFrame" src="about:blank"></iframe>
        </div>
        <div class="source-view" id="srcViewText">
          <div class="source-text" id="sourceText"></div>
        </div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-header">
        Parsed Data
        <div>
          <label style="font-size:13px;font-weight:normal;cursor:pointer">
            <input type="checkbox" id="verifiedCheck" onchange="markDirty()"> Verified
          </label>
        </div>
      </div>
      <div class="form-panel" id="formPanel"></div>
      <div class="toolbar">
        <button class="btn btn-primary" onclick="saveCurrentMatch()">Save</button>
        <button class="btn btn-outline" onclick="revertCurrentMatch()">Revert</button>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let goldData = [];
let currentIndex = -1;
let dirty = false;
let originalJson = '';

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2000);
}

function markDirty() { dirty = true; }

// ── API ──

async function loadGold() {
  const resp = await fetch('/api/gold');
  goldData = await resp.json();
  updateStats();
  renderList();
}

function scorecardPageUrl(page) {
  const base = 'https://archive.acscricket.com/research/rm/malayan_interstate_cricket_1899-1957/rm_malayan_interstate_cricket_scorecards';
  return page === 1 ? `${base}/index.html` : `${base}/${page}/index.html`;
}

async function loadSourceText(page) {
  const resp = await fetch(`/api/source/${page}`);
  if (!resp.ok) return '(no source text available)';
  return await resp.text();
}

function switchSource(which) {
  document.querySelectorAll('.source-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.source-view').forEach(v => v.classList.remove('active'));
  document.getElementById(which === 'page' ? 'srcTabPage' : 'srcTabText').classList.add('active');
  document.getElementById(which === 'page' ? 'srcViewPage' : 'srcViewText').classList.add('active');
}

async function loadValidation(page) {
  const resp = await fetch(`/api/validate/${page}`);
  if (!resp.ok) return [];
  return await resp.json();
}

async function saveGold() {
  await fetch('/api/gold', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(goldData),
  });
}

// ── List ──

function updateStats() {
  const verified = goldData.filter(m => m.verified).length;
  document.getElementById('stats').textContent = `${verified} / ${goldData.length} verified`;
}

function renderList() {
  const el = document.getElementById('matchList');
  el.innerHTML = goldData.map((m, i) => {
    const match = m.match || {};
    const v = m.verified;
    return `<div class="match-item ${v ? 'verified' : ''}" onclick="openMatch(${i})">
      <span class="page">p${m.page}</span>
      <span class="teams">${esc(match.team1 || '?')} v ${esc(match.team2 || '?')}</span>
      <span class="date">${esc(match.date || '?')}</span>
      <span class="badge ${v ? 'verified' : 'unverified'}">${v ? 'Verified' : 'Unverified'}</span>
    </div>`;
  }).join('');
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function showList() {
  if (dirty && !confirm('Discard unsaved changes?')) return;
  currentIndex = -1;
  dirty = false;
  document.getElementById('matchList').style.display = '';
  document.getElementById('editor').classList.remove('active');
  document.getElementById('backBtn').classList.remove('active');
}

// ── Editor ──

async function openMatch(idx) {
  if (dirty && !confirm('Discard unsaved changes?')) return;
  currentIndex = idx;
  dirty = false;
  const m = goldData[idx];
  originalJson = JSON.stringify(m);

  document.getElementById('matchList').style.display = 'none';
  document.getElementById('editor').classList.add('active');
  document.getElementById('backBtn').classList.add('active');
  document.getElementById('pageNum').textContent = m.page;

  document.getElementById('sourceFrame').src = scorecardPageUrl(m.page);
  loadSourceText(m.page).then(text => {
    document.getElementById('sourceText').textContent = text;
  });

  document.getElementById('verifiedCheck').checked = !!m.verified;

  renderForm(m);

  // Show validation issues
  const issues = await loadValidation(m.page);
  renderValidation(issues);
}

function renderValidation(issues) {
  let existing = document.getElementById('validationBox');
  if (existing) existing.remove();
  if (!issues || issues.length === 0) return;

  const box = document.createElement('div');
  box.id = 'validationBox';
  box.className = 'validation-issues';
  box.innerHTML = `<strong>Validation issues (${issues.length}):</strong><ul>` +
    issues.map(i => `<li><strong>${i.check}</strong> (sev ${i.severity}): ${esc(i.innings || '')} — ${esc(i.detail || i.message || '')}</li>`).join('') +
    '</ul>';
  document.getElementById('formPanel').prepend(box);
}

function renderForm(m) {
  const panel = document.getElementById('formPanel');
  const match = m.match || {};

  let html = `
  <div class="section">
    <div class="section-header">Match Info</div>
    <div class="section-body">
      <div class="row">
        <div class="field"><label>Team 1</label><input id="f_team1" value="${attr(match.team1)}" onchange="markDirty()"></div>
        <div class="field"><label>Team 2</label><input id="f_team2" value="${attr(match.team2)}" onchange="markDirty()"></div>
      </div>
      <div class="field"><label>Venue</label><input id="f_venue" value="${attr(match.venue)}" onchange="markDirty()"></div>
      <div class="row">
        <div class="field"><label>Date</label><input id="f_date" value="${attr(match.date)}" onchange="markDirty()"></div>
        <div class="field"><label>Result</label><input id="f_result" value="${attr(match.result)}" onchange="markDirty()"></div>
      </div>
      <div class="row">
        <div class="field"><label>Balls per over</label><input id="f_bpo" type="number" value="${m.balls_per_over ?? ''}" onchange="markDirty()"></div>
        <div class="field"><label>Toss</label><input id="f_toss" value="${attr(m.toss)}" onchange="markDirty()"></div>
      </div>
      <div class="field"><label>Umpires (comma-separated)</label><input id="f_umpires" value="${attr((m.umpires || []).join(', '))}" onchange="markDirty()"></div>
      <div class="field"><label>Close of play</label><input id="f_close" value="${attr(m.close_of_play)}" onchange="markDirty()"></div>
      <div class="field"><label>Notes (one per line)</label><textarea id="f_notes" rows="2" style="width:100%;font-family:var(--mono);font-size:13px;padding:6px 10px;border:1px solid var(--border);border-radius:4px" onchange="markDirty()">${esc((m.notes || []).join('\n'))}</textarea></div>
    </div>
  </div>`;

  // Innings tabs
  const innings = m.innings || [];
  html += `<div class="innings-tabs" id="inningsTabs">`;
  innings.forEach((inn, i) => {
    html += `<div class="innings-tab ${i === 0 ? 'active' : ''}" onclick="switchInnings(${i})" id="innTab${i}">${esc(inn.team || '?')} (${inn.innings_number || i+1})<button onclick="event.stopPropagation();deleteInnings(${i})" style="margin-left:6px;background:none;border:none;cursor:pointer;color:inherit;opacity:0.7;font-size:15px;line-height:1;vertical-align:middle" title="Delete innings">&times;</button></div>`;
  });
  html += `<button class="btn btn-sm btn-outline" style="margin-left:auto;align-self:center;white-space:nowrap" onclick="addInnings()">+ Add Innings</button>`;
  html += `</div>`;

  innings.forEach((inn, i) => {
    html += renderInnings(inn, i, i === 0);
  });

  panel.innerHTML = html;
}

function renderInnings(inn, idx, active) {
  const batting = inn.batting || [];
  const bowling = inn.bowling || [];
  const extras = inn.extras || {};
  const total = inn.total || {};
  const fow = inn.fow;

  let html = `<div class="innings-content ${active ? 'active' : ''}" id="innContent${idx}">`;

  // Innings metadata
  html += `<div class="section">
    <div class="section-header">Innings Info</div>
    <div class="section-body">
      <div class="row">
        <div class="field"><label>Team</label><input id="inn_team_${idx}" value="${attr(inn.team)}" onchange="markDirty()"></div>
        <div class="field"><label>Innings Number</label><input id="inn_num_${idx}" type="number" value="${inn.innings_number ?? idx + 1}" onchange="markDirty()"></div>
      </div>
    </div>
  </div>`;

  // Batting
  html += `<div class="section">
    <div class="section-header">Batting</div>
    <div class="section-body">
    <table class="batting">
      <thead><tr><th></th><th class="batting-name">Name</th><th>C</th><th>WK</th><th class="batting-dismissal">Dismissal</th><th class="batting-runs">Runs</th><th></th></tr></thead>
      <tbody id="bat_${idx}">`;
  batting.forEach((b, bi) => {
    html += battingRow(idx, bi, b);
  });
  html += `</tbody></table>
    <div class="add-row"><button onclick="addBatsman(${idx})">+ Add batsman</button></div>
    </div>
  </div>`;

  // Extras & Total
  html += `<div class="section">
    <div class="section-header">Extras &amp; Total</div>
    <div class="section-body">
      <div class="row">
        <div class="field"><label>Extras Total</label><input id="ext_total_${idx}" type="number" value="${extras.total ?? ''}" onchange="markDirty()"></div>
        <div class="field"><label>Extras Detail</label><input id="ext_detail_${idx}" value="${attr(extras.detail)}" onchange="markDirty()"></div>
      </div>
      <div class="row">
        <div class="field"><label>Total Runs</label><input id="tot_runs_${idx}" type="number" value="${total.runs ?? ''}" onchange="markDirty()"></div>
        <div class="field"><label>Wickets (blank=all out)</label><input id="tot_wkts_${idx}" type="number" value="${total.wickets ?? ''}" onchange="markDirty()"></div>
        <div class="field"><label>Declared</label><select id="tot_decl_${idx}" onchange="markDirty()">
          <option value="false" ${!total.declared ? 'selected' : ''}>No</option>
          <option value="true" ${total.declared ? 'selected' : ''}>Yes</option>
        </select></div>
      </div>
    </div>
  </div>`;

  // FOW
  html += `<div class="section">
    <div class="section-header">Fall of Wickets</div>
    <div class="section-body">
      <div class="field"><label>FOW (comma-separated run totals)</label>
        <input class="fow-input" id="fow_${idx}" value="${fow ? fow.join(', ') : ''}" onchange="markDirty()">
      </div>
    </div>
  </div>`;

  // Bowling
  html += `<div class="section">
    <div class="section-header">Bowling</div>
    <div class="section-body">
    <table class="bowling">
      <thead><tr><th></th><th>Name</th><th>O</th><th>M</th><th>R</th><th>W</th><th>NB</th><th>Wd</th><th></th></tr></thead>
      <tbody id="bowl_${idx}">`;
  bowling.forEach((b, bi) => {
    html += bowlingRow(idx, bi, b);
  });
  html += `</tbody></table>
    <div class="add-row"><button onclick="addBowler(${idx})">+ Add bowler</button></div>
    </div>
  </div>`;

  html += `</div>`;
  return html;
}

function battingRow(innIdx, batIdx, b) {
  return `<tr id="batrow_${innIdx}_${batIdx}">
    <td style="color:var(--muted);font-size:12px">${batIdx + 1}</td>
    <td><input class="batting-name" value="${attr(b.name)}" data-field="name" onchange="markDirty()"></td>
    <td><input type="checkbox" ${b.captain ? 'checked' : ''} data-field="captain" onchange="markDirty()"></td>
    <td><input type="checkbox" ${b.wicketkeeper ? 'checked' : ''} data-field="wicketkeeper" onchange="markDirty()"></td>
    <td><input class="batting-dismissal" value="${attr(b.dismissal)}" data-field="dismissal" onchange="markDirty()"></td>
    <td><input type="number" class="batting-runs" value="${b.runs ?? ''}" data-field="runs" onchange="markDirty()"></td>
    <td><button class="del-btn" onclick="deleteBatsman(${innIdx},${batIdx})">&times;</button></td>
  </tr>`;
}

function bowlingRow(innIdx, bowlIdx, b) {
  return `<tr id="bowlrow_${innIdx}_${bowlIdx}">
    <td style="color:var(--muted);font-size:12px">${bowlIdx + 1}</td>
    <td><input value="${attr(b.name)}" data-field="name" onchange="markDirty()"></td>
    <td><input type="text" value="${b.overs ?? ''}" data-field="overs" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="${b.maidens ?? ''}" data-field="maidens" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="${b.runs ?? ''}" data-field="runs" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="${b.wickets ?? ''}" data-field="wickets" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="${b.noballs ?? ''}" data-field="noballs" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="${b.wides ?? ''}" data-field="wides" style="width:50px" onchange="markDirty()"></td>
    <td><button class="del-btn" onclick="deleteBowler(${innIdx},${bowlIdx})">&times;</button></td>
  </tr>`;
}

function attr(v) { return v == null ? '' : String(v).replace(/"/g, '&quot;'); }

function switchInnings(idx) {
  document.querySelectorAll('.innings-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.innings-content').forEach(c => c.classList.remove('active'));
  document.getElementById(`innTab${idx}`).classList.add('active');
  document.getElementById(`innContent${idx}`).classList.add('active');
}

// ── Collect form data ──

function collectMatch() {
  const m = goldData[currentIndex];
  m.verified = document.getElementById('verifiedCheck').checked;

  m.match = {
    team1: val('f_team1'),
    team2: val('f_team2'),
    venue: val('f_venue'),
    date: val('f_date'),
    result: val('f_result'),
  };

  const bpo = val('f_bpo');
  m.balls_per_over = bpo === '' ? null : parseInt(bpo);
  m.toss = val('f_toss') || null;

  const umpStr = val('f_umpires');
  m.umpires = umpStr ? umpStr.split(',').map(s => s.trim()).filter(Boolean) : null;

  m.close_of_play = val('f_close') || null;

  const notesStr = document.getElementById('f_notes').value;
  m.notes = notesStr.trim() ? notesStr.split('\n').map(s => s.trim()).filter(Boolean) : null;

  // Innings
  (m.innings || []).forEach((inn, idx) => {
    inn.team = val(`inn_team_${idx}`) || inn.team;
    inn.innings_number = numOrNull(val(`inn_num_${idx}`)) ?? idx + 1;
    // Batting
    inn.batting = collectBatting(idx);
    // Bowling
    inn.bowling = collectBowling(idx);
    // Extras
    const extTotal = val(`ext_total_${idx}`);
    const extDetail = val(`ext_detail_${idx}`);
    inn.extras = {
      total: extTotal === '' ? 0 : parseInt(extTotal),
      detail: extDetail || null,
    };
    // Total
    const totRuns = val(`tot_runs_${idx}`);
    const totWkts = val(`tot_wkts_${idx}`);
    inn.total = {
      runs: totRuns === '' ? 0 : parseInt(totRuns),
      wickets: totWkts === '' ? null : parseInt(totWkts),
      declared: document.getElementById(`tot_decl_${idx}`).value === 'true',
    };
    // FOW
    const fowStr = val(`fow_${idx}`);
    inn.fow = fowStr ? fowStr.split(',').map(s => parseInt(s.trim())).filter(n => !isNaN(n)) : null;
  });

  return m;
}

function collectBatting(innIdx) {
  const tbody = document.getElementById(`bat_${innIdx}`);
  if (!tbody) return [];
  const rows = tbody.querySelectorAll('tr');
  return Array.from(rows).map(row => {
    const inputs = row.querySelectorAll('input');
    const runsVal = inputs[4].value;
    return {
      name: inputs[0].value,
      captain: inputs[1].checked,
      wicketkeeper: inputs[2].checked,
      dismissal: inputs[3].value,
      runs: runsVal === '' ? 0 : parseInt(runsVal),
    };
  });
}

function collectBowling(innIdx) {
  const tbody = document.getElementById(`bowl_${innIdx}`);
  if (!tbody) return [];
  const rows = tbody.querySelectorAll('tr');
  return Array.from(rows).map(row => {
    const inputs = row.querySelectorAll('input');
    return {
      name: inputs[0].value,
      overs: inputs[1].value || null,
      maidens: numOrNull(inputs[2].value),
      runs: numOrNull(inputs[3].value),
      wickets: numOrNull(inputs[4].value),
      noballs: numOrNull(inputs[5].value),
      wides: numOrNull(inputs[6].value),
    };
  });
}

function numOrNull(v) { return v === '' || v == null ? null : parseInt(v); }
function val(id) { return document.getElementById(id)?.value ?? ''; }

// ── Add/Delete rows ──

function addBatsman(innIdx) {
  markDirty();
  const tbody = document.getElementById(`bat_${innIdx}`);
  const newIdx = tbody.querySelectorAll('tr').length;
  const tr = document.createElement('tr');
  tr.id = `batrow_${innIdx}_${newIdx}`;
  tr.innerHTML = `
    <td style="color:var(--muted);font-size:12px">${newIdx + 1}</td>
    <td><input class="batting-name" value="" data-field="name" onchange="markDirty()"></td>
    <td><input type="checkbox" data-field="captain" onchange="markDirty()"></td>
    <td><input type="checkbox" data-field="wicketkeeper" onchange="markDirty()"></td>
    <td><input class="batting-dismissal" value="" data-field="dismissal" onchange="markDirty()"></td>
    <td><input type="number" class="batting-runs" value="" data-field="runs" onchange="markDirty()"></td>
    <td><button class="del-btn" onclick="this.closest('tr').remove();markDirty()">&times;</button></td>`;
  tbody.appendChild(tr);
}

function deleteBatsman(innIdx, batIdx) {
  markDirty();
  document.getElementById(`batrow_${innIdx}_${batIdx}`)?.remove();
}

function addBowler(innIdx) {
  markDirty();
  const tbody = document.getElementById(`bowl_${innIdx}`);
  const newIdx = tbody.querySelectorAll('tr').length;
  const tr = document.createElement('tr');
  tr.id = `bowlrow_${innIdx}_${newIdx}`;
  tr.innerHTML = `
    <td style="color:var(--muted);font-size:12px">${newIdx + 1}</td>
    <td><input value="" data-field="name" onchange="markDirty()"></td>
    <td><input type="text" value="" data-field="overs" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="" data-field="maidens" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="" data-field="runs" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="" data-field="wickets" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="" data-field="noballs" style="width:50px" onchange="markDirty()"></td>
    <td><input type="number" value="" data-field="wides" style="width:50px" onchange="markDirty()"></td>
    <td><button class="del-btn" onclick="this.closest('tr').remove();markDirty()">&times;</button></td>`;
  tbody.appendChild(tr);
}

function deleteBowler(innIdx, bowlIdx) {
  markDirty();
  document.getElementById(`bowlrow_${innIdx}_${bowlIdx}`)?.remove();
}

// ── Add/Delete innings ──

function addInnings() {
  if (currentIndex < 0) return;
  collectMatch();
  const m = goldData[currentIndex];
  const newIdx = m.innings.length;
  m.innings.push({
    team: '',
    innings_number: newIdx + 1,
    batting: [],
    extras: { total: 0, detail: null },
    total: { runs: 0, wickets: null, declared: false },
    fow: null,
    bowling: []
  });
  markDirty();
  renderForm(m);
  switchInnings(newIdx);
}

function deleteInnings(idx) {
  if (currentIndex < 0) return;
  if (!confirm(`Delete innings ${idx + 1}?`)) return;
  collectMatch();
  const m = goldData[currentIndex];
  m.innings.splice(idx, 1);
  markDirty();
  renderForm(m);
  if (m.innings.length > 0) switchInnings(Math.min(idx, m.innings.length - 1));
}

// ── Save / Revert ──

async function saveCurrentMatch() {
  if (currentIndex < 0) return;
  collectMatch();
  await saveGold();
  dirty = false;
  originalJson = JSON.stringify(goldData[currentIndex]);
  updateStats();
  renderList();
  toast('Saved');

  // Re-validate after save
  const issues = await loadValidation(goldData[currentIndex].page);
  renderValidation(issues);
}

function revertCurrentMatch() {
  if (currentIndex < 0) return;
  goldData[currentIndex] = JSON.parse(originalJson);
  dirty = false;
  openMatch(currentIndex);
}

// ── Init ──
loadGold();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "":
            self._html(HTML_PAGE)

        elif path == "/api/gold":
            data = self._read_gold()
            self._json_response(data)

        elif path.startswith("/api/source/"):
            page = path.split("/")[-1]
            page_file = PAGES_DIR / f"{page}.txt"
            if page_file.exists():
                self._text(page_file.read_text(encoding="utf-8"))
            else:
                self._error(404, "Not found")

        elif path.startswith("/api/validate/"):
            page = int(path.split("/")[-1])
            data = self._read_gold()
            match_data = [m for m in data if m.get("page") == page]
            if not match_data:
                self._json_response([])
                return
            from validate import validate as run_validate, ALL_CHECKS
            issues = run_validate(match_data, ALL_CHECKS)
            self._json_response(issues)

        else:
            self._error(404, "Not found")

    def do_POST(self):
        if self.path == "/api/gold":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)
            GOLD_PATH.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._json_response({"ok": True})
        else:
            self._error(404, "Not found")

    def _read_gold(self):
        if GOLD_PATH.exists():
            return json.loads(GOLD_PATH.read_text(encoding="utf-8"))
        return []

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _json_response(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _text(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _error(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # suppress request logs


def main():
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Gold Scorecard Editor running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
