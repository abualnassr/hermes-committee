/*
 * Hermes Committee — the Committee page in Hermes Desktop.
 *
 * Three pages, one per moment:
 *   1. Brief     the question and evidence; the committee type, a seat table with inline editors
 *                (role, model, instructions), the chairman's brief and the run settings.
 *   2. Convene   the table: members on an arc, the chairman's bench below. Stages, gauges, timeline
 *                lanes, a live log and the vote matrix, all drawn from the run record (snap()).
 *                Finished runs replay from the same record at REPLAY_SPEED×.
 *   3. Decision  the ruling, KPIs, why / do next / what would change it, how the table stood,
 *                the blind ranking and the detail tabs.
 * Colours follow the Hermes Desktop theme (--ui-* tokens); light or dark picks the stage hues.
 *
 * Runs never start from this page by themselves: Convene stores the set-up as a draft and sends
 * `/hermes-committee:convene draft=<id> …` in a new chat, so every run is a visible chat turn.
 * Data comes from this plugin's backend routes (dashboard/plugin_api.py) and Hermes's own model
 * list (`model.options`). The plugin never grants itself access to a provider.
 */
import {
  ErrorState,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Skeleton,
  host,
  profileColor,
  queryClient,
  useQuery
} from '@hermes/plugin-sdk'
import { createElement as h, Fragment, useEffect, useRef, useState } from 'react'

// One button system for the whole page: outline → secondary, ghost → quiet, destructive → danger.
function Button({ size, variant, className, children, ...rest }) {
  const kind = variant === 'destructive' ? 'danger' : variant === 'ghost' ? 'quiet' : 'secondary'
  return h('button', { type: 'button', ...rest, className: `hc-b hc-b-${kind}${size === 'sm' ? ' hc-b-sm' : ''}${className ? ` ${className}` : ''}` }, children)
}
const busyLabel = text => [h('span', { key: 's', className: 'hc-bspin', 'aria-hidden': true }), text]

const PLUGIN_ID = 'hermes-committee'
const ROUTE = '/hermes-committee'
const KEY = [PLUGIN_ID]
const TERMINAL = ['done', 'failed', 'cancelled']
const MAX_INSTR = 1200
const STARTERS = ['Be skeptical of the leading option', 'Cite the evidence for every claim', 'Give numbers, not adjectives',
  'Focus on cost and downtime', 'Focus on safety and compliance', 'Say what would change your mind']
const PROVIDER_COLORS = { 'openai-codex': '#10a37f', openai: '#10a37f', anthropic: '#d97757',
  deepseek: '#4d6bfe', openrouter: '#8b5cf6', gemini: '#1a73e8', 'xai-oauth': '#9aa0a6', xai: '#9aa0a6', nous: '#e11d48', alibaba: '#f59e0b',
  'kimi-coding': '#06b6d4', zai: '#22c55e', nvidia: '#76b900', 'opencode-zen': '#a855f7' }

let CTX = null
// Survives the page remounting while Convene hops to the new chat and back.
const pending = { draft: null, since: 0 }

// ── data ───────────────────────────────────────────────────────────────────

async function rest(path, options) {
  const result = await CTX.rest(path, { timeoutMs: 60_000, ...(options || {}) })
  if (result === undefined || result === null) throw new Error(`No data from ${path.split('?')[0]}. Restart the gateway if you just installed or updated the plugin.`)
  return result
}
const isLive = r => r && !TERMINAL.includes(r.status) && !r.stale
const useRuns = () => useQuery({ queryKey: [...KEY, 'runs'], queryFn: () => rest('/runs'),
  refetchInterval: q => ((q.state.data?.runs || []).some(isLive) || pending.draft ? 2000 : 20_000) })
const useRun = id => useQuery({ queryKey: [...KEY, 'run', id], queryFn: () => rest(`/runs/${encodeURIComponent(id)}`), enabled: !!id,
  refetchInterval: q => (isLive(q.state.data) ? 1500 : false) })
const useSetup = () => useQuery({ queryKey: [...KEY, 'setup'], queryFn: () => rest('/setup') })
// Hermes fills in list prices only on a refreshed model list; read it in the background (a few seconds).
const usePrices = () => useQuery({ queryKey: [...KEY, 'prices'], staleTime: 6 * 3600_000, retry: false,
  queryFn: async () => {
    const p = await host.request('model.options', { refresh: true })
    return rest('/models/remember', { method: 'POST', body: { providers: (p?.providers || []).map(x => ({ slug: x.slug, auth_type: x.auth_type, pricing: x.pricing })) } })
  } })
const useStandings = enabled => useQuery({ queryKey: [...KEY, 'standings'], queryFn: () => rest('/standings'), enabled })
const useModels = () => useQuery({ queryKey: [...KEY, 'models'], staleTime: 300_000,
  queryFn: async () => {
    const p = await host.request('model.options', {})
    const providers = (p?.providers || []).filter(x => x.authenticated !== false && !['moa', 'council'].includes(x.slug))
      .map(x => ({ slug: x.slug, name: x.name || x.slug, models: (x.models || []).map(m => (typeof m === 'string' ? m : m.id || m.name)).filter(Boolean) }))
    // Prices and sign-in kind, so chat and terminal runs can price seats too (no Hermes internals).
    rest('/models/remember', { method: 'POST', body: { providers: (p?.providers || []).map(x => ({ slug: x.slug, auth_type: x.auth_type, pricing: x.pricing })) } })
      .then(() => queryClient.invalidateQueries({ queryKey: [...KEY, 'caps'] })).catch(() => {})
    return { providers, active: { provider: p?.provider || '', model: p?.model || '' } }
  } })

function useNow(active, ms = 250) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!active) return undefined
    const id = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(id)
  }, [active, ms])
  return now
}

// ── helpers ────────────────────────────────────────────────────────────────

const shortModel = m => (m ? String(m).replace(/^.*\//, '').replace(/\[[^\]]*\]$/, '') : '')
const pcolor = p => PROVIDER_COLORS[p] || profileColor(p || 'active') || 'var(--ui-accent)'
const initials = r => String(r || '?').split(/[\s&/]+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase()
const fmtSecs = s => (s == null ? '–' : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} m ${Math.round(s % 60)} s`)
const fmtTokens = n => (n == null ? 'n/a' : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const sleep = ms => new Promise(r => setTimeout(r, ms))

// Starts the committee's chat in the background (a new session the user can open later), so the
// page stays in front. Falls back to opening a new chat when this Hermes cannot do that.
let PROFILE = ''          // the Hermes profile this plugin runs in (from /setup); the chat must be created there
let LAST_BG_ERROR = ''
async function startInBackground(message, title, preview) {
  let step = 'session.create'
  try {
    const prof = PROFILE ? { profile: PROFILE } : {}
    const s = await host.request('session.create', { title: title || 'Committee', source: 'gui', ...prof })
    const sid = s?.session_id
    if (!sid) throw new Error('no session id')
    const name = message.slice(1).split(/\s/, 1)[0]
    const arg = message.slice(name.length + 2)
    step = 'command.dispatch'
    const d = await host.request('command.dispatch', { session_id: sid, name, arg, ...prof })
    if (!d || d.type !== 'skill' || !d.message) throw new Error(`unexpected reply (${d?.type || 'none'})`)
    step = 'prompt.submit'
    let err = null
    for (let i = 0; i < 8; i++) {
      try { await host.request('prompt.submit', { session_id: sid, text: d.message, title_preview: preview || arg.slice(0, 200), ...prof }); LAST_BG_ERROR = ''; return 'background' }
      catch (e) { err = e; await sleep(400) }
    }
    throw err
  } catch (e) {
    LAST_BG_ERROR = `${step}: ${e?.message || e}`
    console.error(`hermes-committee: background start failed at ${LAST_BG_ERROR} (profile ${PROFILE || 'unknown'}); opening a chat instead`)
  }
  host.newChat()
  for (let i = 0; i < 30; i++) { await sleep(150); if (host.composer.submit('new', message)) { await sleep(500); host.navigate(ROUTE); return 'chat' } }
  let drafted = false
  for (let i = 0; i < 10 && !drafted; i++) { drafted = await host.composer.setDraft('new', message); if (!drafted) await sleep(150) }
  if (!drafted) await CTX.os.writeClipboard(message)
  return drafted ? 'typed' : 'copied'
}

// Draft → background chat. Returns { gate } when the backend refuses, else { where }.
async function startCommittee(body, title) {
  const r = await rest('/drafts', { method: 'POST', body })
  if (!r.ok) return { gate: r }
  pending.draft = r.draft; pending.since = Date.now()
  const where = await startInBackground(r.message, title, body.question)
  if (where === 'typed' || where === 'copied') pending.draft = null
  notify(where === 'background' ? 'success' : 'info', {
    background: 'Committee started. It runs in its own chat in the background; you can follow it here.',
    chat: `Committee started in a new chat (running it in the background was not possible: ${LAST_BG_ERROR || 'unknown reason'}). Following it on the Committee page…`,
    typed: 'The committee request is typed in a new chat. Press Enter to start it.',
    copied: 'Could not reach a chat. The request is copied: paste it into a chat and press Enter.' }[where])
  return { where }
}
const notify = (kind, message) => { try { host.notify({ kind, message }) } catch { /* best effort */ } }
const plannedCalls = (n, review) => n + (review ? n : 0) + 1
const debateCalls = (n, mode, rounds) => (mode && mode !== 'never' ? 1 + n * (rounds === 2 ? 2 : 1) : 0)
const DELIB = {
  material: ['When they disagree', 'Debate when the seats differ on the action, or on a fact or claim the decision rests on'],
  action: ['Only on the action', 'Debate only when the seats recommend different actions'],
  always: ['Always', 'Always run at least one round, even when they agree'],
  never: ['Never', 'No debate: the chairman decides from the answers and the vote'],
}

function ago(t) {
  if (!t) return ''
  const s = Math.max(0, Date.now() / 1000 - t)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)} min ago`
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`
  return new Date(t * 1000).toLocaleDateString()
}

function usageTotals(run) {
  const calls = run.usage || []
  const sum = k => { const v = calls.map(c => c[k]).filter(x => typeof x === 'number'); return v.length ? v.reduce((a, b) => a + b, 0) : null }
  const costs = calls.map(c => c.cost_usd).filter(x => typeof x === 'number')
  const real = calls.filter(c => !c.abandoned)
  return { calls: real.length, input: sum('input_tokens'), output: sum('output_tokens'),
           cost: costs.length && costs.length === calls.length ? costs.reduce((a, b) => a + b, 0) : null,
           est: sum('cost_est'), list: sum('cost_list') ?? 0, unknown: calls.filter(c => c.cost_known === false).length,
           abandoned: calls.length - real.length }
}

// $0.00 / $0.042 / $1.20, like the reports
function usd(v) {
  if (v == null) return '—'
  if (v <= 0) return '$0.00'
  return v >= 0.1 ? `$${v.toFixed(2)}` : v >= 0.001 ? `$${v.toFixed(3)}` : '<$0.001'
}

// Tiny Markdown → React (headings, bullets, numbered lists, bold). No HTML is ever injected.
function inline(text) {
  return String(text).split(/(\*\*[^*]+\*\*)/g).map((p, i) => (p.startsWith('**') && p.endsWith('**') ? h('b', { key: i }, p.slice(2, -2)) : p))
}
function Markdown({ text }) {
  const out = []
  let list = null
  const flush = () => { if (list) { out.push(h(list.tag, { key: out.length, className: 'hc-list' }, list.items)); list = null } }
  String(text || '').split(/\r?\n/).forEach((ln, i) => {
    const t = ln.trim()
    let m
    if (!t) { flush(); return }
    if ((m = t.match(/^(#{1,4})\s+(.*)$/))) { flush(); out.push(h('div', { key: i, className: 'hc-md-h' }, inline(m[2]))); return }
    if ((m = t.match(/^[-*•]\s+(.*)$/)) || (m = t.match(/^\d+[.)]\s+(.*)$/))) {
      const tag = /^\d/.test(t) ? 'ol' : 'ul'
      if (!list || list.tag !== tag) { flush(); list = { tag, items: [] } }
      list.items.push(h('li', { key: i }, inline(m[1])))
      return
    }
    flush()
    out.push(h('p', { key: i, className: 'hc-md-p' }, inline(t)))
  })
  flush()
  return h('div', { className: 'hc-md' }, out)
}

// ── run snapshot: one view of a run at time t (live, replay or finished) ──
// Everything drawn on the Convene page comes from here, and here only reads what the engine recorded.

const REPLAY_SPEED = 10
const STAGES = ['research', 'stage1', 'review', 'deliberate', 'chair']
const REDUCED = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
const clock = s => { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` }
const fit = (s, n) => (String(s).length > n ? `${String(s).slice(0, Math.max(1, n - 1)).trimEnd()}…` : String(s))
const clause = s => String(s || '').split(/(?<=[.;:!?])\s|\s[—–]\s/)[0].replace(/[.;:]$/, '')
const seatList = run => [...(run.seats || [])].map((s, i) => ({ ...s, _i: s.order ?? i })).sort((a, b) => a._i - b._i)
const runEnd = run => Math.max(run.summary?.elapsed || 0, ...(run.lanes || []).filter(b => !b.running).map(b => b.start + b.secs), 1)
const nameOf = (run, label) => (label === 'chair' ? 'Chairman' : (run.seats || []).find(s => s.label === label)?.name || label)

function snap(run, t, replaying) {
  const seats = seatList(run)
  const live = !replaying && isLive(run)
  const lv = run.live || {}
  const ended = !live && (!replaying || t >= runEnd(run))
  const lanes = (run.lanes || []).filter(b => b.start <= t + 0.05).map(b => {
    const running = b.running || (replaying && b.start + b.secs > t)
    return { ...b, running, shown: running ? Math.max(0, t - b.start) : b.secs }
  })
  const of = (who, stage) => lanes.filter(b => b.who === who && b.stage === stage)
  const st = (who, stage) => { const ls = of(who, stage); return ls.some(b => b.running) ? 'run' : ls.length ? 'ok' : null }
  let stage
  if (ended) stage = run.status === 'done' ? 'done' : 'stopped'
  else if (live) stage = lv.researching || lv.sources ? 'research' : lv.stage || 'stage1'
  else { stage = 'stage1'; STAGES.forEach(k => { const v = run.starts?.[k]; if (v != null && v <= t) stage = k }) }
  const describing = live && stage === 'stage1' && !!lv.describing
  const researching = stage === 'research' ? (live ? (lv.sources ? 'review' : lv.researching || 'starting') : 'searching') : ''
  const seatsOut = {}
  seats.forEach(s => {
    const L = s.label
    let a = describing ? null : st(L, 'stage1')
    if (run.answers?.[L]?.status === 'failed' && (a === 'ok' || (a === null && stage !== 'stage1'))) a = 'fail'
    if (live && stage === 'stage1' && !describing && lv.done?.[L] === false) a = 'fail'
    const runLane = of(L, 'stage1').find(b => b.running)
    const exp = run.expected_secs?.[s.model || 'active model'] || 60
    const p = a === 'run' && runLane ? Math.min(0.94, replaying ? runLane.shown / Math.max(1, (run.lanes || []).find(b => b.who === L && b.stage === 'stage1')?.secs || exp) : runLane.shown / exp) : a ? 1 : 0
    seatsOut[L] = { a, v: st(L, 'review'), d: st(L, 'deliberate'), p, run: runLane ? runLane.shown : 0,
      tokens: lanes.filter(b => b.who === L && !b.running).reduce((x, b) => x + (b.tokens || 0), 0),
      summary: a === 'ok' ? (run.answers?.[L]?.summary || lv.answers?.[L] || '') : '' }
  })
  const chairLanes = lanes.filter(b => b.who === 'chair')
  const chair = stage === 'done' ? 'ok' : stage === 'stopped' ? 'stopped' : st('chair', 'chair') === 'run' ? 'run'
    : st('chair', 'deliberate') === 'run' ? 'agenda' : 'wait'
  const ballots = lanes.filter(b => b.stage === 'review' && !b.running && b.who !== 'chair').sort((x, y) => x.start + x.secs - (y.start + y.secs)).map(b => b.who)
  if (live) Object.keys(lv.ballots || {}).forEach(l => { if (!ballots.includes(l)) ballots.push(l) })
  const okSeats = seats.filter(s => seatsOut[s.label].a === 'ok').map(s => s.label)
  let totals
  if (live || ended) totals = run.totals || {}
  else {
    const fin = lanes.filter(b => !b.running)
    totals = { calls: fin.length, tokens: fin.reduce((x, b) => x + (b.tokens || 0), 0),
      cost_usd: fin.reduce((x, b) => x + (b.cost || 0), 0), unknown_calls: fin.filter(b => b.cost == null).length }
  }
  const rankAt = run.starts?.deliberate ?? run.starts?.chair
  const ranked = (run.ranking || []).length > 0 && (live || ended || (rankAt != null && t >= rankAt))
  return { t, live, replaying, ended, stage, step: live ? lv.step : null, describing, researching, seats: seatsOut, chair, chairLanes,
    ballots: [...new Set(ballots)], okSeats, debaters: seats.filter(s => seatsOut[s.label].d === 'run').map(s => s.label),
    replies: lanes.filter(b => b.stage === 'deliberate' && b.who !== 'chair' && !b.running).length,
    lanes, totals, events: (run.events || []).filter(e => e.t <= t + 0.05), ranked,
    masked: stage === 'review', stances: stage === 'done' ? (run.stances || {}) : {},
    rows: replaying && !ended ? (run.matrix?.rows || []).filter(r => ballots.includes(r)) : (run.matrix?.rows || []) }
}

// ── the table: members on an arc above, the chairman's bench below ──
const AC = [0, 150], RA = 330, BENCH = { y: 250, w: 220, h: 60 }
function arcLayout(n) {
  const step = n > 1 ? Math.min(50, 150 / (n - 1)) : 0
  const r = n <= 5 ? 33 : n === 6 ? 30 : n === 7 ? 27 : 25
  const ang = Array.from({ length: n }, (_, i) => ((270 - (step * (n - 1)) / 2 + i * step) * Math.PI) / 180)
  return { ang, r, pos: ang.map(t => [Math.cos(t) * RA + AC[0], Math.sin(t) * RA + AC[1]]),
    chord: n > 1 ? 2 * RA * Math.sin(((step * Math.PI) / 180) / 2) : 400 }
}
const arcAt = (t, off = 0) => [Math.cos(t) * (RA + off) + AC[0], Math.sin(t) * (RA + off) + AC[1]]
const arcPts = (a, b, off = 0, n = 40) => Array.from({ length: n + 1 }, (_, k) => arcAt(a + ((b - a) * k) / n, off))
const linePts = (a, b, n = 30) => Array.from({ length: n + 1 }, (_, k) => [a[0] + ((b[0] - a[0]) * k) / n, a[1] + ((b[1] - a[1]) * k) / n])
function spokeEnds([x, y], r) {
  const ex = Math.max(-80, Math.min(80, x * 0.24)), ey = BENCH.y - BENCH.h / 2, d = Math.hypot(ex - x, ey - y)
  return [[x + ((ex - x) / d) * (r + 3), y + ((ey - y) / d) * (r + 3)], [ex, ey]]
}
// A dot that travels along a path, then removes itself. Nothing happens with reduced motion.
function fly(g, cls, pts, dur, r = 4.5, done) {
  if (!g || !g.isConnected || REDUCED) { if (done) done(); return }
  const diamond = cls.includes('hc-fx-ballot')
  const c = document.createElementNS('http://www.w3.org/2000/svg', diamond ? 'rect' : 'circle')
  c.setAttribute('class', cls); c.setAttribute('opacity', 0)
  if (diamond) { c.setAttribute('width', r * 2); c.setAttribute('height', r * 2) } else c.setAttribute('r', r)
  g.appendChild(c)
  const t0 = performance.now()
  const f = now => {
    if (!c.isConnected) return
    const p = Math.min((now - t0) / dur, 1), e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2
    const k = e * (pts.length - 1), i = Math.floor(k), fr = k - i, [a, b] = pts[i], [x, y] = pts[Math.min(i + 1, pts.length - 1)]
    const cx = a + (x - a) * fr, cy = b + (y - b) * fr
    if (diamond) { c.setAttribute('x', cx - r); c.setAttribute('y', cy - r); c.setAttribute('transform', `rotate(45 ${cx} ${cy})`) }
    else { c.setAttribute('cx', cx); c.setAttribute('cy', cy) }
    c.setAttribute('opacity', p < 0.12 ? p / 0.12 : p > 0.88 ? (1 - p) / 0.12 : 1)
    if (p < 1) requestAnimationFrame(f); else { c.remove(); if (done) done() }
  }
  requestAnimationFrame(f)
}

// ── the evidence box over the table (HTML, so its text stays readable at any size). Every model call
// that takes evidence (answers, blind vote, debate, the chairman's agenda and verdict) draws it from the
// box's outlet, the top centre of the drawing. Research calls get the question only, so they draw nothing. ──
const TAKES_EVIDENCE = { stage1: 1, review: 1, deliberate: 1, chair: 1 }
const TOP = -336
function evidencePills(run) {
  const ix = run.evidence_index || {}, secs = ix.sections || [], imgs = ix.images || []
  const files = (run.files || []).map(f => {
    const own = secs.filter(x => x.source === f.name).map(x => x.id), img = imgs.find(i => i.source === f.name)
    const id = img ? img.id : own.length > 1 ? `${own[0]}–${own[own.length - 1].replace(/^\D+/, '')}` : own[0] || (f.kind === 'image' ? 'I' : 'E')
    return { id, label: f.name, title: f.name, kind: img || f.kind === 'image' ? 'img' : 'file' }
  })
  if (String(run.evidence || '').trim()) files.push({ id: '✎', label: 'typed notes', title: fit(String(run.evidence).trim(), 300), kind: 'file' })
  const web = (run.web?.items || []).filter(w => w.keep !== false).map(w => ({ id: w.id, kind: 'web',
    label: String(w.site || '').replace(/^www\./, '').replace(/\.[a-z.]{2,}$/i, '') || 'web',
    title: `${w.title || ''} · ${w.site || ''}\n${fit(w.excerpt || '', 220)}\n${w.url || ''}`.trim() }))
  return { files, web }
}
const webVisible = (run, sn) => (run.web?.items || []).length > 0 && sn.stage !== 'research'
  && (sn.live || sn.ended || run.starts?.stage1 == null || sn.t >= run.starts.stage1)
function EvidenceBox({ run, sn, boxRef }) {
  const ev = evidencePills(run)
  const webShown = webVisible(run, sn)
  const files = ev.files.slice(0, 10), more = ev.files.slice(10)
  const nFiles = (run.files || []).length
  const count = [nFiles ? `${nFiles} file${nFiles > 1 ? 's' : ''}` : '', String(run.evidence || '').trim() ? 'typed notes' : '',
    webShown ? `${ev.web.length} web source${ev.web.length > 1 ? 's' : ''}` : ''].filter(Boolean).join(' · ')
  const webNote = sn.stage === 'research' ? (sn.researching === 'review' ? '🌐 Research first · waiting for your source review…' : '🌐 Research first · the chairman’s model is searching the web…')
    : webShown ? '' : run.research ? (run.web && sn.stage !== 'stage1' || sn.ended ? '🌐 Research first found no sources to use' : '🌐 Research first · sources arrive before the answers') : ''
  const chip = (q, i) => h('span', { key: `${q.id}${i}`, className: `hc-ep hc-ep-${q.kind}`, title: q.title, style: { animationDelay: `${i * 0.12}s` } },
    h('b', null, q.id), h('span', null, q.label))
  return h('div', { className: `hc-evbox2${sn.stage === 'research' ? ' hc-evsearch' : ''}`, ref: boxRef },
    h('div', { className: 'hc-evhead' }, h('b', null, 'Evidence'), h('span', null, 'every model reads all of it, straight from here'),
      h('span', { className: 'hc-evcount' }, count)),
    h('div', { className: 'hc-evrow' }, files.length ? files.map(chip) : h('span', { className: 'hc-evnote' }, 'No files or typed evidence · each call gets the question and background'),
      more.length ? h('span', { className: 'hc-ep hc-ep-more', title: more.map(q => `${q.id}  ${q.title}`).join('\n') }, `+${more.length} more`) : null),
    webShown || webNote ? h('div', { className: 'hc-evrow' }, webShown ? ev.web.map(chip) : h('span', { className: 'hc-evnote' }, webNote)) : null,
    h('i', { className: 'hc-evout' }))
}
const quadPts = (a, c, b, n = 36) => Array.from({ length: n + 1 }, (_, k) => {
  const t = k / n, u = 1 - t
  return [u * u * a[0] + 2 * u * t * c[0] + t * t * b[0], u * u * a[1] + 2 * u * t * c[1] + t * t * b[1]]
})
const qd = (a, c, b) => `M${a[0].toFixed(1)} ${a[1].toFixed(1)} Q${c[0].toFixed(1)} ${c[1].toFixed(1)} ${b[0].toFixed(1)} ${b[1].toFixed(1)}`
function lines2(text, n) {
  const out = ['']
  for (const w of String(text || '').split(/\s+/)) {
    if (!w) continue
    if ((out[out.length - 1] + ' ' + w).trim().length > n) { if (out.length === 2) { out[1] = fit(out[1] + ' ' + w, n); break } out.push(w) }
    else out[out.length - 1] = (out[out.length - 1] + ' ' + w).trim()
  }
  return out.filter(Boolean)
}
let tableIds = 0
const BW = 300, BH = 62

function ArcStage({ run, sn }) {
  const seats = seatList(run)
  const { ang, pos, r, chord } = arcLayout(seats.length)
  const R = r + 7
  const fx = useRef(null), fxUnder = useRef(null), boxRef = useRef(null), outRef = useRef(null), prev = useRef(null)
  const uid = useRef(null); if (uid.current == null) uid.current = ++tableIds
  const id = k => `hc${uid.current}${k}`
  const [ripple, setRipple] = useState(0)
  const [bubbles, setBubbles] = useState({})
  const idx = Object.fromEntries(seats.map((s, i) => [s.label, i]))
  const chars = Math.max(7, Math.floor((Math.min(chord, 260) - 10) / 9.2))
  const benchTop = BENCH.y - BH / 2
  // geometry: feeds from the outlet to above each seat's name; curved spokes from each seat to the bench
  const feeds = pos.map(([x, y]) => { const a = [0, TOP], c = [x, TOP + 6], b = [x, y - R - 58]; return { d: qd(a, c, b), pts: quadPts(a, c, b) } })
  const chairFeed = { d: `M0 ${TOP} L0 ${benchTop - 14}`, pts: linePts([0, TOP], [0, benchTop - 14], 40) }
  const spokes = pos.map(([x, y], i) => {
    const e = [Math.max(-BW / 2 + 40, Math.min(BW / 2 - 40, (i - (seats.length - 1) / 2) * 36)), benchTop + 2]
    const c = [(x + e[0]) / 2, Math.max(y, e[1]) - 46]
    const d0 = Math.hypot(c[0] - x, c[1] - y), a = [x + ((c[0] - x) / d0) * (R + 3), y + ((c[1] - y) / d0) * (R + 3)]
    return { d: qd(a, c, e), pts: quadPts(a, c, e), back: quadPts(e, c, a) }
  })
  const ch = sn.chair
  const chairLane = sn.chairLanes.find(b => b.running)
  const chairTok = sn.chairLanes.filter(b => !b.running).reduce((x, b) => x + (b.tokens || 0), 0)
  const expChair = Math.max(45, run.expected_secs?.[run.chairman?.model || 'active model'] || 60)
  const fill = sn.stage === 'done' ? 1 : ch === 'run' && chairLane ? Math.min(0.94, chairLane.shown / expChair) : 0
  const webShown = webVisible(run, sn)
  const nWeb = (run.web?.items || []).filter(w => w.keep !== false).length

  const glowBox = wait => setTimeout(() => {
    if (REDUCED) return
    for (const el of [boxRef.current, outRef.current]) if (el) { el.classList.add('hc-sending'); clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('hc-sending'), 1400) }
  }, wait)
  const send = (to, k = 0, wait = 0) => {
    const f = to === 'chair' ? chairFeed : feeds[idx[to]]
    if (!f) return
    for (let j = 0; j < 4; j++) setTimeout(() => fly(fxUnder.current, j % 2 ? 'hc-fx-ev hc-fx-ev2' : 'hc-fx-ev', f.pts, 1150, 4.5 - j * 0.6), wait + k * 60 + j * 80)
    glowBox(wait)
  }
  const laneKey = b => `${b.who}|${b.stage}|${b.start}`
  useEffect(() => {
    const p = prev.current
    const answered = seats.filter(s => sn.seats[s.label]?.a === 'ok').map(s => s.label)
    if (p) {
      let k = 0
      const wait = webShown && !p.webShown ? Math.min(2200, 900 + nWeb * 140) : 0   // let the sources land first
      sn.lanes.forEach(b => {
        if (p.lanes.has(laneKey(b)) || !TAKES_EVIDENCE[b.stage]) return
        if (b.who === 'chair') { if (b.stage === 'chair' || b.stage === 'deliberate') send('chair', k++, wait) } else if (b.who in idx) send(b.who, k++, wait)
      })
      // an answer lands: a burst runs down its spoke and its first line shows beside the seat
      answered.filter(l => !p.answered.includes(l)).forEach(l => {
        const sp = spokes[idx[l]]
        for (let j = 0; j < 5; j++) setTimeout(() => fly(fx.current, 'hc-fx-ans', sp.pts, 1000, 4.5 - j * 0.6), j * 70)
        const text = sn.seats[l]?.summary
        if (text && !sn.masked) {
          setBubbles(b => ({ ...b, [l]: text }))
          setTimeout(() => setBubbles(b => { const n = { ...b }; delete n[l]; return n }), 3800)
        }
      })
      sn.ballots.filter(l => !p.ballots.includes(l) && l in idx).forEach(l => {
        const i = idx[l]
        seats.forEach((s, j) => { if (j !== i && sn.okSeats.includes(s.label)) fly(fx.current, 'hc-fx-ballot', arcPts(ang[i], ang[j], -(62 + Math.random() * 26)), 850 + Math.abs(i - j) * 120 + Math.random() * 250, 4.5) })
      })
      if (sn.chair === 'run' && p.chair !== 'run') seats.forEach((s, n) => { if (sn.okSeats.includes(s.label)) setTimeout(() => fly(fx.current, 'hc-fx-out', spokes[n].pts, 1000, 4.5), 300 + n * 200) })
      if (webShown && !p.webShown) for (let n = 0; n < nWeb; n++) setTimeout(() => fly(fx.current, 'hc-fx-web', quadPts([0, benchTop], [(n - nWeb / 2) * 60, 0], [0, TOP]), 900, 5), n * 140)
      if (sn.stage === 'done' && p.stage !== 'done') {
        setRipple(x => x + 1)
        seats.forEach((s, n) => { const st = sn.stances[s.label]; if (st) setTimeout(() => fly(fx.current, st === 'agree' ? 'hc-fx-ok' : 'hc-fx-warn', spokes[n].back, 1100, 6), n * 120) })
      }
    } else if (sn.stage === 'stage1' && sn.t < 6) sn.lanes.filter(b => b.stage === 'stage1' && b.running && b.who in idx).forEach((b, n) => send(b.who, n))
    prev.current = { stage: sn.stage, ballots: sn.ballots, chair: sn.chair, webShown, answered, lanes: new Set(sn.lanes.map(laneKey)) }
  })
  const deb = sn.debaters.join(',')
  useEffect(() => {
    const list = sn.debaters.map(l => idx[l]).filter(i => i != null)
    if (list.length < 2 || REDUCED) return undefined
    let alive = true
    const spark = (a, b) => {
      if (!alive) return
      fly(fx.current, 'hc-fx-spark', arcPts(ang[a], ang[b], -(48 + Math.random() * 30)), 700 + Math.abs(a - b) * 130, 4.5,
        () => setTimeout(() => { const o = list.filter(x => x !== b); spark(b, o[Math.floor(Math.random() * o.length)]) }, 120 + Math.random() * 250))
    }
    list.forEach((a, k) => setTimeout(() => spark(a, list[(k + 1) % list.length]), k * 260))
    return () => { alive = false }
  }, [deb, seats.length])

  const debList = sn.debaters.map(l => idx[l]).filter(i => i != null)
  const verdictWait = ch === 'run'
  const debating = sn.stage === 'deliberate' && (sn.debaters.length > 0 || sn.replies > 0)
  const chairStatus = sn.stage === 'research' ? (sn.researching === 'review' ? 'waiting for your source review' : 'researching the question…')
    : ch === 'agenda' ? `listing the disputed points · ${clock(chairLane?.shown)}` : ch === 'run' ? `writing the verdict · ${clock(chairLane?.shown)}`
      : ch === 'ok' ? `decided${chairTok ? ` · ${fmtNum(chairTok)} tok` : ''}` : chairTok ? `${fmtNum(chairTok)} tok` : 'listening'
  const decision = sn.stage === 'done' ? (run.chair?.decision || '') : ''
  const ARC = 2 * Math.PI * R
  return h('div', { className: 'hc-stagewrap2' },
    h(EvidenceBox, { run, sn, boxRef }),
    h('div', { className: 'hc-stagewrap' }, h('svg', { className: 'hc-room hc-table', viewBox: '-490 -340 980 712', role: 'img',
        'aria-label': `The committee table: ${seats.length} seats and the chairman; the evidence box above sends the same evidence to every model` },
      h('defs', null,
        h('radialGradient', { id: id('floor'), cx: '50%', cy: '100%', r: '75%' },
          h('stop', { offset: '0', stopColor: 'var(--hc-acc)', stopOpacity: 0.16 }), h('stop', { offset: '0.6', stopColor: 'var(--hc-acc)', stopOpacity: 0.03 }),
          h('stop', { offset: '1', stopColor: 'var(--hc-acc)', stopOpacity: 0 })),
        h('filter', { id: id('glow'), x: '-80%', y: '-80%', width: '260%', height: '260%' }, h('feGaussianBlur', { stdDeviation: 7 })),
        h('clipPath', { id: id('bench') }, h('rect', { x: -BW / 2, y: -BH / 2, width: BW, height: BH, rx: BH / 2 }))),
      h('ellipse', { cx: 0, cy: BENCH.y + 70, rx: 580, ry: 340, fill: `url(#${id('floor')})` }),
      [0, 1, 2].map(i => h('ellipse', { key: `g${i}`, className: 'hc-guide', cx: 0, cy: BENCH.y + 60, rx: 260 + i * 100, ry: 150 + i * 72, style: { opacity: 0.5 - i * 0.13 } })),
      h('g', { className: 'hc-feeds', ref: outRef },
        feeds.map((f, i) => h('path', { key: `fd${i}`, className: 'hc-feed', d: f.d })),
        h('path', { className: 'hc-feed hc-feed-chair', d: chairFeed.d }),
        h('circle', { className: 'hc-outlet', cx: 0, cy: TOP, r: 5 })),
      h('g', { ref: fxUnder }),
      seats.map((s, i) => {
        const S = sn.seats[s.label] || {}
        const active = S.a === 'run' || S.v === 'run' || S.d === 'run'
        return h('g', { key: `sp${s.label}` },
          h('path', { className: 'hc-tspoke', d: spokes[i].d }),
          active ? h('path', { className: `hc-tenergy ${S.v === 'run' ? 'hc-e-vote' : S.d === 'run' ? 'hc-e-deb' : ''}`, d: spokes[i].d }) : null)
      }),
      debList.length >= 2 ? debList.slice(1).map((b, k) => {
        const a = debList[k], [x1, y1] = pos[a], [x2, y2] = pos[b]
        return h('path', { key: `db${k}`, className: 'hc-debarc', d: qd([x1, y1 + R], [(x1 + x2) / 2, Math.max(y1, y2) + 120], [x2, y2 + R]) })
      }) : null,
      ripple && sn.stage === 'done' ? [0, 1, 2].map(k => h('ellipse', { key: `rp${ripple}-${k}`, className: 'hc-tripple', cx: 0, cy: BENCH.y, rx: BW / 2, ry: BH / 2, style: { animationDelay: `${k * 350}ms` } })) : null,
      h('g', { className: `hc-bench${ch === 'run' || ch === 'agenda' ? ' hc-bwrite' : ''}${ch === 'ok' ? ' hc-bdone' : ''}${sn.stage === 'research' ? ' hc-bresearch' : ''}`, transform: `translate(0 ${BENCH.y})` },
        h('rect', { className: 'hc-bhalo', x: -BW / 2 - 10, y: -BH / 2 - 6, width: BW + 20, height: BH + 12, rx: BH / 2 + 6, filter: `url(#${id('glow')})` }),
        h('rect', { className: 'hc-bdisc', x: -BW / 2, y: -BH / 2, width: BW, height: BH, rx: BH / 2 }),
        h('g', { clipPath: `url(#${id('bench')})` },
          h('rect', { className: 'hc-bfill', x: -BW / 2, y: -BH / 2, width: BW * fill, height: BH }),
          sn.stage === 'research' ? h('rect', { className: 'hc-bscan', x: -BW / 2, y: -BH / 2, width: 34, height: BH }) : null),
        h('text', { className: 'hc-btitle', y: -4 }, '⚖  CHAIRMAN'),
        h('text', { className: 'hc-bsub', y: 17 }, fit(shortModel(run.chairman?.model) || 'active model', 34)),
        h('text', { className: 'hc-bstat', y: BH / 2 + 24 }, chairStatus),
        decision ? lines2(`“${decision}”`, 78).map((t, j) => h('text', { key: `v${j}`, className: 'hc-bverdict', y: BH / 2 + 52 + j * 22 }, t)) : null),
      seats.map((s, i) => {
        const S = sn.seats[s.label] || {}
        const stance = sn.stances[s.label]
        const masked = sn.masked
        const waiting = verdictWait && !stance && S.a === 'ok'
        const idle = debating && !S.d && S.a === 'ok'
        const active = S.a === 'run' || S.v === 'run' || S.d === 'run'
        const cls = ['hc-node hc-tseat', S.a === 'run' && 'hc-thinking', S.v === 'run' && 'hc-voting', S.d === 'run' && 'hc-debating', masked && 'hc-masked',
          S.a === 'fail' && 'hc-failed', stance === 'agree' && 'hc-agree', stance === 'dissent' && 'hc-dissent', S.a === 'ok' && !stance && !active && 'hc-answered',
          waiting && 'hc-waiting', idle && 'hc-idle', active && 'hc-active'].filter(Boolean).join(' ')
        const status = masked ? (S.v === 'run' ? 'ranking the others…' : S.v === 'ok' ? 'ballot cast' : S.a === 'fail' ? 'no answer' : '')
          : S.d === 'run' ? 'replying on the disputed points…' : S.a === 'run' ? `answering · ${clock(S.run)}` : S.a === 'fail' ? 'no answer'
            : stance === 'agree' ? 'in line with the decision' : stance === 'dissent' ? 'dissent kept'
              : waiting ? 'waiting for the verdict' : idle ? 'not in the debate' : S.a === 'ok' ? 'answered' : sn.stage === 'research' ? 'waiting for the sources' : 'waiting'
        const prog = S.a === 'run' ? S.p : S.a ? 1 : 0
        const [x, y] = pos[i]
        return h('g', { key: s.label, className: cls, transform: `translate(${x.toFixed(1)} ${y.toFixed(1)})` },
          h('title', null, masked ? `Response ${s.label}` : `${s.name}${S.summary ? `\n${S.summary}` : ''}`),
          h('g', { className: 'hc-podg' },
            h('circle', { className: 'hc-thalo', r: R + 6, filter: `url(#${id('glow')})` }),
            h('circle', { className: 'hc-ttrack', r: R }),
            h('circle', { className: 'hc-tarc', r: R, strokeDasharray: ARC, strokeDashoffset: ARC * (1 - prog) }),
            active ? h('circle', { className: 'hc-tcomet', r: R, strokeDasharray: `${(ARC * 0.14).toFixed(1)} ${ARC.toFixed(1)}` }) : null,
            h('circle', { className: 'hc-tdisc', r }),
            h('text', { className: 'hc-ini' }, initials(s.name)),
            h('text', { className: 'hc-let' }, s.label)),
          h('text', { className: 'hc-lbl', textAnchor: 'middle', y: -(R + 34) }, masked ? `Response ${s.label}` : fit(s.name, chars)),
          h('text', { className: 'hc-sub', textAnchor: 'middle', y: -(R + 14) }, fit(shortModel(s.model) || 'active model', chars + 2)),
          h('text', { className: 'hc-stance', textAnchor: 'middle', y: R + 27 }, fit(status, Math.max(chars + 6, 30))),
          h('text', { className: 'hc-tk', textAnchor: 'middle', y: R + 46 }, S.tokens ? `${fmtNum(S.tokens)} tok` : ''))
      }),
      h('g', { ref: fx }),
      Object.entries(bubbles).map(([l, text]) => {
        const i = idx[l]
        if (i == null) return null
        const [x, y] = pos[i], W = 270, L = lines2(text, 38)
        // centre seat: below · outer seats: below, reaching inward · inner seats: beside, on the outer side
        const H = 14 + L.length * 18, below = Math.abs(x) < 40 || Math.abs(x) >= 250
        const bx = Math.abs(x) < 40 ? x - W / 2 : Math.abs(x) >= 250 ? (x < 0 ? x - 60 : x + 60 - W) : (x < 0 ? x - R - 18 - W : x + R + 18)
        const by = below ? y + R + 58 : y - H / 2
        return h('g', { key: `bb${l}`, className: 'hc-bubble' },
          h('rect', { x: bx, y: by, width: W, height: H, rx: 10 }),
          L.map((t, j) => h('text', { key: j, x: bx + 12, y: by + 22 + j * 18 }, t)))
      }))))
}

// Small arc for the brief (click a seat to edit it) and the decision (how each seat stood).
function MiniArc({ seats, selected, onSelect, height = 150 }) {
  const n = seats.length, span = Math.min(150, 30 * (n - 1) + 30), a0 = 270 - span / 2
  const pts = seats.map((s, i) => { const a = ((n === 1 ? 270 : a0 + (i * span) / (n - 1)) * Math.PI) / 180; return [Math.cos(a) * 92, 40 + Math.sin(a) * 92] })
  return h('svg', { className: 'hc-mini', viewBox: '-110 -78 220 156', style: { height } },
    pts.map(([x, y], i) => h('line', { key: `l${i}`, className: 'hc-mini-sp', x1: x, y1: y + 15, x2: Math.max(-26, Math.min(26, x * 0.25)), y2: 50 })),
    h('g', { className: `hc-mseat hc-mchair${selected === 'chair' ? ' hc-sel' : ''}`, transform: 'translate(0 60)', onClick: onSelect ? () => onSelect('chair') : undefined },
      h('title', null, 'Chairman'),
      h('rect', { x: -36, y: -11, width: 72, height: 22, rx: 11 }), h('text', null, '⚖ Chair')),
    seats.map((s, i) => h('g', { key: s.key, transform: `translate(${pts[i][0].toFixed(1)} ${pts[i][1].toFixed(1)})` },
      h('g', { className: `hc-mseat${s.stance ? ` hc-${s.stance}` : ''}${selected === s.key ? ' hc-sel' : ''}`, style: { animationDelay: `${(i + 1) * 45}ms` },
        onClick: onSelect ? () => onSelect(s.key) : undefined },
        h('title', null, s.title || s.name), h('circle', { r: 14 }), h('text', null, initials(s.name)),
        s.drift ? h('g', { className: 'hc-drift', transform: 'translate(11 -11)' }, h('circle', { r: 5.5 }), h('text', null, '!')) : null))))
}

// The chairman's note on whether each seat with instructions kept to its role.
const roleFit = (run, label) => ((run.chair || {}).role_fit || []).find(x => x.label === label)

// Follows the Desktop theme: light or dark picks the stage hues; text on the accent stays readable.
function useScheme(ref) {
  const [sch, setSch] = useState({ scheme: 'dark', onAcc: '#ffffff' })
  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    const mk = v => { const i = document.createElement('i'); i.style.cssText = `position:absolute;width:0;height:0;pointer-events:none;background:${v}`; el.appendChild(i); return i }
    const pb = mk('var(--hc-bg)'), pa = mk('var(--hc-acc)')
    const lum = node => {
      const c = getComputedStyle(node).backgroundColor || ''
      let n = (c.replace(/^color\(\s*srgb/, '').match(/[\d.]+/g) || []).map(Number)
      if (n.length < 3) return null
      if (/^color\(/.test(c)) n = n.map(v => v * 255)
      const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
      return 0.2126 * lin(n[0]) + 0.7152 * lin(n[1]) + 0.0722 * lin(n[2])
    }
    const read = () => {
      const b = lum(pb), a = lum(pa)
      setSch(s => {
        const next = { scheme: b == null ? s.scheme : b > 0.35 ? 'light' : 'dark', onAcc: a == null ? s.onAcc : a > 0.4 ? '#17130a' : '#ffffff' }
        return next.scheme === s.scheme && next.onAcc === s.onAcc ? s : next
      })
    }
    read()
    const mo = new MutationObserver(read)
    mo.observe(document.documentElement, { attributes: true })
    if (document.body) mo.observe(document.body, { attributes: true })
    const mq = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: dark)') : null
    mq?.addEventListener?.('change', read)
    const id = setInterval(read, 4000)                   // themes that change variables without touching attributes
    return () => { mo.disconnect(); mq?.removeEventListener?.('change', read); clearInterval(id); pb.remove(); pa.remove() }
  }, [])
  return sch
}

// ── model picker ───────────────────────────────────────────────────────────

function ModelPicker({ slot, onPick, models, locked }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [all, setAll] = useState({})
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const off = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', off)
    return () => document.removeEventListener('mousedown', off)
  }, [open])
  const provs = models?.providers || []
  const name = p => provs.find(x => x.slug === p)?.name || p
  const f = q.trim().toLowerCase()
  const pick = (provider, model) => { onPick({ provider, model }); setOpen(false); setQ('') }
  const groups = provs.map(p => {
    const ms = p.models.filter(m => !f || m.toLowerCase().includes(f) || p.name.toLowerCase().includes(f) || p.slug.includes(f))
    return { p, ms, shown: f || all[p.slug] ? ms : ms.slice(0, 6) }
  }).filter(g => g.ms.length)
  const exact = f && !provs.some(p => p.models.includes(q.trim()))
  return h('div', { className: 'hc-picker', ref },
    h('button', { className: 'hc-pbtn', onClick: () => setOpen(!open) },
      h('span', { className: 'hc-dot hc-dot-lg', style: { background: pcolor(slot.provider) } }),
      h('span', { className: 'hc-grow', style: { minWidth: 0 } },
        h('div', { className: 'hc-ellipsis', style: { fontWeight: 600 } }, shortModel(slot.model) || 'Your active model'),
        h('div', { className: 'hc-xs hc-muted hc-ellipsis' }, slot.provider ? name(slot.provider) : `whatever Hermes is using${models?.active?.model ? ` (${shortModel(models.active.model)})` : ''}`)),
      h('span', { className: 'hc-muted' }, open ? '▴' : '▾')),
    open ? h('div', { className: 'hc-pop' },
      h('input', { className: 'hc-in', autoFocus: true, value: q, onChange: e => setQ(e.target.value), placeholder: `Search ${provs.length} connected providers…` }),
      !f ? h('div', { className: `hc-pi${!slot.provider && !slot.model ? ' hc-cur' : ''}`, onClick: () => pick('', '') }, '⟳ Your active Hermes model') : null,
      !models ? h('div', { className: 'hc-pi hc-muted' }, 'Loading your models…') : null,
      groups.map(({ p, ms, shown }) => h(Fragment, { key: p.slug },
        h('div', { className: 'hc-pg' }, h('span', { className: 'hc-dot', style: { background: pcolor(p.slug) } }), p.name,
          locked(p.slug) ? h('span', { className: 'hc-lock', title: 'Hermes has not allowed the committee to use this provider yet' }, '🔒 needs your OK') : null),
        shown.map(m => h('div', { key: m, className: `hc-pi${slot.provider === p.slug && slot.model === m ? ' hc-cur' : ''}`, onClick: () => pick(p.slug, m) }, m)),
        shown.length < ms.length ? h('div', { className: 'hc-pi hc-more', onClick: () => setAll({ ...all, [p.slug]: true }) }, `Show all ${ms.length}…`) : null)),
      exact && slot.provider ? h('div', { className: 'hc-pi', onClick: () => pick(slot.provider, q.trim()) }, `Use “${q.trim()}” on ${name(slot.provider)}`) : null,
      models && !groups.length && !exact ? h('div', { className: 'hc-pi hc-muted' }, 'No model matches.') : null) : null)
}

// ── guide & help ───────────────────────────────────────────────────────────

function Guide({ onClose }) {
  const step = (n, title, text) => h('div', { className: 'hc-gstep' }, h('div', { className: 'hc-gnum' }, n), h('div', null, h('b', null, title), h('div', { className: 'hc-sm hc-muted' }, text)))
  return h('div', { className: 'hc-card hc-guide' },
    h('div', { className: 'hc-row' }, h('b', { style: { fontSize: 14 } }, 'Get a decision from several AIs in 3 steps'), h('span', { className: 'hc-grow' }),
      h('button', { className: 'hc-link', onClick: onClose }, 'Got it, hide this')),
    h('div', { className: 'hc-gsteps' },
      step(1, 'Ask', 'Type the decision you need below. Put numbers, logs or notes under “Add evidence” so every AI judges the same facts.'),
      step(2, 'Pick your committee', 'Choose a committee type, then click any seat row to change its role, AI model and instructions. “＋ Add a seat” adds one.'),
      step(3, 'Convene', 'Hermes opens a chat and runs it there. This page follows it live: answers, a blind vote, then the chairman’s verdict. Usually 2–8 minutes.')),
    h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, 'You get the verdict, who agreed and who didn’t, every answer ranked by the other AIs, and a PDF report. You can press Stop at any time.'))
}

function Help({ onClose, setup }) {
  const sec = (t, ...body) => h('div', { className: 'hc-help-sec' }, h('div', { className: 'hc-lbl' }, t), ...body)
  const p = t => h('p', { className: 'hc-sm' }, t)
  return h('div', { className: 'hc-overlay', onMouseDown: e => { if (e.target === e.currentTarget) onClose() } },
    h('div', { className: 'hc-card hc-help' },
      h('div', { className: 'hc-row' }, h('b', { style: { fontSize: 15 } }, 'How to use the Committee'), h('span', { className: 'hc-grow' }), h('button', { className: 'hc-link', onClick: onClose }, '✕ Close')),
      sec('What it does',
        p('A committee is several AI models answering the same question. It runs in stages:'),
        h('ol', { className: 'hc-list hc-sm' },
          h('li', null, h('b', null, 'Answers. '), 'Every seat answers on its own, without seeing the others.'),
          h('li', null, h('b', null, 'Blind vote. '), 'Each seat ranks the others’ answers, labelled only “Response A, B, C…”, so nobody can favour a brand.'),
          h('li', null, h('b', null, 'Deliberation (when they disagree). '), 'The chairman lists the points the seats disagree on; each seat holds or changes its position with a reason.'),
          h('li', null, h('b', null, 'Verdict. '), 'The chairman reads everything, makes the final call, keeps the dissent and lists next actions.'))),
      sec('Setting up',
        p('Type your question, then pick a committee type. Classic is the original council: the same task for everyone. The other types give each seat a role.'),
        p('Click a seat row (or a seat on the small table) to change its role, its AI model and its instructions. Instructions tell that seat how to think, for example: “You are a mechanical engineer. Judge failure modes from the evidence and give numbers.” The + chips add common ones.'),
        p('Different models give more independent views. If every seat uses the same model, the answers will be similar.'),
        p('💾 Save committee keeps a committee for next time. Your saved committees appear next to the committee types.')),
      sec('📎 Evidence: files, folders and photos',
        p('Use the Evidence box under the question. “Add files” picks one or more files; “Choose folder” reads every supported file in a folder (and two levels of sub-folders); you can also drop files on the box, or paste text. Every seat reads the same files, and the seats cite them as [E1], [E2]… and [I1], [I2]….'),
        p('Formats: text, Markdown, CSV and JSON; PDF; Word (.docx); Excel (.xlsx, each sheet as a table, up to 2,000 rows); photos (JPG, PNG, WebP, HEIC). Up to 25 MB per file and 60 files. Files are read once, when you add them; changing them afterwards does not change the run.'),
        p('Every seat is sent the photos together with a written description of each (a vision model in the committee writes it first, one extra call per picture), so a model that ignores pictures still has the facts. If a provider refuses the picture, that seat is asked again with the description only, and the plugin remembers that model as text-only (📝 on the seat card). To decide yourself, open the seat and choose “Can see photos” or “Descriptions only”.'),
        p('When to use it: add the files the decision actually rests on, such as the inspection report, the trend export, photos of the damage. Leave the rest out. Every file goes to every seat in almost every call, so the token count on the box is multiplied by about 2 × seats + 1. Untick a file to leave it out without removing it.'),
        p('Photos cost about 1,600 tokens per call for each seat that sees them. Add photos when the condition is easier to see than to describe; for numbers, a table or text export is cheaper and more exact than a screenshot.')),
      sec('Reading the Convene page',
        p('The table shows each seat on the arc and the chairman’s bench below. A spinning ring means that seat’s model call is out; the thin arc around a seat during the answers is an estimate from that model’s usual time, not real progress. When a seat answers, its first line appears above it.'),
        p('During the blind vote the seats show their response letters and the vote matrix fills as each ballot lands. Red sparks run between the seats that debate a disputed point. Seat colours stay neutral until the verdict: then green means in line with the decision and amber means the chairman named it as dissent.'),
        p('Tokens, calls and cost update when each call finishes. Cost is an estimate from token counts and list prices: subscription calls count $0, and calls with no known price are shown as “+” and never counted as $0. Models do not report what they are thinking, so the log shows only what has happened: answers, ballots, positions and the verdict.'),
        p('⏳ Running short: when about 70% of the time or budget is used, an amber box offers more time or money; nothing stops while it shows. If a stage is about to be skipped or cut, the run pauses for up to 45 seconds and asks (paused time does not count). Decline, or no answer, continues within the limit, and it will not ask about that limit again. When to use it: the committee is close to a good answer and losing the blind vote or the debate would cost more than a few minutes or cents.'),
        p('↺ Replay plays a finished run again at 10× speed, rebuilt from its saved timings, ballots and positions. When to use it: to show someone how a decision was reached, or to see where the time went.')),
      sec('Running',
        p('Convene starts the committee in its own chat in the background; you stay on this page and watch. The chat appears in the sidebar (named after the committee), with the answer and the PDF, so you can open it any time.'),
        p('Committee name (optional, above the question): shown in History, on the verdict and on the report. When to use it: when you run several committees on one topic and want to find them again.'),
        p('🔁 Follow up (on the Decision page): the same seats, models, time limit and budget answer a new question, with a new blind vote and verdict. The seats get the earlier question, verdict and dissent as background, and the same evidence (picture descriptions are reused); you can add new or missed files in the dialog. When to use it: a new fact, a changed constraint or a “what if”. To have the verdict explained or reworded, use 💬 Discuss in chat instead: one model answers there, at no committee cost.'),
        p('Stop (here or in the chat) prevents new model calls. Calls already sent may still finish and be billed.'),
        p('⏱ Time limit (default 10 minutes, in the committee settings): the whole run must finish within it. Time for the chairman is kept back, so you still get a verdict. If time runs short, seats that have not answered are left out and the blind vote is skipped; the verdict says so. If even the chairman runs out of time, the run stops with “No verdict” and the answers are kept.'),
        p('When to use it: keep 10 minutes for everyday questions, choose 5 for a quick check, and raise it for long evidence, many seats or slow reasoning models. Choose “No limit” only when you will watch the run.'),
        p('💲 Budget (default $2, next to the time limit): the most this run may spend. Before Convene the bar shows an estimate, e.g. “$0.08–$0.45”: the low end is a typical run, the high end assumes every model writes its longest answer. The run is checked against the high end, so it stays under the limit: if the answers would not fit, they are made shorter; if the vote no longer fits after the answers, it is skipped; if the chairman cannot fit, the run stops with “No verdict” and the answers are kept.'),
        p('Seats on a subscription are not priced and not counted. Pay-per-use seats are priced from Hermes’s own pricing tables (the ones it uses for session costs), else the same model’s public price on OpenRouter or Nous Portal. Hermes also detects which providers are subscriptions; correct one in the 💲 budget menu if it is wrong. A pay-per-use model with no price at all needs one before Convene: type it in the 💲 budget menu (saved). Cost is an estimate, not your provider’s bill.'),
        p('When to use it: keep $2 for everyday questions on paid APIs. Lower it (e.g. $0.50) when trying a new committee or expensive models; raise it for long evidence, many seats or several photos. Choose “No limit” when every seat is on a subscription.'),
        p('💾 Save committee (on the Convene bar, or on a verdict) keeps the seats, their models and instructions, photo settings, the chairman, the blind vote, the time limit and the budget under a name of your choice. It appears under “My committees”: pick it, type a new question, add new evidence, Convene. When to use it: a committee you will ask again, such as a reliability board for one plant. To remove one, pick it and click 🗑 Delete twice; one written by hand in config.yaml is only hidden, config.yaml is not changed.'),
        p('You can also start a committee from any chat: /hermes-committee:convene followed by your question.'),
        p('With attached files, ask in plain words instead (Desktop does not allow a slash command with attachments): attach the files and write “Convene a committee: <your question>. Use the attached files as evidence.” You can add “under $1 and 5 minutes”.')),
      sec('🗣 Deliberation',
        p('After the answers and the blind vote, the chairman lists the points the seats disagree on and tags each one: about the action, about a claim the decision rests on, or only emphasis. The plugin then decides from your setting whether to debate. In a round, every seat sees the others’ answers and must hold or change its position on each point, with its argument and evidence. “The majority agrees” is not accepted as a reason. With 2 rounds, the second covers only the points still open. The chairman then makes the final call and says which argument decided each point.'),
        p('Settings (🗣 chip on the Convene bar): When they disagree (recommended) debates differences on the action or on a claim; Only on the action skips disputes about facts when the action is agreed; Always runs a round even when they agree; Never is the classic three stages.'),
        p('The verdict’s Deliberation tab and the report show each seat’s position before and after, and who changed their mind and why, so you can see whether a seat was persuaded by evidence or just went along.'),
        p('When to use it: real trade-offs where reasonable experts would argue, such as run or swap, repair or replace, or accepting a risk. Choose Never for fact look-ups and quick checks. One round costs about one more answer per seat, and is only spent when the seats disagree; if the time limit or budget has no room, the debate is skipped and the verdict still comes.')),
      sec('🔒 Providers that need your OK',
        p('Hermes only lets the committee call providers you have allowed. A 🔒 provider needs one command in a terminal; when you press Convene the page shows it with a Copy button. The plugin never changes this permission by itself.'),
        Array.isArray(setup?.policy?.providers) ? p(`Allowed now: ${setup.policy.providers.join(', ') || 'none'}.`) : p('Right now every connected provider is allowed.')),
      sec('Cost and privacy',
        p('One run makes 2 × seats + 1 model calls: the seats answer, the seats vote, the chairman decides. Your question and evidence go to every seat’s provider. Each run records token counts and an estimated cost; the report has a cost table by stage.'),
        p('The result is advice. For safety, legal, medical or financial decisions, a qualified person must review it.'))))
}

// ── set-up screen ──────────────────────────────────────────────────────────

const blank = (name = '') => ({ name, instructions: '', provider: '', model: '' })
const clean = s => Object.fromEntries(Object.entries({ provider: s.provider, model: s.model }).filter(([, v]) => v))

function fromRoster(r) {
  return { template: r.template, review: r.review !== false, research: !!r.research, research_secs: r.research_secs || 90, ...(r.deliberate ? { deliberate: r.deliberate, rounds: r.deliberate_rounds || 1 } : {}),
    chairGuidance: r.chairman_guidance || undefined, decision: typeof r.decision_focus === 'boolean' ? r.decision_focus : undefined,
    chairman: { provider: r.chairman?.provider || '', model: r.chairman?.model || '', ...(r.chairman?.thinking ? { thinking: r.chairman.thinking } : {}) },
    seats: (r.seats || []).map(s => ({ name: s.name || '', instructions: s.instructions || '', provider: s.provider || '', model: s.model || '',
      ...(typeof s.vision === 'boolean' ? { vision: s.vision } : {}), ...(s.thinking ? { thinking: s.thinking } : {}) })),
    ...(r.max_minutes != null ? { maxMinutes: r.max_minutes } : {}), ...(r.max_usd != null ? { maxUsd: r.max_usd } : {}) }
}

const QUICK_USD = [0.5, 1, 2, 5]
const COMMITTEE_NAME = /^[\p{L}\p{N}][\p{L}\p{N}_ .()&+\-]{0,39}$/u

// A saved committee from a run: seats, models, instructions, photo settings, chairman and limits.
function presetFromRun(run) {
  return { template: run.template, review: run.review !== false, research: !!run.research, deliberate: run.deliberate || 'never', deliberate_rounds: run.deliberate_rounds || 1,
    ...(run.custom_brief ? { chairman_guidance: run.chairman_guidance, decision_focus: !!run.decision_focus } : {}),
    max_minutes: run.limits?.max_secs ? Math.round(run.limits.max_secs / 60) : 0, max_usd: run.limits?.max_usd || 0,
    chairman: Object.fromEntries(Object.entries({ provider: run.chairman?.provider, model: run.chairman?.model, thinking: run.chairman?.thinking }).filter(([, v]) => v)),
    seats: (run.seats || []).slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0)).map(s => ({ name: s.name, instructions: s.instructions || '',
      ...(s.provider ? { provider: s.provider } : {}), ...(s.model ? { model: s.model } : {}), ...(typeof s.vision === 'boolean' ? { vision: s.vision } : {}), ...(s.thinking ? { thinking: s.thinking } : {}) })) }
}

function SaveCommitteeButton({ run }) {
  const [name, setName] = useState(null)
  const save = async () => {
    const n = (name || '').trim()
    if (!COMMITTEE_NAME.test(n)) { notify('warning', 'Give the committee a name: up to 40 characters, starting with a letter or digit.'); return }
    try {
      const r = await rest('/presets', { method: 'POST', body: { name: n, preset: presetFromRun(run) } })
      if (!r || r.ok === false) throw new Error(r?.error || 'not saved')
      notify('success', `Saved committee “${n}”. Pick it under “My committees” when you set up the next one.`)
      setName(null)
      queryClient.invalidateQueries({ queryKey: [...KEY, 'setup'] })
    } catch (e) { notify('error', `Save failed: ${e.message || e}`) }
  }
  if (name === null) return h(Button, { size: 'sm', variant: 'ghost', onClick: () => setName(run.title || ''),
    title: 'Save these seats, models, instructions, chairman and limits to reuse with any question and evidence' }, '💾 Save committee')
  return h('span', { className: 'hc-row' },
    h('input', { className: 'hc-in', style: { width: 190 }, autoFocus: true, dir: 'auto', maxLength: 40, value: name, placeholder: 'Committee name',
      onChange: e => setName(e.target.value), onKeyDown: e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setName(null) } }),
    h(Button, { size: 'sm', variant: 'outline', onClick: save }, 'Save'), h(Button, { size: 'sm', variant: 'ghost', onClick: () => setName(null) }, 'Cancel'))
}

// The budget chip on the Convene bar and its small menu.
// The user's price for a pay-per-use model nobody publishes a price for (USD per million tokens).
function PriceEntry({ provider, model, who, onPrice }) {
  const [pin, setPin] = useState('')
  const [pout, setPout] = useState('')
  const ok = pin !== '' && pout !== '' && Number(pin) >= 0 && Number(pout) >= 0
  return h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginTop: 4 } },
    h('span', { className: 'hc-warn-t' }, `${who} (${shortModel(model)}): $`),
    h('input', { className: 'hc-in', type: 'number', min: 0, step: 0.01, style: { width: 64 }, placeholder: 'in', value: pin, onChange: e => setPin(e.target.value) }),
    h('span', { className: 'hc-muted' }, 'in, $'),
    h('input', { className: 'hc-in', type: 'number', min: 0, step: 0.01, style: { width: 64 }, placeholder: 'out', value: pout, onChange: e => setPout(e.target.value) }),
    h('span', { className: 'hc-muted' }, 'out per million tokens'),
    h(Button, { size: 'sm', variant: 'outline', disabled: !ok, onClick: () => onPrice(provider, model, Number(pin), Number(pout)) }, 'Save price'))
}

// How each provider in the committee is paid for: a subscription is not priced, pay per use is.
function BillingRows({ est, onBilling, onPrice }) {
  const rows = est?.providers || []
  if (!rows.length) return null
  return h('div', { style: { marginTop: 10 } },
    h('div', { className: 'hc-lbl' }, 'How you pay for each provider'),
    rows.map(r => {
      const note = r.unpriced.length ? h('span', { className: 'hc-warn-t' }, ` · no price: ${r.unpriced.join(', ')}`)
        : r.via.length ? h('span', { className: 'hc-muted', title: 'This provider publishes no price, so the same model’s public list price is used as an estimate.' },
          ` · list price via ${[...new Set(r.via.map(v => v.replace(/^.*\((.*)\)$/, '$1')))].join(', ')}`) : null
      const how = r.in_config ? `${r.billing === 'subscription' ? 'Subscription' : 'Pay per use'} · set in config.yaml`
        : r.chosen ? 'your choice' : r.detected ? `detected by Hermes: ${r.billing === 'subscription' ? 'subscription' : 'pay per use'}`
        : r.unpriced.length ? 'not detected: enter a price or mark it' : 'not detected: counted at list price'
      return h('div', { key: r.provider, className: 'hc-xs', style: { marginTop: 6, display: 'block' } },
        h('div', { style: { display: 'block', lineHeight: 1.4 } }, h('b', { title: r.who.join(', ') }, r.provider), h('span', { className: 'hc-muted' }, ` (${r.who.join(', ')})`), note),
        r.in_config ? h('div', { className: 'hc-muted' }, how)
          : h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginTop: 3 } },
            [['subscription', 'Subscription'], ['paid', 'Pay per use']].map(([v, label]) => h('button', { key: v,
              className: `hc-chip${r.chosen && r.billing === v ? ' hc-on' : ''}`, onClick: () => onBilling(r.provider, r.chosen && r.billing === v ? null : v) }, label)),
            how ? h('span', { className: 'hc-muted' }, how) : null),
        r.billing !== 'subscription' ? (r.need_price || []).map(x => h(PriceEntry, { key: x.model, provider: r.provider, model: x.model, who: x.who, onPrice })) : null)
    }),
    h('div', { className: 'hc-xs hc-muted', style: { marginTop: 4 } }, 'Hermes detects most providers. Change one only if it is wrong; your answer is saved, used by chat runs too, and undone by clicking it again.'))
}

function Budget({ value, max, est, onChange, onBilling, onPrice }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    const off = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', off)
    return () => document.removeEventListener('mousedown', off)
  }, [open])
  const set = v => onChange(Math.max(0, Math.min(max, Math.round((Number(v) || 0) * 100) / 100)))
  const over = value && est && est.high > value
  return h('span', { className: 'hc-tl', ref },
    h('button', { className: `hc-chip${value ? '' : ' hc-chip-off'}${over ? ' hc-warn-t' : ''}`, onClick: () => setOpen(!open), title: 'Budget for the whole run' },
      value ? `💲 ${usd(value)} max` : '💲 No budget'),
    open ? h('div', { className: 'hc-pop hc-tlpop' },
      h('div', { className: 'hc-lbl' }, 'Budget for the whole run (USD)'),
      h('div', { className: 'hc-row hc-wrap' },
        QUICK_USD.filter(m => m <= max).map(m => h('button', { key: m, className: `hc-chip${value === m ? ' hc-on' : ''}`, onClick: () => set(m) }, usd(m))),
        h('button', { className: `hc-chip${value ? '' : ' hc-on'}`, onClick: () => set(0) }, 'No limit')),
      h('label', { className: 'hc-row hc-sm', style: { marginTop: 8 } }, 'Other: $',
        h('input', { className: 'hc-in', type: 'number', min: 0.01, step: 0.25, max, style: { width: 80 }, value: value || '', placeholder: '—',
          onChange: e => set(e.target.value) }), `(max $${max})`),
      est ? h('div', { className: 'hc-xs', style: { marginTop: 8 } }, h('b', null, 'This committee: '), `${usd(est.low)} typical, up to ${usd(est.high)}`,
        est.subscription?.length ? h('div', { className: 'hc-muted' }, `On a subscription, not counted: ${est.subscription.join(', ')}`) : null,
        est.unknown?.length ? h('div', { className: 'hc-warn-t' }, `No price for ${est.unknown.join(', ')}. Enter it below, or mark the provider Subscription.`) : null) : null,
      h(BillingRows, { est, onBilling, onPrice }),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } },
        'Every call is checked at its most expensive before it is sent. If money runs short, answers are made shorter, then the blind vote is skipped; the chairman’s share is kept back so you still get a verdict.'),
      h('div', { className: 'hc-xs', style: { marginTop: 6 } }, h('b', null, 'When to use it: '),
        'keep $2 for everyday questions on paid APIs. Lower it when trying expensive models or a new committee; raise it for long evidence, many seats or photos. Choose No limit when every seat is on a subscription.'),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 6 } }, 'An estimate from token counts and list prices, not your provider’s bill.')) : null)
}

const QUICK_MINUTES = [5, 10, 20, 30]

// The time limit chip on the Convene bar and its small menu.
function Deliberate({ value, rounds, n, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    const off = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', off)
    return () => document.removeEventListener('mousedown', off)
  }, [open])
  const on = value !== 'never'
  return h('span', { className: 'hc-tl', ref },
    h('button', { className: `hc-chip${on ? '' : ' hc-chip-off'}`, onClick: () => setOpen(!open), title: 'Let the seats debate the points they disagree on before the chairman decides' },
      on ? `🗣 Deliberate: ${DELIB[value][0].toLowerCase()}${rounds === 2 ? ' · 2 rounds' : ''}` : '🗣 No deliberation'),
    open ? h('div', { className: 'hc-pop hc-tlpop', style: { width: 340 } },
      h('div', { className: 'hc-lbl' }, 'Deliberation before the verdict'),
      ['material', 'action', 'always', 'never'].map(k => h('button', { key: k, className: `hc-rrow${value === k ? ' hc-on' : ''}`, style: { textAlign: 'left' }, onClick: () => onChange({ deliberate: k }) },
        h('span', null, value === k ? '●' : '○'), h('span', { className: 'hc-grow' }, h('b', null, DELIB[k][0]), k === 'material' ? ' (recommended)' : '', h('div', { className: 'hc-xs hc-muted' }, DELIB[k][1])))),
      on ? h('div', { className: 'hc-row hc-sm', style: { marginTop: 8 } }, 'Rounds:',
        [1, 2].map(r => h('button', { key: r, className: `hc-chip hc-chip-sm${rounds === r ? ' hc-on' : ''}`, onClick: () => onChange({ rounds: r }) }, String(r))),
        h('span', { className: 'hc-xs hc-muted' }, `up to ${debateCalls(n, value, rounds)} more calls`)) : null,
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } },
        'After the answers and the vote, the chairman lists where the seats disagree. If that matches your choice, each seat answers those points: hold or change its position, with a reason. The chairman then makes the final call and says which argument decided each point.'),
      h('div', { className: 'hc-xs', style: { marginTop: 6 } }, h('b', null, 'When to use it: '),
        'real trade-offs where experts would argue, such as run or swap, repair or replace, accept a risk. Use Never for fact look-ups or quick checks. A round costs about one more answer per seat, and is only paid when they disagree.')) : null)
}

function TimeLimit({ value, max, typical, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    const off = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', off)
    return () => document.removeEventListener('mousedown', off)
  }, [open])
  const set = v => onChange(Math.max(0, Math.min(max, Math.round(Number(v) || 0))))
  return h('span', { className: 'hc-tl', ref },
    h('button', { className: `hc-chip${value ? '' : ' hc-chip-off'}`, onClick: () => setOpen(!open), title: 'Time limit for the whole run' },
      value ? `⏱ ${value} min` : '⏱ No time limit'),
    open ? h('div', { className: 'hc-pop hc-tlpop' },
      h('div', { className: 'hc-lbl' }, 'Time limit for the whole run'),
      h('div', { className: 'hc-row hc-wrap' },
        QUICK_MINUTES.filter(m => m <= max).map(m => h('button', { key: m, className: `hc-chip${value === m ? ' hc-on' : ''}`, onClick: () => set(m) }, `${m} min`)),
        h('button', { className: `hc-chip${value ? '' : ' hc-on'}`, onClick: () => set(0) }, 'No limit')),
      h('label', { className: 'hc-row hc-sm', style: { marginTop: 8 } }, 'Other:',
        h('input', { className: 'hc-in', type: 'number', min: 1, max, style: { width: 70 }, value: value || '', placeholder: '—',
          onChange: e => set(e.target.value) }), `minutes (max ${max})`),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } },
        'The chairman’s time is kept back, so you still get a verdict. If time runs short, slow seats are left out and the blind vote is skipped.'),
      h('div', { className: 'hc-xs', style: { marginTop: 6 } }, h('b', null, 'When to use it: '),
        'keep 10 min for everyday questions. Use 5 min for a quick check. Raise it for long evidence, many seats or slow reasoning models.'),
      typical ? h('div', { className: 'hc-xs hc-muted', style: { marginTop: 6 } }, `This committee usually takes about ${typical} min.`) : null) : null)
}

// ── evidence files ─────────────────────────────────────────────────────────
// Files are read once by the backend when added (POST /evidence/<pack>/files). The page keeps
// only summaries: name, size, tokens, notes. Every chosen file goes to every seat.

const EV_EXT = ['.txt', '.md', '.markdown', '.csv', '.tsv', '.json', '.log', '.yaml', '.yml', '.xml', '.html', '.htm', '.ini',
  '.cfg', '.toml', '.rst', '.pdf', '.docx', '.xlsx', '.xlsm', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.heic', '.heif']
const EV_ICON = { text: '📝', pdf: '📕', word: '📘', excel: '📗', image: '📷' }
const EV_MAX_MB = 25
const IMG_TOKENS = 1600
const DESC_TOKENS = 250                     // a written picture description, roughly
const extOf = n => (String(n).match(/\.[^.\\/]+$/) || [''])[0].toLowerCase()
const fmtSize = b => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`)
const fmtNum = n => Math.round(n).toLocaleString('en-US')
const readAsDataUrl = file => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(String(r.result))
  r.onerror = () => reject(r.error || new Error('could not read the file'))
  r.readAsDataURL(file)
})

// Who sees the pictures: the seat's own choice; else every model is tried, except one that refused a picture before.
const seesPhotos = (slot, caps) => (typeof slot.vision === 'boolean' ? slot.vision : caps?.[`${slot.provider || ''}|${slot.model || ''}`]?.vision !== false)

function useCaps(slots) {
  const routes = slots.map(s => ({ provider: s.provider || '', model: s.model || '' }))
  const key = JSON.stringify(routes)
  return useQuery({ queryKey: [...KEY, 'caps', key], staleTime: 600_000,
    queryFn: async () => (await rest('/caps', { method: 'POST', body: { routes } })).caps || {} })
}

function evidenceStats(files, seats, chair, review, caps) {
  const on = files.filter(f => f.fid && f.on !== false)
  const pics = on.reduce((a, f) => a + (f.images || 0), 0)
  const textTok = on.reduce((a, f) => a + (f.tokens || 0) - (f.images || 0) * IMG_TOKENS, 0)
  const seeing = seats.filter(s => seesPhotos(s, caps)).length
  const calls = seats.length + (review ? seats.length : 0) + 1
  const total = textTok * calls + pics * (IMG_TOKENS * (seeing + 1) + DESC_TOKENS * (calls - seeing))
  return { on, pics, textTok, seeing, calls, total, perCall: textTok + pics * IMG_TOKENS,
           describer: pics ? (seesPhotos(chair, caps) || seeing > 0) : true }
}

function EvidencePanel({ state, upd, caps, stats }) {
  const inputRef = useRef(null)
  const [drag, setDrag] = useState(false)
  const [paste, setPaste] = useState(!!state.evidence)
  const files = state.files || []
  const setFiles = fn => upd(s => ({ files: fn(s.files || []) }))

  const packRef = useRef(state.pack || null)
  useEffect(() => { packRef.current = state.pack || null }, [state.pack])
  const ensurePack = async () => {
    if (packRef.current) return packRef.current
    const r = await rest('/evidence', { method: 'POST', body: {} })
    packRef.current = r.pack
    upd({ pack: r.pack })
    return r.pack
  }
  const send = async (pack, name, dataUrl) => {
    let r = await rest(`/evidence/${pack}/files`, { method: 'POST', body: { name, data: dataUrl }, timeoutMs: 180_000 })
    if (!r.ok && /expired/i.test(r.error || '')) {                        // a pack older than a week
      const fresh = (await rest('/evidence', { method: 'POST', body: {} })).pack
      packRef.current = fresh
      upd({ pack: fresh, files: [] })
      r = await rest(`/evidence/${fresh}/files`, { method: 'POST', body: { name, data: dataUrl }, timeoutMs: 180_000 })
    }
    return r
  }
  // items: [{ name, size, read: () => Promise<dataUrl> }]
  const addAll = async items => {
    if (!items.length) return
    let pack
    try { pack = await ensurePack() } catch (e) { notify('error', `Could not start the evidence list: ${e.message || e}`); return }
    for (const it of items) {
      const tmp = `t${Math.random().toString(36).slice(2, 9)}`
      setFiles(fs => [...fs, { tmp, name: it.name, size: it.size, busy: true }])
      let row
      try {
        if (!EV_EXT.includes(extOf(it.name))) throw new Error('This file type is not supported. Use text, PDF, Word, Excel or photos.')
        if (it.size > EV_MAX_MB * 1048576) throw new Error(`Over the ${EV_MAX_MB} MB limit per file.`)
        const r = await send(pack, it.name, await it.read())
        row = r.ok ? { ...r.file, on: true } : { name: it.name, size: it.size, error: r.error }
      } catch (e) { row = { name: it.name, size: it.size, error: e.message || String(e) } }
      setFiles(fs => fs.map(f => (f.tmp === tmp ? row : f)))
    }
  }
  const addBrowserFiles = list => addAll([...(list || [])].map(f => ({ name: f.name, size: f.size, read: () => readAsDataUrl(f) })))
  const chooseFolder = async () => {
    const files = CTX.files
    if (!files?.requestFolder) { notify('warning', 'This Hermes Desktop has no folder access for plugins yet. Use “Add files” instead.'); return }
    const picked = await files.requestFolder()
    if (!picked?.ok) { if (picked?.message) notify('warning', picked.message); return }
    if (!picked.folder) return
    const found = [], skipped = []
    const walk = async (path, depth) => {
      const r = await files.list(picked.folder.id, path || undefined)
      if (!r?.ok) return
      for (const e of r.entries || []) {
        if (found.length >= 60) return
        if (e.isDirectory) { if (depth < 2 && !e.name.startsWith('.')) await walk(e.path, depth + 1) }
        else if (EV_EXT.includes(extOf(e.name)) && !e.name.startsWith('~$')) found.push(e)
        else if (!e.name.startsWith('.') && !e.name.startsWith('~$')) skipped.push(e.name)
      }
    }
    await walk('', 0)
    const skipNote = skipped.length ? ` Skipped ${skipped.length} unsupported: ${skipped.slice(0, 4).join(', ')}${skipped.length > 4 ? '…' : ''}.` : ''
    if (!found.length) { notify('info', `No supported files in “${picked.folder.name}”.${skipNote}`); return }
    notify('info', `Reading ${found.length} file${found.length > 1 ? 's' : ''} from “${picked.folder.name}”…${skipNote}`)
    const items = []
    for (const e of found) {
      const st = await files.stat(picked.folder.id, e.path)
      items.push({ name: e.path.replace(/\\/g, '/'), size: st?.size || 0, read: async () => {
        const r = await files.readDataUrl(picked.folder.id, e.path)
        if (!r?.ok) throw new Error(r?.message || 'Hermes could not read this file (files above 16 MB cannot be read from a folder).')
        return r.url
      } })
    }
    await addAll(items)
  }
  const remove = async f => {
    setFiles(fs => fs.filter(x => x !== f))
    if (f.fid && packRef.current) { try { await rest(`/evidence/${packRef.current}/files/${f.fid}`, { method: 'DELETE' }) } catch { /* the run only uses ticked files */ } }
  }
  const toggle = f => setFiles(fs => fs.map(x => (x === f ? { ...x, on: x.on === false } : x)))
  const [confirmClear, setConfirmClear] = useState(false)
  const clearAll = async () => {
    if (!confirmClear) { setConfirmClear(true); setTimeout(() => setConfirmClear(false), 4000); return }
    setConfirmClear(false)
    if (files.some(f => f.busy)) { notify('warning', 'Wait until every file is read, then clear.'); return }
    const pack = packRef.current
    setFiles(() => [])
    if (pack) { try { await rest(`/evidence/${pack}`, { method: 'DELETE' }) } catch { /* the run only uses listed files */ } }
  }

  return h('div', { className: `hc-ev${drag ? ' hc-ev-drag' : ''}`,
      onDragOver: e => { e.preventDefault(); setDrag(true) }, onDragLeave: () => setDrag(false),
      onDrop: e => { e.preventDefault(); setDrag(false); addBrowserFiles(e.dataTransfer?.files) } },
    h('div', { className: 'hc-row hc-wrap' },
      h('b', { className: 'hc-sm' }, 'Evidence'),
      h('span', { className: 'hc-xs hc-muted' }, 'every seat reads the same files'),
      h('span', { className: 'hc-grow' }),
      h('input', { ref: inputRef, type: 'file', multiple: true, accept: EV_EXT.join(','), style: { display: 'none' },
        onChange: e => { addBrowserFiles(e.target.files); e.target.value = '' } }),
      h(Button, { size: 'sm', variant: 'outline', onClick: () => inputRef.current?.click() }, '📄 Add files'),
      CTX.files?.requestFolder ? h(Button, { size: 'sm', variant: 'outline', onClick: chooseFolder }, '📁 Choose folder') : null,
      h(Button, { size: 'sm', variant: 'ghost', onClick: () => setPaste(!paste) }, paste ? 'Hide text box' : '✎ Paste text'),
      files.length ? h(Button, { size: 'sm', variant: confirmClear ? 'destructive' : 'ghost', onClick: clearAll, title: 'Remove every file from this list (past runs keep their own copies)' },
        confirmClear ? `Click again to remove ${files.length} file${files.length > 1 ? 's' : ''}` : '🗑 Clear all') : null),
    !files.length ? h('div', { className: 'hc-xs hc-muted hc-ev-hint' },
      h('b', null, 'When to use it: '), 'add the files the decision rests on, such as inspection reports, trend exports or photos of the damage. ',
      'Text, PDF, Word, Excel and photos, up to 25 MB each. You can also drop files here. ',
      'Leave out what does not matter: every file goes to every seat in almost every call.') : null,
    files.length ? h('div', { className: 'hc-evlist' }, files.map((f, i) => h('div', { key: f.fid || f.tmp || i, className: `hc-evrow${f.error ? ' hc-evbad' : ''}${f.on === false ? ' hc-evoff' : ''}` },
      f.fid ? h('input', { type: 'checkbox', checked: f.on !== false, onChange: () => toggle(f), title: 'Untick to leave this file out' }) : h('span', { className: 'hc-evsp' }),
      h('span', null, f.error ? '⚠' : f.busy ? '⏳' : EV_ICON[f.kind] || '📄'),
      h('span', { className: 'hc-ellipsis hc-evname', title: f.name }, f.name),
      f.busy ? h('span', { className: 'hc-xs hc-muted' }, 'reading…') :
        f.error ? h('span', { className: 'hc-xs hc-warn-t hc-ellipsis hc-grow', title: f.error }, f.error) :
          h(Fragment, null,
            h('span', { className: 'hc-xs hc-muted' }, `${fmtSize(f.size || 0)} · ≈ ${fmtNum(f.tokens || 0)} tokens`),
            h('span', { className: 'hc-xs hc-muted hc-ellipsis hc-grow', title: (f.notes || []).join('\n') },
              f.kind === 'image' ? `photo · seen by ${stats.seeing} seat${stats.seeing === 1 ? '' : 's'}, described for the rest` : (f.notes || []).join(' · '))),
      h('button', { className: 'hc-link', title: 'Remove', onClick: () => remove(f) }, '✕')))) : null,
    stats.on.length ? h('div', { className: 'hc-xs hc-evsum' },
      `${stats.on.length} file${stats.on.length > 1 ? 's' : ''} · ≈ ${fmtNum(stats.perCall)} tokens per call · sent in ~${stats.calls} calls ≈ ${fmtNum(stats.total)} input tokens per run`,
      stats.pics ? h('div', null, `📷 ${stats.pics} picture${stats.pics > 1 ? 's' : ''}: ${stats.seeing} of ${state.seats.length} seats can see them; a vision model writes a description for the others, the blind vote and the chairman (${stats.pics} extra call${stats.pics > 1 ? 's' : ''}). Each picture costs ≈ ${fmtNum(IMG_TOKENS)} tokens per call to a seat that sees it.`) : null,
      stats.pics && !stats.describer ? h('div', { className: 'hc-warn-t' }, 'Every model here has refused pictures before or is set to descriptions only. Give the chairman or one seat a vision model, or remove the photos.') : null) : null,
    paste ? h('textarea', { className: 'hc-in', rows: 4, style: { marginTop: 8 }, value: state.evidence, onChange: e => upd({ evidence: e.target.value }),
      placeholder: 'Or paste facts every AI should use: measurements, costs, logs, notes.' }) : null,
    h('div', { className: 'hc-research' },
      h('button', { className: `hc-sw${state.research ? ' hc-on' : ''}`, role: 'switch', 'aria-checked': !!state.research, title: 'Research first',
        onClick: () => upd({ research: !state.research }) }),
      h('div', null, h('b', { className: 'hc-sm' }, '🌐 Research first'),
        h('div', { className: 'hc-xs hc-muted' }, 'Hermes searches the web once before the seats start and adds what it finds, with sources, to the evidence. Every seat gets the same facts.',
          state.research ? h(Fragment, null, ' On a run from this page you can untick sources before the seats start.') : null),
        h('div', { className: 'hc-xs hc-muted' }, h('b', null, 'When to use it: '), 'questions that depend on current facts, prices, standards or recent events. Leave it off for your own data and internal decisions.'),
        state.research ? h('div', { className: 'hc-row hc-xs', style: { gap: 6, marginTop: 4 } }, h('span', { className: 'hc-muted' }, 'Time allowed:'),
          [[45, '45 s'], [90, '1½ min'], [150, '2½ min']].map(([v, l]) => h('button', { key: v, className: `hc-chip hc-chip-sm${(state.research_secs || 90) === v ? ' hc-on' : ''}`,
            title: 'Research stops at this point and the committee goes on with what it found (your source review does not count)', onClick: () => upd({ research_secs: v }) }, l))) : null,
        state.research ? h('div', { className: 'hc-xs hc-warn-t' }, 'Your question (not your files) is sent to your web search provider. About 2 extra calls on the chairman’s model and ~2,300 more tokens for each seat.') : null)))
}

function SetupView({ setup, models, onConvened, openHelp }) {
  const [state, setState] = useState(() => {
    const saved = CTX.storage.get('setup', null)
    if (saved && Array.isArray(saved.seats) && saved.seats.length) return saved
    const name = setup.default_preset && setup.rosters?.[setup.default_preset] && !setup.rosters[setup.default_preset].error ? setup.default_preset : null
    if (name) return { ...fromRoster(setup.rosters[name]), source: name, question: '', evidence: '' }
    const t = setup.templates.find(x => x.id === 'classic') || setup.templates[0]
    return { template: t.id, review: true, chairGuidance: t.chairman_guidance, decision: !!t.decision_focus, chairman: blank(), seats: t.seats.map(s => ({ ...blank(s.name), instructions: s.instructions })), source: t.id, question: '', evidence: '' }
  })
  const [sel, setSel] = useState(null)
  const [showEvidence, setShowEvidence] = useState(!!state.evidence || !!(state.files || []).length)
  const [busy, setBusy] = useState(false)
  const [gate, setGate] = useState(null)
  const [saveAs, setSaveAs] = useState(null)
  const [confirmDel, setConfirmDel] = useState(false)
  const [guide, setGuide] = useState(() => !CTX.storage.get('guideHidden', false))
  const qRef = useRef(null)
  useEffect(() => { CTX.storage.set('setup', state) }, [state])

  // Mirrors Hermes's rule: no list = anything allowed; a list restricts; "*" allows all.
  const plist = setup.policy?.providers
  const locked = p => !!p && Array.isArray(plist) && !plist.includes('*') && !plist.includes(String(p).toLowerCase())
  const upd = patch => setState(s => ({ ...s, ...(typeof patch === 'function' ? patch(s) : patch) }))
  const updSeat = (i, patch) => setState(s => ({ ...s, seats: s.seats.map((x, j) => (j === i ? { ...x, ...patch } : x)) }))
  const L = setup.limits
  const n = state.seats.length
  const maxMin = state.maxMinutes ?? L.max_minutes_default ?? 10
  const maxUsd = state.maxUsd ?? L.max_usd_default ?? 2
  const [est, setEst] = useState(null)
  const [unknownOk, setUnknownOk] = useState(false)
  const [billTick, setBillTick] = useState(0)
  const priceStamp = usePrices().dataUpdatedAt || 0
  const caps = useCaps([...state.seats, state.chairman]).data || {}
  const stats = evidenceStats(state.files || [], state.seats, state.chairman, state.review, caps)

  const pickTemplate = t => {
    const prev = state.seats
    const keep = i => prev[i] || prev[i % prev.length] || {}
    upd({ template: t.id, source: t.id, chairGuidance: t.chairman_guidance, decision: !!t.decision_focus, seats: t.seats.map((s, i) => ({ name: s.name, instructions: s.instructions, provider: keep(i).provider || '', model: keep(i).model || '' })) })
    setSel(null)
  }
  const pickPreset = name => { const r = setup.rosters[name]; if (r && !r.error) { setState(s => ({ ...s, ...fromRoster(r), source: name })); setSel(null) } }
  const addSeat = () => { if (n >= L.max_seats) return; const last = state.seats[n - 1] || blank(); upd({ seats: [...state.seats, { ...blank(`Member ${n + 1}`), provider: last.provider, model: last.model }] }); setSel(n) }

  const names = state.seats.map(s => s.name.trim().toLowerCase())
  const problems = []
  if (!state.question.trim()) problems.push('Type the question first.')
  if (names.some(x => !x)) problems.push('Every seat needs a role name.')
  if (new Set(names).size !== names.length) problems.push('Two seats have the same name.')
  if (state.seats.some(s => s.instructions.length > MAX_INSTR)) problems.push(`Instructions are limited to ${MAX_INSTR} characters.`)
  if ((state.files || []).some(f => f.busy)) problems.push('Wait until every file is read.')
  if (stats.pics && !stats.describer) problems.push('Your photos need a model that can see pictures: set one on the chairman or a seat, or remove the photos.')
  const lockedNow = [...new Set([...state.seats, state.chairman].map(s => s.provider).filter(locked))]
  const sameModel = n > 1 && new Set(state.seats.map(s => `${s.provider}/${s.model}`)).size === 1
  const exp = setup.expected_secs || {}
  const slowest = Math.max(0, ...state.seats.map(s => exp[s.model || 'active model'] || 0))
  const minutes = slowest ? Math.max(1, Math.round((slowest * (state.review ? 1.6 : 1) + (exp[state.chairman.model || 'active model'] || 40)) / 60)) : null

  const tplNow = setup.templates.find(x => x.id === state.template) || {}
  const chairGuidance = (state.chairGuidance ?? tplNow.chairman_guidance ?? '').slice(0, MAX_INSTR)
  const decision = typeof state.decision === 'boolean' ? state.decision : !!tplNow.decision_focus
  const customBrief = chairGuidance.trim() !== (tplNow.chairman_guidance || '').trim() || decision !== !!tplNow.decision_focus
  const delib = state.deliberate || L.deliberate_default || 'material'
  const rounds = state.rounds === 2 ? 2 : 1
  const estBody = JSON.stringify({ question: state.question || '?', evidence: state.evidence, template: state.template, review: state.review, research: !!state.research, research_secs: state.research_secs || 90,
    deliberate: delib, deliberate_rounds: rounds, chairman_guidance: chairGuidance, decision_focus: decision,
    chairman: state.chairman, max_usd: maxUsd, seats: state.seats.map(s => ({ name: s.name.trim() || 'Seat', instructions: s.instructions, provider: s.provider, model: s.model })),
    ...(stats.on.length && state.pack ? { evidence_pack: state.pack, evidence_files: stats.on.map(f => f.fid) } : {}) })
  useEffect(() => {
    let live = true
    const t = setTimeout(() => { rest('/estimate', { method: 'POST', body: JSON.parse(estBody) }).then(r => { if (live) setEst(r && !r.error ? r : null) }).catch(() => live && setEst(null)) }, 600)
    return () => { live = false; clearTimeout(t) }
  }, [estBody, billTick, priceStamp])
  useEffect(() => { setUnknownOk(false) }, [String(est?.unknown || '')])
  if (maxUsd && est && est.low > maxUsd) problems.push(`Even a short run is estimated at ${usd(est.low)}, above your ${usd(maxUsd)} budget. Raise the budget, or use fewer or cheaper seats.`)
  if (maxUsd && est?.unknown?.length) problems.push(`No price for ${est.unknown.join(', ')}. Open the 💲 budget menu and enter its price, or mark its provider Subscription.`)

  const convene = async () => {
    if (problems.length) { notify('warning', problems[0]); if (!state.question.trim()) qRef.current?.focus(); return }
    setBusy(true); setGate(null)
    try {
      const body = { question: state.question, title: (state.title || '').trim(), evidence: state.evidence, template: state.template, review: state.review, research: !!state.research, research_secs: state.research_secs || 90,
        deliberate: delib, deliberate_rounds: rounds, chairman_guidance: chairGuidance, decision_focus: decision, chairman: state.chairman, max_minutes: maxMin,
        max_usd: maxUsd, allow_unknown_price: unknownOk,
        seats: state.seats.map(s => ({ name: s.name.trim(), instructions: s.instructions.trim(), provider: s.provider, model: s.model,
          ...(typeof s.vision === 'boolean' ? { vision: s.vision } : {}), ...(s.thinking ? { thinking: s.thinking } : {}) })),
        ...(stats.on.length && state.pack ? { evidence_pack: state.pack, evidence_files: stats.on.map(f => f.fid) } : {}) }
      const res = await startCommittee(body, body.title || state.question.trim().slice(0, 60))
      if (res.gate) { setGate(res.gate); return }
      if (res.where === 'typed' || res.where === 'copied') return
      CTX.storage.set('setup', { ...state, question: '', title: '', evidence: '', files: [], pack: null })
      onConvened()
    } catch (e) {
      notify('error', `Could not convene: ${e.message || e}`)
    } finally { setBusy(false) }
  }

  const savePreset = async name => {
    name = (name || '').trim()
    if (!COMMITTEE_NAME.test(name)) { notify('warning', 'Give the committee a name: up to 40 characters, starting with a letter or digit.'); return }
    try {
      const preset = { template: state.template, review: state.review, research: !!state.research, research_secs: state.research_secs || 90, deliberate: delib, deliberate_rounds: rounds,
        ...(customBrief ? { chairman_guidance: chairGuidance, decision_focus: decision } : {}), max_minutes: maxMin, max_usd: maxUsd, chairman: { ...clean(state.chairman), ...(state.chairman.thinking ? { thinking: state.chairman.thinking } : {}) },
        seats: state.seats.map(s => ({ name: s.name.trim(), instructions: s.instructions.trim(), ...clean(s), ...(typeof s.vision === 'boolean' ? { vision: s.vision } : {}), ...(s.thinking ? { thinking: s.thinking } : {}) })) }
      const r = await rest('/presets', { method: 'POST', body: { name, preset } })
      if (!r || r.ok === false) throw new Error(r?.error || 'The preset was not saved')
      notify('success', `Saved committee “${name}”. Pick it under “My committees” next time; the question and evidence are yours to add each time.`)
      setSaveAs(null); upd({ source: name })
      queryClient.invalidateQueries({ queryKey: [...KEY, 'setup'] })
    } catch (e) { notify('error', `Save failed: ${e.message || e}`) }
  }
  const isPreset = !!setup.rosters?.[state.source]
  useEffect(() => { setConfirmDel(false) }, [state.source])
  const deletePreset = async () => {
    if (!confirmDel) { setConfirmDel(true); setTimeout(() => setConfirmDel(false), 4000); return }
    const name = state.source
    try {
      const r = await rest(`/presets/${encodeURIComponent(name)}`, { method: 'DELETE' })
      if (!r || r.ok === false) throw new Error(r?.error || 'not deleted')
      notify('success', `Deleted committee “${name}”. Your current seats stay on the page.`)
      upd({ source: state.template }); setConfirmDel(false)
      queryClient.invalidateQueries({ queryKey: [...KEY, 'setup'] })
    } catch (e) { notify('error', `Delete failed: ${e.message || e}`) }
  }

  const billOf = s => {
    const r = (est?.providers || []).find(x => (x.who || []).includes(s === state.chairman ? 'Chairman' : (s.name || '').trim()))
    if (!r) return ['—', '']
    return r.billing === 'subscription' ? ['Subscription', 'hc-sub'] : r.unpriced?.length ? ['No price', 'hc-warn-t'] : ['Pay per use', '']
  }
  const tplDesc = setup.rosters?.[state.source] ? 'Your saved committee. Change any seat below; save again to keep the changes.' : (tplNow.description || '')
  const warns = [
    maxUsd && est && est.high > maxUsd && est.low <= maxUsd ? ['Long answers would pass the budget: they may be shortened, then the vote skipped.', 'The run stays within the budget'] : null,
    maxMin && minutes && minutes > maxMin ? [`Usually takes ${minutes} min: slow seats or the vote may be cut.`, 'Raise the time limit to keep every stage'] : null,
    state.research && maxMin && maxMin < (minutes || 3) + 3 ? [`Research first needs about a minute: with a ${maxMin}-min limit it may be cut short or skipped.`, `${Math.max(8, (minutes || 3) + 3)} min or more is safer`] : null,
    sameModel ? ['All seats use the same model: the views will be less independent.', ''] : null,
    ...(est?.context_warnings || []).map(w => [`⚠ ${w}`, '']),
    lockedNow.length ? [`🔒 ${lockedNow.length} provider${lockedNow.length > 1 ? 's' : ''} need your OK once: Convene shows the command.`, ''] : null,
  ].filter(Boolean)
  const web = state.research ? 2 : 0
  const calls = `${plannedCalls(n, state.review) + stats.pics + web}${delib !== 'never' ? `–${plannedCalls(n, state.review) + stats.pics + web + debateCalls(n, delib, rounds)}` : ''}`
  const emax = est ? Math.max(maxUsd || 0, est.high, 0.01) * 1.15 : 1
  const nModels = n + 1

  const seatRow = (s, i) => {
    const open = sel === i
    const [bill, bcls] = billOf(s)
    return [h('tr', { key: `s${i}`, className: `hc-seatrow${open ? ' hc-open' : ''}`, onClick: () => setSel(open ? null : i) },
      h('td', null, h('span', { className: 'hc-av2' }, initials(s.name || '?')), h('span', { dir: 'auto' }, s.name || 'Unnamed seat'), h('span', { className: 'hc-pen' }, '✎'),
        h('span', { className: `hc-ins${s.instructions ? '' : ' hc-none'}`, dir: 'auto' }, s.instructions || 'No instructions: answers in its own way')),
      h('td', null, h('span', { className: 'hc-pill', title: s.provider || 'your active Hermes model' }, h('span', { className: 'hc-dot', style: { background: pcolor(s.provider) } }), fit(shortModel(s.model) || 'active model', 22), ' ▾'),
        locked(s.provider) ? h('div', { className: 'hc-xs hc-warn-t' }, '🔒 needs your OK') : stats.pics ? h('div', { className: 'hc-xs hc-muted' }, seesPhotos(s, caps) ? '👁 tries the photo' : '📝 gets descriptions') : null),
      h('td', { className: `hc-bill ${bcls}` }, bill)),
    open ? h('tr', { key: `e${i}`, className: 'hc-edit' }, h('td', { colSpan: 3 }, h(InlineEditor, {
      slot: s, index: i, n, limits: L, models, locked, caps, pics: stats.pics,
      onChange: patch => updSeat(i, patch),
      onRemove: () => { upd({ seats: state.seats.filter((_, j) => j !== i) }); setSel(null) },
      onDuplicate: () => { if (n >= L.max_seats) return; const x = [...state.seats]; x.splice(i + 1, 0, { ...state.seats[i], name: `${state.seats[i].name} 2` }); upd({ seats: x }); setSel(i + 1) },
      onClose: () => setSel(null) }))) : null]
  }
  const chairOpen = sel === 'chair'
  const [cbill, ccls] = billOf(state.chairman)
  const chairRows = [h('tr', { key: 'chair', className: `hc-seatrow${chairOpen ? ' hc-open' : ''}`, onClick: () => setSel(chairOpen ? null : 'chair') },
    h('td', null, h('span', { className: 'hc-av2 hc-av-chair' }, '⚖'), h('b', null, 'Chairman'), h('span', { className: 'hc-pen' }, '✎'),
      h('span', { className: 'hc-ins', dir: 'auto' }, `${customBrief ? 'Custom brief · ' : ''}${decision ? 'Decision' : 'Answer'} · ${chairGuidance || 'no instructions'}`)),
    h('td', null, h('span', { className: 'hc-pill' }, h('span', { className: 'hc-dot', style: { background: pcolor(state.chairman.provider) } }), fit(shortModel(state.chairman.model) || 'active model', 22), ' ▾')),
    h('td', { className: `hc-bill ${ccls}` }, cbill)),
  chairOpen ? h('tr', { key: 'chairE', className: 'hc-edit' }, h('td', { colSpan: 3 }, h('div', { className: 'hc-editor' },
    h('div', null, h('label', { className: 'hc-elbl' }, 'AI model'), h(ModelPicker, { slot: state.chairman, models, locked, onPick: patch => upd({ chairman: { ...state.chairman, ...patch } }) }),
      locked(state.chairman.provider) ? h('div', { className: 'hc-xs hc-warn-t', style: { marginTop: 6 } }, '🔒 This provider needs your OK once. Convene will show you the command.') : null),
    stats.pics ? h(PhotoChoice, { slot: state.chairman, caps, onChange: patch => upd({ chairman: { ...state.chairman, ...patch } }) }) : null,
    h(ThinkingChoice, { slot: state.chairman, chair: true, caps, onChange: patch => upd({ chairman: { ...state.chairman, ...patch } }) }),
    h(ChairBrief, { guidance: chairGuidance, decision, tplNow, customBrief, onBrief: patch => upd(patch) }),
    h('div', { className: 'hc-hint' }, 'The chairman reads every answer, the blind critiques and any debate, then writes the verdict, keeps the dissent and lists next actions. ', h('b', null, 'When to use a strong model here: '), 'always; this one call shapes the result most.'),
    h('div', { className: 'hc-efoot' }, h('span', { className: 'hc-grow' }), h('button', { className: 'hc-btn2', onClick: () => setSel(null) }, 'Done'))))) : null]

  return h(Fragment, null,
    guide ? h(Guide, { onClose: () => { setGuide(false); CTX.storage.set('guideHidden', true) } }) : null,
    h('div', { className: 'hc-brief' },
      h('div', { style: { minWidth: 0 } },
        h('div', { className: 'hc-kicker' }, 'Step 1 · The brief'),
        h('h1', { className: 'hc-h1' }, 'What should the committee decide?'),
        h('textarea', { ref: qRef, className: 'hc-bq', rows: 4, dir: 'auto', value: state.question, onChange: e => upd({ question: e.target.value }),
          placeholder: 'e.g. Our conveyor gearbox shows rising oil debris 11 weeks before the planned outage. Run to the outage, or change it out now?' }),
        h('div', { className: 'hc-qmeta' }, h('input', { className: 'hc-in hc-namein', dir: 'auto', maxLength: 80, value: state.title || '', onChange: e => upd({ title: e.target.value }),
          placeholder: 'Committee name (optional) · shown in History and on the report' }), h('span', { className: 'hc-mono' }, `${state.question.length} chars`)),
        h('div', { className: 'hc-panel', style: { marginTop: 12 } },
          h('div', { className: 'hc-ph' }, `Evidence${stats.on.length ? ` · ${stats.on.length} file${stats.on.length > 1 ? 's' : ''}` : ''}`, h('span', { className: 'hc-phr' }, 'optional')),
          h('div', { className: 'hc-evwrap' }, h(EvidencePanel, { state, upd, caps, stats })),
          stats.on.length || state.evidence?.trim() ? h('div', { className: 'hc-share' }, '⚠', h('span', null, `All ${nModels} models on this committee receive this evidence. Attach only what every participant may see.`)) : null)),
      h('div', { className: 'hc-panel', style: { minWidth: 0 } },
        h('div', { className: 'hc-ph' }, 'Step 2 · Your committee', h('span', { className: 'hc-phr' }, h('button', { className: 'hc-link hc-tint', onClick: openHelp }, '? How does this work'))),
        h('div', { className: 'hc-pb' },
          h('div', { className: 'hc-types' },
            setup.templates.map(t => h('button', { key: t.id, className: `hc-chip${state.source === t.id ? ' hc-on' : ''}`, onClick: () => pickTemplate(t), title: t.description }, t.title)),
            Object.keys(setup.rosters || {}).map(p => h('button', { key: p, className: `hc-chip${state.source === p ? ' hc-on' : ''}`, onClick: () => pickPreset(p), title: 'Your saved committee' }, `★ ${p}`))),
          h('div', { className: 'hc-rosterhead' },
            h(MiniArc, { seats: state.seats.map((s, i) => ({ key: i, name: s.name || '?', title: `${s.name || 'Unnamed seat'} · click to edit` })), selected: sel, onSelect: k => setSel(sel === k ? null : k) }),
            h('div', { className: 'hc-xs hc-muted' }, tplDesc,
              h('div', { style: { marginTop: 6 } }, `${new Set(state.seats.map(s => s.provider || 'active')).size} provider(s) across ${n} seats. Different models give more independent views.`))),
          h('table', { className: 'hc-tbl hc-roster2' },
            h('thead', null, h('tr', null, h('th', null, 'Seat · click to edit role, model and instructions'), h('th', null, 'Model'), h('th', null, 'Billing'))),
            h('tbody', null, state.seats.map(seatRow), chairRows,
              n < L.max_seats ? h('tr', { className: 'hc-addrow', onClick: addSeat }, h('td', { colSpan: 3 }, `＋ Add a seat (${n} of ${L.max_seats})`)) : null)),
          h('div', { className: 'hc-runrow' },
            h('div', { className: 'hc-kv2' }, h('div', null, 'Blind vote', h('small', null, 'Seats rank each other’s answers anonymously. Off = quicker, cheaper.')),
              h('button', { className: `hc-sw${state.review ? ' hc-on' : ''}`, role: 'switch', 'aria-checked': !!state.review, title: 'Blind vote', onClick: () => upd({ review: !state.review }) })),
            h('div', { className: 'hc-kv2' }, h('div', null, 'Deliberation', h('small', null, 'Seats debate the points they disagree on')),
              h(Deliberate, { value: delib, rounds, n, onChange: patch => upd(patch) })),
            h('div', { className: 'hc-kv2' }, h('div', null, 'Time limit', h('small', null, 'The chairman’s time is kept back')),
              h(TimeLimit, { value: maxMin, max: L.max_minutes_max || 60, typical: minutes, onChange: v => upd({ maxMinutes: v }) })),
            h('div', { className: 'hc-kv2' }, h('div', null, 'Budget', h('small', null, 'Subscription seats count $0')),
              h(Budget, { value: maxUsd, max: L.max_usd_max || 500, est, onChange: v => upd({ maxUsd: v }),
                onBilling: (provider, billing) => rest('/billing', { method: 'POST', body: { provider, billing } }).then(() => setBillTick(t => t + 1)).catch(() => {}),
                onPrice: (provider, model, pin, pout) => rest('/price', { method: 'POST', body: { provider, model, in: pin, out: pout } }).then(() => setBillTick(t => t + 1)).catch(e => notify('error', String(e?.message || e))) }))),
          est && est.high > 0 ? h(Fragment, null,
            h('div', { className: 'hc-xs hc-muted', style: { marginTop: 14 } }, maxUsd ? 'Estimated spend against your budget' : 'Estimated spend (no budget set)'),
            h('div', { className: 'hc-estbar', title: `Typical run ${usd(est.low)}; at most ${usd(est.high)} if every model writes its longest answer.` },
              h('div', { className: 'hc-rng', style: { left: `${(est.low / emax) * 100}%`, width: `${Math.max(0.6, ((est.high - est.low) / emax) * 100)}%` } }),
              maxUsd ? h('div', { className: 'hc-cap', style: { left: `${(maxUsd / emax) * 100}%` } }) : null))
            : est && est.subscription?.length && !est.unknown?.length ? h('div', { className: 'hc-xs hc-muted', style: { marginTop: 14 } }, 'Every seat is on a subscription: $0 counts toward the budget.') : null,
          warns.length ? h('div', { className: 'hc-warns' }, warns.map(([w, t], i) => h('div', { key: i, className: 'hc-xs hc-warn-t', title: t }, w))) : null,
          gate ? h(PermissionNotice, { gate, onClose: () => setGate(null) }) : null,
          h('div', { className: 'hc-go' },
            h('div', { className: 'hc-xs hc-muted hc-mono' }, `${n} seats · ${calls} model calls${minutes ? ` · about ${minutes} min` : ''}`,
              est ? h('br') : null, est ? (est.high > 0 ? `${usd(est.low)}–${usd(est.high)} estimated${est.subscription?.length ? ' · subscription seats $0' : ''}` : '') : null,
              maxUsd && est?.unknown?.length ? h('span', { className: 'hc-warn-t' }, ` · 💲 no price: ${est.unknown.join(', ')}`) : null),
            h('span', { className: 'hc-grow' }),
            saveAs !== null ? h('span', { className: 'hc-row' },
              h('input', { className: 'hc-in', style: { width: 170 }, autoFocus: true, value: saveAs, placeholder: 'e.g. Pump reliability board', maxLength: 40, dir: 'auto', onChange: e => setSaveAs(e.target.value), onKeyDown: e => { if (e.key === 'Enter') savePreset(saveAs); if (e.key === 'Escape') setSaveAs(null) } }),
              h(Button, { size: 'sm', variant: 'outline', onClick: () => savePreset(saveAs) }, 'Save'), h(Button, { size: 'sm', variant: 'ghost', onClick: () => setSaveAs(null) }, 'Cancel'))
              : h('button', { className: 'hc-btn2', onClick: () => setSaveAs(setup.rosters?.[state.source] ? state.source : ''),
                title: 'Save these seats, models, instructions, photo settings, chairman, blind vote, time limit and budget. Reuse them with any question and evidence.' }, '💾 Save committee'),
            isPreset && saveAs === null ? h('button', { className: `hc-btn2${confirmDel ? ' hc-danger' : ''}`, onClick: deletePreset,
              title: setup.rosters[state.source]?.source === 'config' ? 'This committee is in config.yaml: it is hidden here, config.yaml is not changed' : 'Delete this saved committee' },
              confirmDel ? `Click again to delete “${state.source}”` : '🗑') : null,
            h('button', { className: `hc-convene hc-convene-lg${busy ? ' hc-busy' : ''}`, disabled: busy, onClick: convene, title: problems[0] || 'Start the committee in its own chat' }, busy ? busyLabel('Convening…') : ['Convene the committee', h('span', { key: 'i', className: 'hc-bic' }, '▶')])),
          problems.length && state.question.trim() ? h('div', { className: 'hc-xs hc-warn-t', style: { textAlign: 'right', marginTop: 6 } }, problems[0]) : null))))
}

function InlineEditor({ slot, index, n, limits, models, locked, caps, pics, onChange, onRemove, onDuplicate, onClose }) {
  return h('div', { className: 'hc-editor' },
    h('div', { className: 'hc-row2' },
      h('div', null, h('label', { className: 'hc-elbl' }, `Seat ${String.fromCharCode(65 + index)} · role`),
        h('input', { className: 'hc-in', autoFocus: !slot.name, dir: 'auto', value: slot.name, onChange: e => onChange({ name: e.target.value.slice(0, 60) }), placeholder: 'e.g. Mechanical Engineer' })),
      h('div', null, h('label', { className: 'hc-elbl' }, 'AI model'), h(ModelPicker, { slot, models, locked, onPick: onChange }))),
    locked(slot.provider) ? h('div', { className: 'hc-xs hc-warn-t' }, '🔒 This provider needs your OK once. Convene will show you the command.') : null,
    pics ? h(PhotoChoice, { slot, caps, onChange }) : null,
    h(ThinkingChoice, { slot, caps, onChange }),
    h('div', null,
      h('label', { className: 'hc-elbl' }, 'Instructions', h('span', { className: `hc-cnt${slot.instructions.length > MAX_INSTR ? ' hc-bad' : ''}` }, `${slot.instructions.length}/${MAX_INSTR}`)),
      h('textarea', { className: 'hc-in', rows: 4, dir: 'auto', value: slot.instructions, onChange: e => onChange({ instructions: e.target.value }), autoFocus: !!slot.name,
        placeholder: 'What role should this seat play and what should it focus on? e.g. Judge only from the vibration and leak data.' }),
      h('div', { className: 'hc-starters' }, STARTERS.map(t => h('button', { key: t, className: 'hc-chip hc-chip-sm',
        onClick: () => onChange({ instructions: `${slot.instructions.trim()}${slot.instructions.trim() ? ' ' : ''}${t}.`.slice(0, MAX_INSTR) }) }, `+ ${t}`)))),
    h('div', { className: 'hc-hint' }, 'Tells this seat which role to play and what to look at. ', h('b', null, 'When to use it: '),
      'give each seat a different angle so the answers don’t overlap. Leave it empty for the model’s own view.'),
    h('div', { className: 'hc-efoot' },
      h('button', { className: 'hc-link', disabled: n >= limits.max_seats, onClick: onDuplicate }, '⧉ Duplicate'),
      h('button', { className: 'hc-link hc-bad', disabled: n <= limits.min_seats, onClick: onRemove }, '✕ Remove seat'),
      h('span', { className: 'hc-grow' }), h('button', { className: 'hc-btn2', onClick: onClose }, 'Done')))
}

// How long the model thinks before it answers (a reasoning level). Medium unless changed; the clerical
// steps (searches, passages, photo notes, the blind vote, listing disputes, debate replies) always run on low.
function ThinkingChoice({ slot, chair, caps, onChange }) {
  const refused = caps?.[`${slot.provider || ''}|${slot.model || ''}`]?.thinking === false
  const opts = [['low', 'Low'], [undefined, 'Medium'], ['high', 'High'], ['default', 'Model default']]
  return h('div', null, h('div', { className: 'hc-flbl' }, chair ? 'Thinking for the verdict' : 'Thinking for the answer'),
    h('div', { className: 'hc-row hc-wrap' }, opts.map(([v, label]) => h('button', { key: String(v),
      className: `hc-chip hc-chip-sm${slot.thinking === v ? ' hc-on' : ''}`, onClick: () => onChange({ thinking: v }) }, label))),
    h('div', { className: 'hc-xs hc-muted', style: { marginTop: 4 } },
      refused ? 'This model refused a thinking level before, so it runs on its own default whatever you pick. '
        : 'How long the model reasons before it writes. Higher can be sharper but is slower and costs more; some models ignore it. ',
      chair ? 'Picking sources and listing the disputes always run on Low.' : 'The blind vote and debate replies always run on Low.'))
}

function PhotoChoice({ slot, caps, onChange }) {
  const cat = caps?.[`${slot.provider || ''}|${slot.model || ''}`]?.vision
  const auto = cat === false ? 'refused a photo before: gets descriptions' : 'tries the photo'
  const opts = [[undefined, `Auto (${auto})`], [true, 'Can see photos'], [false, 'Descriptions only']]
  return h('div', null, h('div', { className: 'hc-flbl' }, 'Photos'),
    h('div', { className: 'hc-row hc-wrap' }, opts.map(([v, label]) => h('button', { key: String(v),
      className: `hc-chip hc-chip-sm${slot.vision === v ? ' hc-on' : ''}`, onClick: () => onChange({ vision: v }) }, label))),
    h('div', { className: 'hc-xs hc-muted', style: { marginTop: 4 } }, 'Auto sends the photo and falls back to the description if this model refuses it. Choose “Descriptions only” to save tokens on a model you know is text-only.'))
}

function ChairBrief({ guidance, decision, tplNow, customBrief, onBrief }) {
  return h(Fragment, null,
    h('div', null,
      h('div', { className: 'hc-row' }, h('div', { className: 'hc-flbl' }, 'Instructions for the verdict'), h('span', { className: 'hc-grow' }),
        h('span', { className: `hc-xs ${guidance.length > MAX_INSTR ? 'hc-bad' : 'hc-muted'}` }, `${guidance.length}/${MAX_INSTR}`)),
      h('textarea', { className: 'hc-in', rows: 5, value: guidance, dir: 'auto', onChange: e => onBrief({ chairGuidance: e.target.value }),
        placeholder: 'What must the verdict contain? e.g. Rank the options by total cost, name the fatal risks, give owners for each action.' }),
      h('div', { className: 'hc-row hc-xs hc-muted', style: { marginTop: 4 } },
        h('span', { className: 'hc-grow' }, customBrief ? `Changed from the ${tplNow.title || 'type'}’s brief.` : `The ${tplNow.title || 'type'}’s brief.`),
        customBrief ? h('button', { className: 'hc-link', onClick: () => onBrief({ chairGuidance: tplNow.chairman_guidance, decision: !!tplNow.decision_focus }) }, '↺ Reset to the type’s brief') : null)),
    h('div', null, h('div', { className: 'hc-flbl' }, 'The committee gives'),
      h('div', { className: 'hc-row hc-wrap' },
        [[false, 'An answer'], [true, 'A decision']].map(([v, label]) => h('button', { key: label, className: `hc-chip hc-chip-sm${decision === v ? ' hc-on' : ''}`, onClick: () => onBrief({ decision: v }) }, label))),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 4 } }, decision
        ? 'Every seat must end with a clear recommendation, and the verdict is one decision. When to use it: choosing between options, go or no-go, repair or replace.'
        : 'Seats answer the question in their own way, and the verdict is one combined answer. When to use it: “how”, “why” or “what is best practice” questions.')))
}

function PermissionNotice({ gate, onClose }) {
  const cmds = gate.commands || []
  return h('div', { className: 'hc-card hc-gate' },
    h('div', { className: 'hc-row' }, h('b', null, '🔒 One-time permission needed'), h('span', { className: 'hc-grow' }), h('button', { className: 'hc-link', onClick: onClose }, '✕')),
    h('div', { className: 'hc-sm', style: { margin: '6px 0 8px' } }, gate.error,
      ` Hermes keeps this permission in your settings, and the committee never changes it by itself. Run ${cmds.length > 1 ? 'these commands' : 'this command'} once in a terminal, then press Convene again (no restart needed):`),
    cmds.map(c => h('div', { key: c, className: 'hc-cmd' }, h('code', null, c),
      h(Button, { size: 'sm', variant: 'outline', onClick: async () => { await CTX.os.writeClipboard(c); notify('success', 'Command copied. Paste it into a terminal.') } }, 'Copy'))),
    h('div', { className: 'hc-xs hc-muted', style: { marginTop: 6 } }, 'Or pick a model from a provider without 🔒.'))
}

// ── run screen (live and verdict) ──────────────────────────────────────────

// ── run screen: Convene (live / replay) and Decision ───────────────────────

function RunView({ runId, page, setPage, openHelp, onConvened }) {
  const q = useRun(runId)
  const run = q.data
  const [replay, setReplay] = useState(null)
  const [stageKey, setStageKey] = useState(0)
  const [stopping, setStopping] = useState(false)
  const [follow, setFollow] = useState(false)
  const live = isLive(run)
  const now = useNow(live || !!replay, replay ? 100 : 500)
  useEffect(() => { setReplay(null); setStopping(false) }, [runId])
  const end = run ? runEnd(run) : 1
  const t = !run ? 0 : replay ? ((now - replay.t0) / 1000) * REPLAY_SPEED : live ? Math.max(0, now / 1000 - run.created) : end
  useEffect(() => { if (replay && t > end + 2) setReplay(null) })
  if (q.isError) return h(ErrorState, { title: 'Could not load this run', description: String(q.error?.message || q.error) })
  if (!run) return h(Skeleton, { className: 'hc-skel' })

  const sn = snap(run, t, !!replay)
  const stop = async () => {
    setStopping(true)
    try { const r = await rest(`/runs/${run.id}/stop`, { method: 'POST' }); notify('info', r.note || 'Stop requested.'); queryClient.invalidateQueries({ queryKey: KEY }) }
    catch (e) { notify('error', `Stop failed: ${e.message || e}`); setStopping(false) }
  }
  const openChat = () => {
    if (run.session_id && typeof host.openSession === 'function') host.openSession(run.session_id).catch(() => notify('warning', 'That chat is no longer available.'))
  }
  const startReplay = () => { setPage('convene'); setStageKey(k => k + 1); setReplay({ t0: Date.now() }) }
  const followUp = follow ? h(FollowUpDialog, { run, onClose: () => setFollow(false), onStarted: () => { setFollow(false); onConvened?.() } }) : null
  if (page === 'decision' && run.status === 'done' && !replay) {
    return h(Fragment, null, h(DecisionView, { run, onReplay: startReplay, openChat, onFollow: () => setFollow(true), openHelp }), followUp)
  }
  const n = (run.seats || []).length
  return h(Fragment, null,
    h('div', { className: 'hc-kicker' }, replay ? `Replay at ${REPLAY_SPEED}× · ` : live ? 'In session · ' : run.status === 'done' ? 'Finished · ' : `${run.stale ? 'Abandoned' : run.status} · `,
      `${run.template_title} · ${n} seats + chairman · ${ago(run.created)}`),
    h('div', { className: 'hc-recaprow' },
      h('div', { className: 'hc-grow', style: { minWidth: 0 } },
        run.title ? h('div', { className: 'hc-runtitle', dir: 'auto' }, run.follow_of ? `↪ ${run.title}` : run.title) : null,
        h('div', { className: 'hc-recap', dir: 'auto' }, `“${run.question}”`)),
      run.session_id ? h(Button, { size: 'sm', variant: 'ghost', onClick: openChat, title: 'Open the chat this committee runs in' }, '💬 Open the chat') : null),
    h('div', { className: 'hc-conv' },
      h('div', { className: 'hc-col' },
        h(StagesPanel, { run, sn, live, replay, stopping: stopping || run.cancel_requested, onStop: stop, onReplay: startReplay, onEndReplay: () => setReplay(null) }),
        h(NowPanel, { run, sn, onDecision: () => setPage('decision') })),
      h('div', { className: 'hc-col', style: { minWidth: 0 } },
        h(ArcStage, { key: stageKey, run, sn }),
        h(Timeline, { run, sn })),
      h('div', { className: 'hc-col hc-col-r' },
        h(LogPanel, { run, sn }),
        h(MatrixPanel, { run, sn }))),
    followUp)
}

const panel = (title, right, ...body) => h('div', { className: 'hc-panel' },
  h('div', { className: 'hc-ph' }, title, right != null ? h('span', { className: 'hc-phr' }, right) : null), ...body)

function Tile({ label, value, frac, color, note, title }) {
  return h('div', { className: 'hc-tile', title },
    h('span', { className: 'hc-tl' }, label),
    h('b', { className: 'hc-tv' }, value),
    h('div', { className: `hc-tbar${frac == null ? ' hc-tbar-off' : ''}` }, frac == null ? null : h('i', { style: { width: `${Math.round(Math.max(0, Math.min(1, frac)) * 100)}%`, background: color } })),
    h('span', { className: 'hc-tn' }, note))
}

// ── running short: extend the time limit or budget during the run ──
// Early (70% used): a box, no pause; "Not now" only hides it. When a stage would be skipped or cut, the
// run pauses (run.live.ask) up to 45 s for an answer; paused time does not count; no answer = decline.
// Only runs convened from this page (run.draft) can be extended; a chat run carries on as before.
const EXT_TOP = { time: 3600, usd: 500 }
function LimitAlert({ run, sn }) {
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState({})
  const [hidden, setHidden] = useState({})
  if (!run.draft || !sn.live) return null
  const lim = run.limits || {}
  const asks = run.limit_asks || {}
  const ask = run.live?.ask
  const tot = sn.totals || {}
  const nowAbs = (run.created || 0) + sn.t
  const left = run.deadline ? run.deadline - nowAbs : null
  const allSub = tot.calls > 0 && tot.subscription_calls === tot.calls
  let kind = null, mode = 'early', title = '', detail = ''
  if (ask && !sent[`ask-${ask.since}`]) {
    kind = ask.kind; mode = 'ask'; title = `Paused · ${ask.why}`
    detail = `Continues within the current limit in ${Math.max(0, Math.ceil(ask.until - nowAbs))} s. Paused time does not count.`
  } else if (!ask && lim.max_secs && left != null && left <= lim.max_secs * 0.3 && !asks.time && !sent.time && !hidden.time) {
    kind = 'time'; title = `Time is running short · ${clock(Math.max(0, left))} left`
    detail = 'Extend now and nothing has to be skipped. Otherwise the committee asks again before it skips a stage.'
  } else if (!ask && lim.max_usd && !allSub && (tot.cost_usd || 0) >= lim.max_usd * 0.7 && !asks.usd && !sent.usd && !hidden.usd) {
    kind = 'usd'; title = `Budget running short · ${usd(tot.cost_usd || 0)} of ${usd(lim.max_usd)} used`
    detail = 'Extend now and nothing has to be shortened or skipped. Otherwise the committee asks again first.'
  }
  if (!kind) return ask && sent[`ask-${ask.since}`] ? h('div', { className: 'hc-alert hc-alert-sent' }, 'Sent · the committee continues in a moment…') : null
  const room = EXT_TOP[kind] - (kind === 'time' ? lim.max_secs || 0 : lim.max_usd || 0)
  const need = mode === 'ask' ? ask.need || 0 : 0
  let opts = kind === 'time' ? [300, 600] : [1, 2]
  if (kind === 'time' && need > 600) opts = [Math.ceil(need / 300) * 300, Math.ceil(need / 300) * 300 + 300]
  if (kind === 'usd' && need > 2) opts = [Math.ceil(need), Math.ceil(need) * 2]
  opts = [...new Set(opts.map(v => Math.min(v, room)))].filter(v => v > 0)
  const label = v => (kind === 'time' ? `+${Math.round(v / 60)} min` : `+${usd(v)}`)
  const answer = async body => {
    setBusy(true)
    try {
      const r = await rest(`/runs/${run.id}/extend`, { method: 'POST', body: { kind, ...body } })
      if (r.ok === false) notify('warning', r.note || 'The run has already finished.')
      setSent(x => ({ ...x, [mode === 'ask' ? `ask-${ask.since}` : kind]: true }))
      queryClient.invalidateQueries({ queryKey: [...KEY, 'run', run.id] })
    } catch (e) { notify('error', `Could not send: ${e.message || e}`) }
    finally { setBusy(false) }
  }
  return h('div', { className: `hc-alert${mode === 'ask' ? ' hc-alert-ask' : ''}`, role: 'alert' },
    h('b', null, '⚠ ', title),
    h('div', { className: 'hc-xs' }, detail),
    need > 0 ? h('div', { className: 'hc-xs hc-muted' }, kind === 'time' ? `Needs about ${Math.max(1, Math.ceil(need / 60))} more min.` : `Needs up to ${usd(need)} more.`) : null,
    h('div', { className: 'hc-alert-btns' },
      opts.map(v => h('button', { key: v, className: 'hc-btn2', disabled: busy, onClick: () => answer({ add: v }),
        title: kind === 'time' ? 'Adds to the time limit for this run' : 'Adds to the budget for this run (an estimate, not your provider’s bill)' }, label(v))),
      !opts.length ? h('span', { className: 'hc-xs hc-muted' }, `Already at the maximum (${kind === 'time' ? '60 min' : '$500'}).`) : null,
      mode === 'ask'
        ? h('button', { className: 'hc-link', disabled: busy, onClick: () => answer({ decline: true }), title: 'Continue within the current limit; the committee will not ask about it again' }, 'Decline')
        : h('button', { className: 'hc-link', onClick: () => setHidden(x => ({ ...x, [kind]: true })), title: 'Hide this. The committee still asks before it skips anything.' }, 'Not now')))
}

// After Research first, on a page run: the sources found, all ticked; untick any, then continue.
function SourcesReview({ run, sn }) {
  const ask = run.live?.sources
  const items = run.web?.items || []
  const [off, setOff] = useState({})
  const [sent, setSent] = useState(null)
  const [busy, setBusy] = useState(false)
  if (!run.draft || !sn.live || !ask || !items.length) return null
  if (sent === ask.since) return h('div', { className: 'hc-alert hc-alert-sent' }, 'Sent · the seats start in a moment…')
  const nowAbs = (run.created || 0) + sn.t
  const keep = items.filter(x => !off[x.id]).map(x => x.id)
  const go = async ids => {
    setBusy(true)
    try {
      const r = await rest(`/runs/${run.id}/sources`, { method: 'POST', body: { keep: ids } })
      if (r.ok === false) notify('warning', r.note || 'The run is no longer waiting.')
      setSent(ask.since)
      queryClient.invalidateQueries({ queryKey: [...KEY, 'run', run.id] })
    } catch (e) { notify('error', `Could not send: ${e.message || e}`) }
    finally { setBusy(false) }
  }
  return h('div', { className: 'hc-alert hc-alert-src', role: 'dialog', 'aria-label': 'Review the web sources' },
    h('b', null, `🌐 ${items.length} web source${items.length === 1 ? '' : 's'} found · review before the seats start`),
    h('div', { className: 'hc-xs' }, `Untick anything you do not trust. Continues with every ticked source in ${Math.max(0, Math.ceil(ask.until - nowAbs))} s; paused time does not count.`),
    h('div', { className: 'hc-srclist' }, items.map(x => h('label', { key: x.id, className: `hc-src${off[x.id] ? ' hc-src-off' : ''}` },
      h('input', { type: 'checkbox', checked: !off[x.id], onChange: () => setOff(o => ({ ...o, [x.id]: !o[x.id] })) }),
      h('div', { style: { minWidth: 0 } },
        h('div', { className: 'hc-row', style: { gap: 6 } }, h('b', { className: 'hc-mono hc-xs' }, x.id), h('span', { className: 'hc-ellipsis hc-sm', title: x.title }, x.title)),
        h('div', { className: 'hc-xs hc-muted hc-ellipsis' }, h('a', { href: x.url, target: '_blank', rel: 'noreferrer', title: x.url }, x.site), x.why ? ` · ${x.why}` : ''),
        h('div', { className: 'hc-xs hc-srcq', dir: 'auto' }, `“${fit(x.excerpt, 200)}”`))))),
    h('div', { className: 'hc-alert-btns' },
      h('button', { className: 'hc-btn2', disabled: busy, onClick: () => go(keep) }, keep.length === items.length ? 'Continue with all' : `Continue with ${keep.length}`),
      h('span', { className: 'hc-xs hc-muted' }, 'Web text is marked unverified for the seats.')))
}

function StagesPanel({ run, sn, live, replay, stopping, onStop, onReplay, onEndReplay }) {
  const n = (run.seats || []).length
  const dl = run.deliberation || {}
  const delibOn = run.deliberate && run.deliberate !== 'never'
  const answered = Object.values(sn.seats).filter(s => s.a === 'ok').length
  const order = [...(run.research ? ['research'] : []), 'stage1', 'review', ...(delibOn ? ['deliberate'] : []), 'chair']
  const cur = sn.stage === 'done' || sn.stage === 'stopped' ? order.length : Math.max(0, order.indexOf(sn.stage))
  const skippedReview = run.skipped?.review
  const webN = (run.web?.items || []).length
  const items = [
    ...(run.research ? [['Research', sn.stage === 'research' ? (sn.researching === 'review' ? 'review sources' : 'searching')
      : run.web ? (webN ? `${webN} source${webN === 1 ? '' : 's'}` : 'none') : '', sn.stage === 'research' ? 0.5 : 1, !!run.web && !webN && sn.stage !== 'research']] : []),
    ['Independent answers', `${answered}/${n}`, answered / Math.max(1, n)],
    ['Blind vote', !run.review ? 'off' : skippedReview ? `skipped · ${skippedReview}` : cur >= 1 ? `${sn.ballots.length}/${Math.max(sn.okSeats.length, 1)}` : '',
      !run.review || skippedReview ? 1 : sn.ballots.length / Math.max(1, sn.okSeats.length), !run.review || !!skippedReview],
    ...(delibOn ? [['Debate', sn.stage === 'deliberate' ? (sn.chair === 'agenda' ? 'listing points' : `${sn.debaters.length} replying`)
      : dl.rounds?.length && cur > order.indexOf('deliberate') ? `${dl.disputes.length} point${dl.disputes.length === 1 ? '' : 's'}`
        : dl.skipped && cur > order.indexOf('deliberate') ? 'no debate' : '',
    sn.stage === 'deliberate' ? Math.min(0.95, sn.replies / Math.max(1, sn.okSeats.length * (run.deliberate_rounds || 1))) : 1,
    !!dl.skipped && cur > order.indexOf('deliberate')]] : []),
    ['Chairman’s verdict', sn.chair === 'run' ? 'writing' : sn.chair === 'ok' ? 'done' : '', sn.chair === 'ok' ? 1 : 0],
  ]
  const lim = run.limits || {}
  const tot = sn.totals || {}
  const cost = tot.cost_usd || 0
  const allSub = tot.calls > 0 && tot.subscription_calls === tot.calls
  return h('div', { className: 'hc-panel' },
    h('ol', { className: 'hc-stages' }, items.map(([label, cnt, frac, skip], i) => {
      const state = i < cur ? 'done' : i === cur ? 'now' : ''
      return h('li', { key: label, className: `hc-st-${state || 'wait'}${skip ? ' hc-st-skip' : ''}${state === 'now' && sn.stage === 'stopped' ? ' hc-st-stop' : ''}` },
        h('span', { className: 'hc-sdot' }, i < cur ? '✓' : i + 1),
        h('div', { style: { minWidth: 0, flex: 1 } }, label, cnt ? h('span', { className: 'hc-scnt' }, cnt) : null,
          h('div', { className: `hc-sbar${state === 'now' && i === items.length - 1 ? ' hc-indet' : ''}` },
            h('i', { style: { width: `${Math.round((i < cur ? 1 : state === 'now' ? frac : 0) * 100)}%` } }))))
    })),
    h('div', { className: 'hc-gauges' },
      h(Tile, { label: 'Time', value: clock(sn.t), frac: lim.max_secs ? sn.t / (lim.max_secs + (lim.paused || 0)) : null, color: 'var(--hc-acc)',
        note: lim.max_secs ? `of ${clock(lim.max_secs)}${lim.paused >= 1 ? ' + pause' : ''}` : 'no limit',
        title: `${replay ? 'Recorded run time' : 'Time since the committee opened'}${lim.paused >= 1 ? `. ${Math.round(lim.paused)} s spent paused for your answer do not count.` : ''}` }),
      h(Tile, { label: 'Cost', value: allSub ? '$0' : `${usd(cost)}${tot.unknown_calls ? '+' : ''}`, frac: lim.max_usd && !allSub ? cost / lim.max_usd : null,
        color: lim.max_usd && cost > lim.max_usd * 0.8 ? 'var(--hc-warn)' : 'var(--hc-ok)',
        note: allSub ? 'subscription' : lim.max_usd ? `of ${usd(lim.max_usd)}` : 'no budget',
        title: `Estimated from token counts and list prices, updated as each call finishes. Subscription calls count $0.${tot.unknown_calls ? ` ${tot.unknown_calls} call(s) have no known price and are not counted.` : ''}` }),
      h(Tile, { label: 'Calls', value: tot.calls || 0, note: 'finished', title: 'Model calls that have finished' }),
      h(Tile, { label: 'Tokens', value: fmtTokens(tot.tokens || 0), note: 'in + out', title: 'Input and output tokens of the finished calls' })),
    live && !replay ? h(SourcesReview, { run, sn }) : null,
    live && !replay ? h(LimitAlert, { run, sn }) : null,
    h('div', { className: 'hc-ctl' },
      live ? h('button', { className: 'hc-stop', disabled: stopping, onClick: onStop }, stopping ? 'Stopping…' : '⏹ Stop committee') : null,
      replay ? h('button', { className: 'hc-btn2', onClick: onEndReplay }, '⏹ End replay')
        : !live && (run.lanes || []).length ? h('button', { className: 'hc-btn2', onClick: onReplay, title: `Plays the recorded run at ${REPLAY_SPEED}× speed` }, `↺ Replay (${REPLAY_SPEED}×)`) : null),
    live ? h('div', { className: 'hc-xs hc-muted hc-ctlnote' }, 'Tokens and cost update when each call finishes. Stop ends new calls; calls already sent may still finish and be billed.') : null,
    replay ? h('div', { className: 'hc-xs hc-muted hc-ctlnote' }, 'Rebuilt from the saved record: timings, ballots and positions as they happened.') : null)
}

function NowPanel({ run, sn, onDecision }) {
  const n = (run.seats || []).length
  const answered = Object.values(sn.seats).filter(s => s.a === 'ok').length
  const dl = run.deliberation || {}
  const c = run.chair || {}
  const K = t => h('div', { className: 'hc-kicker' }, t)
  const big = (...x) => h('div', { className: 'hc-big', dir: 'auto' }, ...x)
  let body
  if (sn.live && run.live?.ask) {
    body = [K('Paused · waiting for you'), big(run.live.ask.why + '.'), h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } },
      'Extend or decline in the box on the left. No model calls run while it waits.')]
  } else if (sn.stage === 'done') {
    body = [K(`Decided${c.confidence ? ` · ${c.confidence} confidence` : ''}`), big(c.decision || '—'),
      h('button', { className: 'hc-convene hc-see', onClick: onDecision }, 'See the decision →')]
  } else if (sn.stage === 'stopped') {
    body = [K(run.status === 'cancelled' ? 'Stopped' : run.stale ? 'Abandoned' : 'No verdict'),
      h('div', { className: 'hc-sm' }, run.error || (run.status === 'cancelled' ? 'The committee was stopped. No further model calls were started.' : 'The chat that ran this committee ended before it finished.'))]
  } else if (sn.stage === 'research') {
    body = sn.researching === 'review'
      ? [K('Research first · your review'), big('Untick any source you do not trust.', h('br'), h('span', { className: 'hc-xs hc-muted' }, 'The seats start when you continue.'))]
      : [K('Research first'), big('Searching the web once,', h('br'), 'the same facts for every seat.'),
        h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, fit(String(sn.researching || ''), 90))]
  } else if (sn.describing) {
    body = [K('Preparing the evidence'), big('Describing the pictures', h('br'), h('span', { className: 'hc-xs hc-muted' }, 'so seats that cannot see photos get a written description.'))]
  } else if (sn.stage === 'stage1') {
    body = [K('Stage 1 · Independent answers'), big('Each seat answers on its own.', h('br'), h('span', { className: 'hc-xs hc-muted' }, 'Nobody sees anyone else’s answer.')),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, `${answered} of ${n} answered`)]
  } else if (sn.stage === 'review' && !sn.ranked) {
    body = [K('Stage 2 · Blind vote'), big('Names are hidden.', h('br'), 'Each seat ranks the others.'),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, `${sn.ballots.length} of ${sn.okSeats.length} ballots in`)]
  } else if (sn.stage === 'deliberate' && sn.chair === 'agenda') {
    body = [K('Debate · the agenda'), big('The chairman lists where the seats disagree.'),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, 'A debate runs only if a point matches your deliberation setting.')]
  } else if (sn.stage === 'deliberate' && sn.debaters.length) {
    const pts = dl.disputes || []
    body = [K(`Debate${pts.length ? ` on ${pts.map(d => d.id).join(', ')}` : ''}`),
      pts.length ? big(`“${fit(pts[0].question || pts[0].point, 120)}”`) : big('The seats answer the disputed points.'),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, `${sn.debaters.length} seat${sn.debaters.length === 1 ? '' : 's'} replying · each holds or changes its position, with a reason.`)]
  } else if (sn.stage === 'chair' || sn.chair === 'run') {
    const nA = sn.okSeats.length, nR = (run.ranking || []).length ? sn.ballots.length : 0
    body = [K('Chairman'), big(`Weighing ${nA} answer${nA === 1 ? '' : 's'}${nR ? `,`: ''}`, h('br'), nR ? `${nR} ranking${nR === 1 ? '' : 's'}${dl.rounds?.length ? ' and the debate' : ''}.` : dl.rounds?.length ? 'and the debate.' : '')]
  } else if (sn.ranked) {
    const R = run.ranking || []
    const k = Math.max(2, R.length)
    body = [K('Blind ranking'), h('ul', { className: 'hc-ranks' }, R.map((x, i) => h('li', { key: x.label, style: { animationDelay: `${i * 120}ms` } },
      h('b', null, x.label), h('span', { className: 'hc-rname' }, sn.masked ? `Response ${x.label}` : nameOf(run, x.label)),
      h('span', { className: 'hc-rb', style: { width: `${Math.max(6, ((k - x.avg_rank) / (k - 1)) * 90)}px` } }), h('span', { className: 'hc-xs hc-muted hc-mono' }, x.avg_rank.toFixed(1)))))]
  } else body = [K('Waiting'), big('The next stage starts in a moment.')]
  return h('div', { className: `hc-panel hc-narr${sn.stage === 'done' || sn.stage === 'stopped' ? ' hc-idle' : ''}` },
    h('div', { className: 'hc-ph' }, 'Now', h('span', { className: 'hc-phr' }, h('span', { className: 'hc-pulse2' }))),
    h('div', { className: 'hc-nb', key: `${sn.stage}-${sn.chair}-${sn.ranked}-${sn.describing}-${sn.researching === 'review'}-${!!run.live?.ask}` }, ...body))
}

const LANE_CLS = { research: 'web', stage1: 'a', review: 'v', deliberate: 'd', chair: 'c' }
// The axis length: the first round length that fits. It changes only when the run outgrows it, so bars never
// drift; quarter ticks fall on round times (0:30, 1:00 …).
const tickStep = need => (need <= 150 ? 30 : need <= 330 ? 60 : need <= 660 ? 120 : need <= 1500 ? 300 : need <= 2400 ? 600 : 900)
const axisSpan = need => { const n = Math.max(60, need), st = tickStep(n); return Math.max(2, Math.ceil(n / st)) * st }
function Timeline({ run, sn }) {
  const seats = seatList(run)
  // With a time limit the axis is the limit itself (bars start at the left and fill toward it); without one it
  // snaps to round lengths that grow in rare steps. Either way it never rescales while a run is going.
  const limit = run.limits?.max_secs ? run.limits.max_secs + (run.limits.paused || 0) : 0
  const used = sn.live ? sn.t : sn.replaying && !sn.ended ? runEnd(run) : Math.max(sn.t, runEnd(run))
  const span = limit ? Math.max(limit, used) : axisSpan(sn.live ? sn.t * 1.08 + 10 : used)
  const step = tickStep(span)
  const ticks = []
  for (let v = 0; v <= span + 0.01; v += step) ticks.push({ v, label: clock(v) })
  if (span - ticks[ticks.length - 1].v > 0.01) {
    const gap = span - ticks[ticks.length - 1].v
    if (gap < step * 0.6 || gap / span < 0.18) ticks.pop()          // leave room for the "limit" label
    ticks.push({ v: span, label: clock(span) })
  }
  if (limit) ticks[ticks.length - 1].lim = Math.abs(ticks[ticks.length - 1].v - limit) < 0.5
  ticks.forEach(x => { x.label = clock(Math.round(x.v)) })
  const pct = v => `${Math.max(0, Math.min(100, (v / span) * 100))}%`
  const rows = [...seats.map(s => ({ who: s.label, name: sn.masked ? `Response ${s.label}` : s.name, model: s.model })), { who: 'chair', name: '⚖ Chairman', model: run.chairman?.model }]
  const liveText = r => {
    const ls = sn.lanes.filter(b => b.who === r.who)
    const cur = ls.find(b => b.running)
    if (cur) return `${{ research: 'researching', stage1: 'answering', review: 'ranking', deliberate: r.who === 'chair' ? 'listing points' : 'replying', chair: 'writing the verdict' }[cur.stage]} · ${clock(cur.shown)}`
    if (r.who === 'chair') return sn.chair === 'ok' ? `→ ${fit(clause(run.chair?.decision || ''), 40)}` : shortModel(r.model) || 'active model'
    const S = sn.seats[r.who] || {}
    if (S.a === 'fail') return 'no answer'
    if (sn.masked) return S.v === 'ok' ? 'ballot cast' : ''
    return S.a === 'ok' ? `→ ${clause(S.summary).toLowerCase()}` : shortModel(r.model) || 'active model'
  }
  return h('div', { className: 'hc-panel', style: { marginTop: 6 } },
    h('div', { className: 'hc-ph' }, 'Timeline', h('span', { className: 'hc-phr hc-mono' }, 'per seat · one bar per model call')),
    h('div', { className: 'hc-lanes' },
      h('div', { className: 'hc-axis' }, h('span', null, 'seat'), h('div', { className: 'hc-ticks' }, ticks.map((x, i) => h('span', { key: i, className: x.lim ? 'hc-tlim' : '', title: x.lim ? 'The time limit set in the brief' : undefined,
        style: { left: `${(x.v / span) * 100}%`, transform: i === 0 ? 'none' : i === ticks.length - 1 ? 'translateX(-100%)' : 'translateX(-50%)' } }, x.lim ? `${x.label} limit` : x.label))), h('span', { style: { textAlign: 'right' } }, 'tokens')),
      rows.map(r => h('div', { key: r.who, className: 'hc-lane' },
        h('div', { className: 'hc-who' }, h('b', { className: 'hc-ellipsis' }, r.name), h('span', { className: 'hc-live hc-ellipsis' }, liveText(r))),
        h('div', { className: 'hc-rail' }, sn.lanes.filter(b => b.who === r.who).map((b, i) => h('i', { key: i, className: `hc-${LANE_CLS[b.stage] || 'c'}${b.running ? ' hc-running' : ''}`,
          title: `${{ research: 'Research', stage1: 'Answer', review: 'Blind vote', deliberate: 'Debate', chair: 'Verdict' }[b.stage]} · ${b.running ? 'running' : `${fmtSecs(b.secs)} · ${fmtNum(b.tokens)} tokens`}`,
          style: { left: pct(b.start), width: pct(Math.max(span * 0.004, b.shown)) } }))),
        h('div', { className: 'hc-tok' }, r.who === 'chair' ? fmtNum(sn.chairLanes.filter(b => !b.running).reduce((x, b) => x + (b.tokens || 0), 0)) : fmtNum(sn.seats[r.who]?.tokens || 0)))),
      sn.live || (sn.replaying && !sn.ended) ? h('div', { className: 'hc-nowline', style: { left: `calc(16px + 170px + (100% - 32px - 242px) * ${Math.min(1, sn.t / span)})` } }) : null),
    h('div', { className: 'hc-legend' }, [...(run.research ? [['web', 'research']] : []), ['a', 'answer'], ['v', 'blind vote'], ['d', 'debate'], ['c', 'chairman']].map(([k, l]) => h('span', { key: k }, h('i', { className: `hc-${k}` }), l))))
}

const LOG_CLS = { start: 'k', answer: 'ok', fail: 'r', ballot: 't', ranking: 'ok', skip: 'w', agenda: 'w', reply: 'k', verdict: 'ok', stop: 'r' }
function LogPanel({ run, sn }) {
  const ref = useRef(null)
  const ev = sn.events
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight }, [ev.length])
  return panel('Live', `${ev.length} event${ev.length === 1 ? '' : 's'}`,
    h('div', { className: 'hc-log', ref }, ev.length ? ev.map((e, i) => h('div', { key: i },
      h('span', { className: 'hc-lt' }, clock(e.t)), ' ',
      h('span', { className: `hc-l${LOG_CLS[e.kind] || 't'}` }, `${e.who ? `${sn.masked && e.kind === 'answer' ? 'a seat' : e.who} · ` : ''}${e.text}`),
      e.quote && !(sn.masked && e.kind === 'answer') ? h('q', { dir: 'auto' }, fit(e.quote, 220)) : null))
      : h('div', { className: 'hc-muted' }, 'Waiting for the first call to finish…')))
}

const heat = (v, k) => {
  if (!v) return null
  const f = k > 1 ? (v - 1) / (k - 1) : 0
  return f <= 0 ? 'color-mix(in srgb,var(--hc-ok) var(--hc-m1),var(--hc-panel))' : f < 0.5 ? 'color-mix(in srgb,var(--hc-ok) var(--hc-m2),var(--hc-panel))'
    : f < 1 ? 'color-mix(in srgb,var(--hc-warn) var(--hc-m3),var(--hc-panel))' : 'color-mix(in srgb,var(--hc-bad) var(--hc-m4),var(--hc-panel))'
}
function Matrix({ run, rows, masked }) {
  const m = run.matrix || {}
  const cols = m.cols || []
  if (!cols.length || (!rows.length && run.review && !run.skipped?.review)) return h('div', { className: 'hc-sm hc-muted hc-pb' }, run.review ? (run.skipped?.review ? 'The blind vote was skipped.' : 'Ballots appear here as each seat ranks the others.') : 'This run had no blind vote.')
  const name = l => (masked ? `Response ${l}` : nameOf(run, l))
  return h('div', { className: 'hc-matrix', style: { gridTemplateColumns: `24px repeat(${cols.length},1fr)` } },
    h('span'), cols.map(c => h('span', { key: `h${c}`, className: 'hc-mh', title: name(c) }, c)),
    cols.map(r => [h('span', { key: `r${r}`, className: 'hc-mh', title: name(r) }, r),
      cols.map(c => {
        const v = rows.includes(r) ? m.cells?.[r]?.[c] : null
        return h('span', { key: `${r}${c}`, className: `hc-mc${r === c ? ' hc-mself' : ''}${v ? ' hc-min' : ''}`, style: v ? { background: heat(v, cols.length) } : undefined,
          title: v ? `${name(r)} ranked ${name(c)} #${v}${r === c ? ' (its own answer, without knowing)' : ''}` : '' }, v || '')
      })]))
}
function MatrixPanel({ run, sn }) {
  return panel('Vote matrix', 'row ranks column · 1 = best', h(Matrix, { run, rows: sn.rows, masked: sn.masked }))
}

// Re-convene: the same seats, models, time limit and budget, a new question, the earlier verdict as background.
function FollowUpDialog({ run, onClose, onStarted }) {
  const base = run.title || run.question.split('\n')[0].slice(0, 50)
  const [q, setQ] = useState('')
  const [title, setTitle] = useState(base ? `${base.replace(/ — follow-up( \d+)?$/, '')} — follow-up` : '')
  const [useCtx, setUseCtx] = useState(true)
  const [useFiles, setUseFiles] = useState(true)
  const [busy, setBusy] = useState(false)
  const nFiles = (run.files || []).length
  const fseats = (run.seats || []).map(s => ({ name: s.name, provider: s.provider || '', model: s.model || '', ...(typeof s.vision === 'boolean' ? { vision: s.vision } : {}), ...(s.thinking ? { thinking: s.thinking } : {}) }))
  const fchair = { provider: run.chairman?.provider || '', model: run.chairman?.model || '', ...(run.chairman?.thinking ? { thinking: run.chairman.thinking } : {}) }
  const [ev, setEv] = useState({ files: [], pack: null, evidence: '', seats: fseats })
  const [showEv, setShowEv] = useState(false)
  const updEv = patch => setEv(s => ({ ...s, ...(typeof patch === 'function' ? patch(s) : patch) }))
  const fcaps = useCaps([...fseats, fchair]).data || {}
  const evStats = evidenceStats(ev.files, fseats, fchair, run.review, fcaps)
  const mins = run.limits?.max_secs ? Math.round(run.limits.max_secs / 60) : 0
  const go = async () => {
    if (!q.trim()) { notify('warning', 'Type the follow-up question first.'); return }
    if (ev.files.some(f => f.busy)) { notify('warning', 'Wait until every file is read.'); return }
    if (evStats.pics && !evStats.describer) { notify('warning', 'The new photos need a model that can see pictures. Remove them, or set up a new committee with a vision model.'); return }
    setBusy(true)
    try {
      const body = { question: q.trim(), title: title.trim(), template: run.template, review: run.review, research: !!run.research, deliberate: run.deliberate || "never", deliberate_rounds: run.deliberate_rounds || 1,
        ...(run.custom_brief ? { chairman_guidance: run.chairman_guidance, decision_focus: !!run.decision_focus } : {}),
        chairman: fchair,
        seats: (run.seats || []).map(s => ({ name: s.name, instructions: s.instructions || '', provider: s.provider || '', model: s.model || '',
          ...(typeof s.vision === 'boolean' ? { vision: s.vision } : {}), ...(s.thinking ? { thinking: s.thinking } : {}) })),
        max_minutes: mins, max_usd: run.limits?.max_usd || 0, allow_unknown_price: !!(run.estimate?.unknown || []).length,
        follow_of: run.id, follow_context: useCtx, follow_evidence: useFiles && nFiles > 0,
        ...(ev.evidence?.trim() ? { evidence: ev.evidence } : {}),
        ...(evStats.on.length && ev.pack ? { evidence_pack: ev.pack, evidence_files: evStats.on.map(f => f.fid) } : {}) }
      const res = await startCommittee(body, body.title || q.trim().slice(0, 60))
      if (res.gate) { notify('warning', res.gate.error || 'The follow-up could not start.'); return }
      onStarted()
    } catch (e) { notify('error', `Could not re-convene: ${e.message || e}`) } finally { setBusy(false) }
  }
  return h('div', { className: 'hc-overlay', onMouseDown: e => { if (e.target === e.currentTarget && !busy) onClose() } },
    h('div', { className: 'hc-card hc-help' },
      h('div', { className: 'hc-row' }, h('b', { style: { fontSize: 15 } }, '🔁 Re-convene with a follow-up'), h('span', { className: 'hc-grow' }), h('button', { className: 'hc-link', onClick: onClose }, '✕ Close')),
      h('div', { className: 'hc-sm hc-muted', style: { margin: '6px 0 10px' } },
        `The same committee (${(run.seats || []).length} seats, the same models and instructions) answers again, blind, with a new vote and a new verdict.`),
      h('div', { className: 'hc-xs', style: { marginBottom: 10 } }, h('b', null, 'When to use it: '),
        'a new fact, new or missed evidence, a changed constraint or a “what if” that needs the committee’s judgement. To have the verdict explained or reworded, use 💬 Discuss in chat instead: one model, no new cost.'),
      h('div', { className: 'hc-flbl' }, 'Follow-up question'),
      h('textarea', { className: 'hc-q', rows: 3, autoFocus: true, dir: 'auto', value: q, onChange: e => setQ(e.target.value),
        placeholder: 'e.g. GB-8’s vibration is also rising. Does that change the decision?' }),
      h('div', { className: 'hc-flbl', style: { marginTop: 8 } }, 'Committee name'),
      h('input', { className: 'hc-in', style: { width: '100%' }, dir: 'auto', maxLength: 80, value: title, onChange: e => setTitle(e.target.value) }),
      h('label', { className: 'hc-row hc-sm', style: { marginTop: 10 } }, h('input', { type: 'checkbox', checked: useCtx, onChange: e => setUseCtx(e.target.checked) }),
        'Give the seats the earlier question, verdict and dissent as background'),
      nFiles ? h('label', { className: 'hc-row hc-sm', style: { marginTop: 4 } }, h('input', { type: 'checkbox', checked: useFiles, onChange: e => setUseFiles(e.target.checked) }),
        `Use the same evidence (${nFiles} file${nFiles > 1 ? 's' : ''}; picture descriptions are reused, not paid for again)`) : null,
      h('div', { className: 'hc-row', style: { marginTop: 8 } },
        h('button', { className: 'hc-link', onClick: () => setShowEv(!showEv) },
          showEv ? 'Hide new evidence' : evStats.on.length ? `📎 New evidence: ${evStats.on.length} file${evStats.on.length > 1 ? 's' : ''}` : '📎 Add new or missed evidence (optional)')),
      showEv ? h(EvidencePanel, { state: ev, upd: updEv, caps: fcaps, stats: evStats }) : null,
      showEv ? h('div', { className: 'hc-xs hc-muted' }, nFiles && useFiles
        ? 'New files are added to the earlier ones. Every seat reads all of them.'
        : 'Only these files go to the committee.') : null,
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 10 } },
        `Same limits: ${mins ? `⏱ ${mins} min` : '⏱ no time limit'} · ${run.limits?.max_usd ? `💲 ${usd(run.limits.max_usd)}` : '💲 no budget'}. To change the seats or the limits, use 💾 Save committee, then set up a new one from it.`),
      h('div', { className: 'hc-row hc-dlgfoot' }, h('span', { className: 'hc-grow' }),
        h(Button, { size: 'sm', variant: 'ghost', onClick: onClose, disabled: busy }, 'Cancel'),
        h('button', { className: `hc-convene${busy ? ' hc-busy' : ''}`, disabled: busy, onClick: go }, busy ? busyLabel('Convening…') : ['Convene follow-up', h('span', { key: 'i', className: 'hc-bic' }, '▶')]))))
}

function SeatAnswer({ run, label, onClose }) {
  const [full, setFull] = useState(false)
  const s = (run.seats || []).find(x => x.label === label)
  if (!s) return null
  const a = run.answers?.[label] || {}
  const pos = (run.ranking || []).findIndex(r => r.label === label)
  const r = run.ranking?.[pos]
  const list = (t, items) => (items?.length ? h('div', null, h('div', { className: 'hc-flbl' }, t), h('ul', { className: 'hc-list hc-sm' }, items.map((x, i) => h('li', { key: i }, x)))) : null)
  return h(Fragment, null,
    h('div', { className: 'hc-row' },
      h('div', { className: 'hc-av', style: { background: pcolor(s.provider) } }, initials(s.name)),
      h('div', { className: 'hc-grow', style: { minWidth: 0 } }, h('b', { className: 'hc-ellipsis', style: { display: 'block' } }, s.name),
        h('div', { className: 'hc-xs hc-muted hc-ellipsis' }, `${shortModel(s.model) || 'active model'} · ${fmtSecs(run.seconds?.stage1?.[label])}`)),
      h('button', { className: 'hc-link', onClick: onClose }, '✕')),
    roleFit(run, label) ? h('div', { className: `hc-sm ${roleFit(run, label).fit === 'drifted' ? 'hc-warn-t' : 'hc-muted'}`, dir: 'auto' },
      `Role (chairman’s note): ${roleFit(run, label).fit === 'drifted' ? 'drifted' : 'kept to it'}${roleFit(run, label).note ? ` · ${roleFit(run, label).note}` : ''}`) : null,
    h('div', { className: 'hc-row hc-wrap' },
      r ? h('span', { className: 'hc-chip hc-static hc-on' }, `#${pos + 1} by peers`) : null,
      r ? h('span', { className: 'hc-chip hc-static' }, `${r.first_votes} first-place vote${r.first_votes === 1 ? '' : 's'}`) : null,
      typeof a.confidence === 'number' ? h('span', { className: 'hc-chip hc-static' }, `confidence ${a.confidence.toFixed(2)}`) : null),
    a.status === 'failed' ? h('div', { className: 'hc-warn' }, `No answer: ${a.error}`) : h('div', { className: 'hc-lead' }, a.summary || ''),
    list('Key points', a.key_points), list('Risks', a.risks), list('Evidence gaps', a.evidence_gaps),
    s.instructions ? h('div', { className: 'hc-xs hc-muted' }, h('b', null, 'Its instructions: '), s.instructions) : null,
    a.answer ? h(Button, { size: 'sm', variant: 'outline', onClick: () => setFull(!full) }, full ? 'Hide full answer' : 'Read full answer') : null,
    full ? h('div', { className: 'hc-answer' }, h(Markdown, { text: a.answer })) : null)
}

const INSIGHT_ICON = { self: '⚖️', consensus: '🤝', calibration: '🎯', dissent: '🧭', timing: '⏱️', debate: '🗣' }

function delibSkipText(why) {
  return { 'no material disagreement': 'no material disagreement', 'same action': 'they recommended the same action', time: 'not enough time left',
    budget: 'not enough budget left', 'agenda failed': 'the chairman could not list the points', off: 'off' }[why] || why
}

const STANCE = { hold: ['holds', ''], change: ['changed', 'hc-on'], partly: ['partly changed', 'hc-on'] }
function DeliberationTab({ run }) {
  const d = run.deliberation || {}
  const c = run.chair || {}
  if (!d.rounds?.length) {
    return h('div', { className: 'hc-sm hc-muted' }, d.skipped ? `No debate: ${delibSkipText(d.skipped)}.${d.reason ? ` Chairman’s reading: ${d.reason}` : ''}` : 'The seats did not deliberate in this run.',
      (d.noted || []).length ? h('div', { style: { marginTop: 8 } }, 'Differences noted but not debated: ', d.noted.map(x => x.point).join('; '), '.') : null)
  }
  const decided = Object.fromEntries((c.disputes_resolved || []).map(x => [String(x.dispute).toUpperCase().split(/[^A-Z0-9]/)[0], x]))
  const last = (seat, id) => {
    let r = null
    d.rounds.forEach(rnd => (rnd.replies?.[seat]?.responses || []).forEach(x => { if (x.dispute === id) r = x }))
    return r
  }
  return h('div', null,
    d.reason ? h('div', { className: 'hc-sm hc-muted', style: { marginBottom: 10 } }, d.reason) : null,
    h('div', { className: 'hc-grid2' }, d.disputes.map(p => h('div', { key: p.id, className: 'hc-card hc-pad' },
      h('div', { className: 'hc-row' }, h('b', null, `${p.id} · ${p.point}`), h('span', { className: 'hc-grow' }),
        h('span', { className: 'hc-chip hc-static hc-chip-sm' }, p.level === 'action' ? 'about the action' : p.level === 'claim' ? 'about a claim' : 'emphasis'),
        (d.open || []).includes(p.id) ? h('span', { className: 'hc-chip hc-static hc-chip-sm hc-warn-t' }, 'still disputed') : null),
      h('ul', { className: 'hc-list hc-sm' }, p.positions.map(pos => {
        const r = last(pos.seat, p.id)
        const kept = r && r.stance !== 'hold' && r.side === pos.side
        const [word, cls] = kept ? ['conceded a point', 'hc-on'] : STANCE[r?.stance] || ['no reply', '']
        return h('li', { key: pos.seat }, h('b', null, pos.seat), ` · ${pos.side}`, r && r.side !== pos.side ? ` → ${r.side}` : '', ' ',
          h('span', { className: `hc-chip hc-static hc-chip-sm ${cls}` }, word), r?.argument ? h('div', { className: 'hc-xs hc-muted' }, r.argument) : null)
      })),
      decided[p.id] ? h('div', { className: 'hc-sm', style: { marginTop: 6 } }, h('b', null, 'Chairman: '), decided[p.id].decided,
        decided[p.id].by_argument ? h('div', { className: 'hc-xs hc-muted' }, `Decided by: ${decided[p.id].by_argument}`) : null) : null))),
    (d.moves || []).length ? h('div', { style: { marginTop: 12 } }, h('div', { className: 'hc-lbl' }, 'Who changed their mind'),
      h('ul', { className: 'hc-list hc-sm' }, d.moves.map((m, i) => h('li', { key: i }, h('b', null, m.seat), ` on ${m.dispute} (round ${m.round}): `,
        (m.kept_side ?? m.from === m.to) ? `kept “${m.from}” but conceded a point. ` : `${m.from} → ${m.to}. `, h('span', { className: 'hc-muted' }, m.because)))))
      : h('div', { className: 'hc-sm hc-muted', style: { marginTop: 12 } }, 'Nobody changed position in the debate.'))
}
function DecisionView({ run, onReplay, openChat, onFollow, openHelp }) {
  const [tab, setTab] = useState('answers')
  const c = run.chair || {}
  const seats = seatList(run)
  const stances = run.stances || {}
  const nOk = Object.values(run.answers || {}).filter(a => a.status === 'ok').length
  const agree = Object.values(stances).filter(v => v === 'agree').length
  const nDis = (c.dissent || []).length
  const conf = { low: 1, medium: 2, high: 3 }[String(c.confidence || '').toLowerCase()] || 0
  const tot = run.totals || {}
  const lim = run.limits || {}
  const dl = run.deliberation || {}
  const items = (c.action_items || []).length ? c.action_items : (c.next_actions || []).map(a => ({ action: a, owner: '', when: '' }))
  const structured = items.some(a => a.owner || a.when)
  const allSub = tot.calls > 0 && tot.subscription_calls === tot.calls
  const date = run.created ? new Date(run.created * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : ''
  const pdf = run.reports?.pdf, md = run.reports?.markdown
  const kpi = (label, value, small, cls, title) => h('div', { className: 'hc-panel hc-kpi', title }, h('span', null, label), h('b', { className: cls || '' }, value, small ? h('small', null, small) : null))
  const reasons = run.exec?.reasons || []
  const R = run.ranking || []
  const k = Math.max(2, R.length)
  return h(Fragment, null,
    h('div', { className: 'hc-panel hc-ruling' },
      h('div', { style: { minWidth: 0 } },
        h('div', { className: 'hc-kicker' }, `The committee’s ${run.decision_focus === false ? 'answer' : 'decision'} · ${run.template_title} · ${date}`),
        run.title ? h('div', { className: 'hc-runtitle', dir: 'auto' }, run.follow_of ? `↪ ${run.title}` : run.title) : null,
        h('div', { className: 'hc-qsmall', dir: 'auto' }, run.follow_of && !run.title ? `↪ Follow-up · ${run.question}` : run.question),
        run.background ? h('details', { className: 'hc-bg' },
          h('summary', { title: 'Context added to your question before the seats answered. It was sent to every seat’s provider.' }, 'Background given to the seats'),
          h('div', { className: 'hc-xs', dir: 'auto' }, run.background)) : null,
        h('h1', { className: 'hc-h1', dir: 'auto' }, c.decision || '—'),
        run.exec?.bottom_line ? h('p', { className: 'hc-bl', dir: 'auto', title: run.exec.assembled ? 'Taken from the chairman’s answer (this run has no written executive summary)' : 'The chairman’s executive summary' },
          run.exec.change ? h('b', null, `${run.exec.change}. `) : null, run.exec.bottom_line) : null,
        h('div', { className: 'hc-row hc-wrap', style: { marginTop: 12, gap: 6 } },
          (run.files || []).length ? h('span', { className: 'hc-chip hc-static', title: (run.files || []).map(f => f.name).join('\n') },
            `📎 ${run.files.length} file${run.files.length > 1 ? 's' : ''}${run.evidence_index?.images?.length ? ` · ${run.evidence_index.images.length} picture${run.evidence_index.images.length > 1 ? 's' : ''}` : ''}`) : null,
          run.skipped?.review ? h('span', { className: 'hc-chip hc-static hc-warn-t', title: 'The chairman decided from the answers alone.' }, `Blind vote skipped (${run.skipped.review === 'time' ? 'time limit' : 'budget'})`) : null,
          dl.skipped && dl.skipped !== 'off' ? h('span', { className: `hc-chip hc-static${['time', 'budget'].includes(dl.skipped) ? ' hc-warn-t' : ''}` }, `No debate: ${delibSkipText(dl.skipped)}`) : null,
          run.reports?.pdf_error ? h('span', { className: 'hc-chip hc-static hc-warn-t', title: run.reports.pdf_error }, 'No PDF (hover for why)') : null),
        h('div', { className: 'hc-actions2' },
          pdf ? h('button', { className: 'hc-convene', onClick: () => CTX.os.revealPath(pdf) }, '📄 Show the PDF') : null,
          md ? h('button', { className: 'hc-btn2', onClick: () => CTX.os.revealPath(md), title: 'The same report as Markdown, for notes and wikis' }, '⧉ Markdown') : null,
          h('button', { className: 'hc-btn2', onClick: onFollow, title: 'Ask the same committee a follow-up question. The seats get this verdict as background.' }, '🔁 Follow up'),
          h(SaveCommitteeButton, { run }),
          run.session_id ? h('button', { className: 'hc-btn2', onClick: openChat, title: 'Open the chat this committee ran in. One model answers there, not the committee.' }, '💬 Discuss in chat') : null,
          h('button', { className: 'hc-btn2', onClick: onReplay, title: `Watch the run again at ${REPLAY_SPEED}× speed` }, '▶ Replay'))),
      h('div', { className: 'hc-confbox' }, h('span', { className: 'hc-xs hc-muted' }, 'Confidence'),
        h('div', { className: 'hc-scale' }, [1, 2, 3].map(i => h('i', { key: i, className: i <= conf ? 'hc-on' : '' }))),
        h('b', null, c.confidence ? c.confidence[0].toUpperCase() + c.confidence.slice(1) : 'Not stated'),
        c.reverse_if?.[0] ? h('span', { className: 'hc-xs hc-muted', dir: 'auto' }, `Would reverse if ${c.reverse_if[0].replace(/^if\s+/i, '')}`) : null)),
    h('div', { className: 'hc-kpis' },
      kpi('Agree', agree, ` / ${nOk}`, '', 'Answers the chairman did not name as dissent'),
      kpi('Dissent', nDis, '', nDis ? 'hc-warn-t' : ''),
      kpi('Debate', dl.rounds?.length ? dl.disputes.length : '—', dl.rounds?.length ? ` point${dl.disputes.length === 1 ? '' : 's'} · ${(dl.moves || []).length} moved` : dl.skipped && dl.skipped !== 'off' ? ' none' : ' off'),
      kpi('Time', clock(run.summary?.elapsed ?? runEnd(run)), lim.max_secs ? ` / ${Math.round(lim.max_secs / 60)}m` : ''),
      kpi('Cost', allSub ? '$0' : usd(tot.cost_usd || 0), allSub ? ' subscription' : `${tot.unknown_calls ? ' + unpriced' : ''}${lim.max_usd ? ` / ${usd(lim.max_usd)}` : ''}`, '',
        'Estimated from token counts and list prices, not your provider’s bill. Subscription calls count $0.'),
      kpi('Calls', tot.calls || 0, ` · ${fmtTokens(tot.tokens || 0)} tok`)),
    h('div', { className: 'hc-three' },
      panel('Why', null, h('div', { className: 'hc-pb' }, reasons.length ? h('ol', { className: 'hc-ol' }, reasons.map((x, i) => h('li', { key: i, dir: 'auto' }, x)))
        : (c.agreement || []).length ? h('ul', { className: 'hc-ol' }, c.agreement.slice(0, 3).map((x, i) => h('li', { key: i, dir: 'auto' }, x)))
          : h('div', { className: 'hc-sm hc-muted' }, 'See the chairman’s full answer below.'))),
      panel('Do next', structured ? null : items.length ? `${items.length} action${items.length === 1 ? '' : 's'}` : null,
        h('div', { className: 'hc-pb', style: { paddingTop: structured ? 4 : 14 } }, !items.length ? h('div', { className: 'hc-sm hc-muted' }, 'No next actions listed.')
          : structured ? h('table', { className: 'hc-tbl' }, h('thead', null, h('tr', null, h('th', null, 'Action'), h('th', null, 'Owner'), h('th', null, 'When'))),
            h('tbody', null, items.map((a, i) => h('tr', { key: i }, h('td', { dir: 'auto' }, a.action), h('td', { dir: 'auto' }, a.owner || '—'), h('td', { className: 'hc-mono hc-xs' }, a.when || '—')))))
            : h('ol', { className: 'hc-ol' }, items.map((a, i) => h('li', { key: i, dir: 'auto' }, a.action))))),
      panel('What would change this', null, h('div', { className: 'hc-pb' }, (c.reverse_if || []).length ? h('ul', { className: 'hc-ol' }, c.reverse_if.map((x, i) => h('li', { key: i, dir: 'auto' }, x)))
        : h('div', { className: 'hc-sm hc-muted' }, 'The chairman named nothing that would reverse it.')))),
    h('div', { className: 'hc-lower' },
      panel('How the table stood', null,
        h(MiniArc, { height: 190, seats: seats.map(s => { const rf = roleFit(run, s.label); return { key: s.label, name: s.name, stance: stances[s.label] || (run.answers?.[s.label]?.status === 'failed' ? 'failed' : ''),
          drift: rf?.fit === 'drifted',
          title: `${s.name} · ${shortModel(s.model) || 'active model'}${stances[s.label] === 'dissent' ? ' · dissent' : stances[s.label] === 'agree' ? ' · in line with the decision' : ''}${rf ? `\nRole: ${rf.fit === 'drifted' ? 'drifted' : 'kept'}${rf.note ? ` · ${rf.note}` : ''}` : ''}` } }) }),
        h('div', { className: 'hc-legend2' }, h('span', null, h('i', { style: { background: 'var(--hc-ok)' } }), 'In line with the decision'),
          h('span', null, h('i', { style: { background: 'var(--hc-warn)' } }), 'Dissent'), h('span', null, h('i', { style: { background: 'var(--hc-acc)' } }), 'Chair'),
          ((run.chair || {}).role_fit || []).some(x => x.fit === 'drifted')
            ? h('span', { title: 'The chairman found this seat mostly answered outside its role. Hover the seat for the reason; rewrite its instructions if it keeps happening.' },
              h('b', { className: 'hc-drift-k' }, '!'), 'Drifted from role') : null)),
      h('div', { className: 'hc-panel' },
        h('div', { className: 'hc-ph' }, 'Blind ranking', h('span', { className: 'hc-phr' }, R.length ? 'average rank · lower is better' : '')),
        R.length ? h('div', { className: 'hc-pb hc-rbars' }, R.map(x => h('div', { key: x.label, className: 'hc-rrowb' },
          h('span', { className: 'hc-ellipsis', title: shortModel((run.seats || []).find(s => s.label === x.label)?.model) }, nameOf(run, x.label)),
          h('span', { className: 'hc-rbar' }, h('i', { style: { width: `${Math.max(4, ((k - x.avg_rank) / (k - 1)) * 100)}%` } })),
          h('span', { className: 'hc-mono hc-muted hc-xs' }, x.avg_rank.toFixed(1))))) : null,
        h(Matrix, { run, rows: run.matrix?.rows || [], masked: false })),
      h('div', { className: 'hc-panel' }, h(DecisionTabs, { run, tab, setTab, openHelp }))))
}

function DecisionTabs({ run, tab, setTab, openHelp }) {
  const c = run.chair || {}
  const dl = run.deliberation || {}
  const [open, setOpen] = useState(null)
  const tabs = [['answers', 'Answers ranked'], ...(run.deliberate && run.deliberate !== 'never' ? [['debate', dl.rounds?.length ? `Debate (${dl.disputes.length})` : 'Debate']] : []),
    ['dissent', `Dissent (${(c.dissent || []).length})`], ...(run.research ? [['sources', `Web sources (${(run.web?.items || []).length})`]] : []), ['insights', 'Insights'], ['cost', 'Time & cost'], ['more', 'Agreement'], ['final', 'Full answer']]
  let body
  if (tab === 'answers') {
    const order = (run.ranking || []).length ? run.ranking.map(r => r.label) : seatList(run).map(s => s.label)
    body = order.map((l, i) => {
      const s = (run.seats || []).find(x => x.label === l) || {}
      const a = run.answers?.[l] || {}
      if (open === l) return h('div', { key: l, className: 'hc-ansopen' }, h(SeatAnswer, { run, label: l, onClose: () => setOpen(null) }))
      return h('button', { key: l, className: 'hc-ansrow', onClick: () => setOpen(l), title: 'Read this seat’s answer' },
        h('span', { className: 'hc-rk' }, (run.ranking || []).length ? i + 1 : '·'),
        h('div', { style: { minWidth: 0 } }, h('b', { className: 'hc-ellipsis', style: { display: 'block' } }, s.name), h('div', { className: 'hc-xs hc-muted hc-mono hc-ellipsis' }, shortModel(s.model) || 'active model')),
        h('div', { dir: 'auto' }, roleFit(run, l)?.fit === 'drifted' ? h('span', { className: 'hc-drift-tag', title: roleFit(run, l).note }, 'drifted from role') : null,
          a.status === 'ok' ? a.summary : h('span', { className: 'hc-warn-t' }, `No answer (${a.error || 'not run'})`)))
    })
  } else if (tab === 'debate') body = h(DeliberationTab, { run })
  else if (tab === 'sources') {
    const web = run.web || {}
    body = (web.items || []).length ? h('div', { className: 'hc-pb' },
      h('div', { className: 'hc-xs hc-muted', style: { marginBottom: 8 } }, `Found by one web search before the seats answered${web.accessed ? ` (${web.accessed})` : ''}; every seat got the same passages, marked unverified.${web.dropped ? ` You unticked ${web.dropped}.` : ''}`),
      web.items.map(x => h('div', { key: x.id, className: 'hc-srcrow' },
        h('div', null, h('b', { className: 'hc-mono hc-xs' }, x.id), ' ', h('a', { href: x.url, target: '_blank', rel: 'noreferrer' }, x.title), h('span', { className: 'hc-xs hc-muted' }, ` · ${x.site}`)),
        h('div', { className: 'hc-sm hc-srcq', dir: 'auto' }, `“${x.excerpt}”`))),
      (web.queries || []).length ? h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, `Searched: ${web.queries.join(' · ')}`) : null)
      : h('div', { className: 'hc-pb hc-sm hc-muted' }, web.note || 'No web sources were added.')
  }
  else if (tab === 'dissent') {
    body = (c.dissent || []).length ? c.dissent.map((d, i) => h('div', { key: i, style: { marginBottom: 14 } },
      h('p', { className: 'hc-dissent', dir: 'auto' }, `“${d.view}”`), h('div', { className: 'hc-xs hc-muted', style: { marginTop: 6 } }, d.name || nameOf(run, d.label))))
      : h('div', { className: 'hc-sm hc-muted' }, 'The chairman recorded no dissent.')
  } else if (tab === 'insights') {
    const items = run.insights || []
    body = h('div', { style: { display: 'flex', flexDirection: 'column', gap: 8 } },
      items.length ? items.map((x, i) => h('div', { key: i, className: 'hc-insight' }, h('span', null, INSIGHT_ICON[x.kind] || '•'), h('div', null, h('b', null, `${x.title}. `), x.text)))
        : h('div', { className: 'hc-sm hc-muted' }, 'Nothing unusual in this run.'),
      h('button', { className: 'hc-link hc-tint', style: { alignSelf: 'flex-start' }, onClick: openHelp }, '? How to read this'))
  } else if (tab === 'cost') {
    const tot = run.totals || {}, lim = run.limits || {}, u = usageTotals(run)
    const per = l => (run.lanes || []).filter(b => b.who === l && !b.running)
    // The thinking level of the call each row controls (a seat's answer, the chairman's verdict); the clerical calls ran on low.
    const thinkOf = l => { const who = l === 'chair' ? 'Chairman' : (run.seats || []).find(s => s.label === l)?.name
      const u = (run.usage || []).find(x => x.who === who && x.stage === (l === 'chair' ? 'chair' : 'stage1') && x.thinking)
      return u ? (u.thinking === 'default' ? 'model default' : u.thinking) : '—' }
    const row = (k, v) => h('div', { className: 'hc-ansrow hc-static' }, h('span'), h('b', null, k), h('div', null, v))
    body = h(Fragment, null,
      row('Time', `${fmtSecs(run.summary?.elapsed ?? runEnd(run))}${lim.max_secs ? ` of a ${Math.round(lim.max_secs / 60)} min limit` : ' · no time limit'}`),
      row('Calls', `${tot.calls || 0} · ${fmtNum(tot.tokens || 0)} tokens (${fmtTokens(u.input)} in · ${fmtTokens(u.output)} out)`),
      row('Estimated cost', `${usd(tot.cost_usd || 0)}${lim.max_usd ? ` of ${usd(lim.max_usd)}` : ''}${tot.subscription_calls ? ` · ${tot.subscription_calls} subscription call${tot.subscription_calls === 1 ? '' : 's'} count $0` : ''}${tot.unknown_calls ? ` · ${tot.unknown_calls} call${tot.unknown_calls === 1 ? '' : 's'} with no known price, not counted` : ''}${u.list > (u.est || 0) ? ` · at list price ${usd(u.list)}` : ''}`),
      h('table', { className: 'hc-tbl', style: { marginTop: 12 } }, h('thead', null, h('tr', null, h('th', null, 'Seat'), h('th', null, 'Calls'), h('th', null, 'Time'), h('th', { title: 'Thinking level for the seat’s answer or the chairman’s verdict. The other steps ran on low.' }, 'Thinking'), h('th', { style: { textAlign: 'right' } }, 'Tokens'))),
        h('tbody', null, [...seatList(run).map(s => s.label), 'chair'].map(l => {
          const ls = per(l)
          return h('tr', { key: l }, h('td', null, nameOf(run, l)), h('td', { className: 'hc-mono hc-xs' }, ls.length), h('td', { className: 'hc-mono hc-xs' }, fmtSecs(ls.reduce((x, b) => x + b.secs, 0))),
            h('td', { className: 'hc-xs' }, thinkOf(l)),
            h('td', { className: 'hc-mono hc-xs', style: { textAlign: 'right' } }, fmtNum(ls.reduce((x, b) => x + b.tokens, 0))))
        }))),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, 'An estimate from token counts and list prices, not your provider’s bill.'))
  } else if (tab === 'more') {
    const L = (t, xs) => h('div', null, h('div', { className: 'hc-lbl' }, t), xs?.length ? h('ul', { className: 'hc-list' }, xs.map((x, i) => h('li', { key: i, dir: 'auto' }, x))) : h('div', { className: 'hc-sm hc-muted' }, 'None listed.'))
    body = h('div', { className: 'hc-grid2' }, L('Agreement', c.agreement), L('Open questions', c.open_questions))
  } else body = h('div', { className: 'hc-answer', style: { maxHeight: 'none', border: 0, padding: 0 } }, h(Markdown, { text: c.final_answer }))
  return h(Fragment, null,
    h('div', { className: 'hc-tabs2' }, tabs.map(([k, v]) => h('button', { key: k, className: `hc-tab2${tab === k ? ' hc-on' : ''}`, onClick: () => setTab(k) }, v))),
    h('div', { className: 'hc-tabbody' }, body))
}

// ── history & standings ────────────────────────────────────────────────────

function HistoryMenu({ runs, onOpen, onStandings, onClose }) {
  const ref = useRef(null)
  const [q, setQ] = useState('')
  useEffect(() => {
    const off = e => { if (ref.current && !ref.current.contains(e.target)) onClose() }
    document.addEventListener('mousedown', off)
    return () => document.removeEventListener('mousedown', off)
  }, [])
  const shown = runs.filter(r => !q || `${r.title || ''} ${r.question} ${r.decision || ''}`.toLowerCase().includes(q.toLowerCase()))
  return h('div', { className: 'hc-hpop', ref },
    h('input', { className: 'hc-in', autoFocus: true, value: q, onChange: e => setQ(e.target.value), placeholder: 'Search past committees…' }),
    h('div', { className: 'hc-hlist' },
      shown.length ? shown.map(r => h('button', { key: r.id, className: 'hc-hrow', onClick: () => onOpen(r.id) },
        h('div', { className: 'hc-row' },
          h('span', { style: { width: 14 } }, isLive(r) ? '●' : r.status === 'done' ? '✓' : r.status === 'cancelled' ? '⏹' : '!'),
          h('b', { className: 'hc-grow hc-ellipsis', dir: 'auto' }, `${r.follow_of ? '↪ ' : ''}${r.title || r.question}`)),
        r.title ? h('div', { className: 'hc-xs hc-ellipsis', style: { paddingLeft: 22 }, dir: 'auto' }, r.question) : null,
        h('div', { className: 'hc-xs hc-muted hc-ellipsis', style: { paddingLeft: 22 } },
          `${r.template_title} · ${ago(r.created)} · ${isLive(r) ? 'in session' : r.decision || (r.stale ? 'abandoned' : r.status)}`))) : h('div', { className: 'hc-sm hc-muted', style: { padding: 10 } }, 'No committees yet.')),
    h('button', { className: 'hc-hrow hc-center hc-sm', onClick: onStandings }, '📊 Model standings across all runs'))
}

function StandingsDialog({ onClose }) {
  const q = useStandings(true)
  const d = q.data
  return h('div', { className: 'hc-overlay', onMouseDown: e => { if (e.target === e.currentTarget) onClose() } },
    h('div', { className: 'hc-card hc-help' },
      h('div', { className: 'hc-row' }, h('b', { style: { fontSize: 15 } }, 'Model standings'), h('span', { className: 'hc-grow' }), h('button', { className: 'hc-link', onClick: onClose }, '✕ Close')),
      q.isError ? h('div', { className: 'hc-warn' }, String(q.error?.message || q.error))
        : !d ? h(Skeleton, { className: 'hc-skel-sm' }) : !d.runs ? h('div', { className: 'hc-sm hc-muted' }, 'Standings appear after your first finished committee with a blind vote.') : h(Fragment, null,
        h('div', { className: 'hc-sm hc-muted', style: { margin: '6px 0 10px' } },
          `How the other AIs ranked each model's answers in ${d.runs} finished committee${d.runs > 1 ? 's' : ''}.${d.enough ? '' : ' Treat this as anecdote until about 10 runs.'}`),
        d.table.map((r, i) => h('div', { key: r.model, className: 'hc-stand' },
          h('b', null, i + 1),
          h('div', { style: { minWidth: 0 } },
            h('div', { className: 'hc-row' }, h('span', { className: 'hc-dot', style: { background: pcolor(r.provider) } }), h('b', null, shortModel(r.model)),
              h('span', { className: 'hc-xs hc-muted hc-ellipsis hc-grow' }, ` as ${r.roles.join(', ')}`), h('span', { className: 'hc-xs hc-muted' }, `${r.first_votes} first-place votes`)),
            h('div', { className: 'hc-bar' }, h('i', { style: { width: `${Math.round(r.score * 100)}%`, background: pcolor(r.provider) } })),
            h('div', { className: 'hc-xs hc-muted' }, `peer score ${r.score.toFixed(2)} · ${r.runs} run${r.runs > 1 ? 's' : ''}`
              + (r.confidence != null ? ` · its own confidence ${r.confidence.toFixed(2)}` : '') + (r.median_secs != null ? ` · median ${fmtSecs(r.median_secs)}` : '')))))),
      h('div', { className: 'hc-xs hc-muted', style: { marginTop: 8 } }, 'Peer score 1.0 = every other seat ranked it first; 0.0 = last.')))
}

// ── page ───────────────────────────────────────────────────────────────────

function CommitteePage() {
  const runsQ = useRuns()
  const setupQ = useSetup()
  const modelsQ = useModels()
  const runs = runsQ.data?.runs || []
  if (setupQ.data?.profile) PROFILE = setupQ.data.profile
  const rootRef = useRef(null)
  const { scheme, onAcc } = useScheme(rootRef)
  const [view, setView] = useState(() => (pending.draft ? { kind: 'waiting' } : null))
  const [lastRun, setLastRun] = useState(null)
  const [hist, setHist] = useState(false)
  const [help, setHelp] = useState(false)
  const [standings, setStandings] = useState(false)
  const openRun = (id, page) => { setLastRun(id); setView({ kind: 'run', id, page: page || (runs.find(r => r.id === id)?.status === 'done' ? 'decision' : 'convene') }) }

  useEffect(() => {
    if (!runsQ.data) return
    if (pending.draft) {
      const r = runs.find(x => x.draft === pending.draft)
      if (r) { pending.draft = null; openRun(r.id, 'convene'); return }
      if (Date.now() - pending.since > 120_000) { pending.draft = null; notify('warning', 'The committee has not opened yet. Check its chat in the sidebar.') }
      else { if (!view || view.kind !== 'waiting') setView({ kind: 'waiting' }); return }
    }
    if (view === null || view.kind === 'waiting') {
      const liveRun = runs.find(isLive)
      if (liveRun) openRun(liveRun.id, 'convene'); else setView({ kind: 'setup' })
    }
  }, [runsQ.data])

  const runId = view?.kind === 'run' ? view.id : lastRun
  const runRow = runs.find(r => r.id === runId)
  const cur = !view || view.kind === 'setup' ? 0 : view.kind === 'waiting' ? 1 : view.page === 'decision' && runRow?.status === 'done' ? 2 : 1
  const steps = [['Brief', () => setView({ kind: 'setup' }), true],
    ['Convene', () => runId && openRun(runId, 'convene'), !!runId],
    ['Decision', () => runId && openRun(runId, 'decision'), runRow?.status === 'done']]

  let body
  if (runsQ.isError || setupQ.isError) body = h(ErrorState, { title: 'The committee backend is not answering', description: `${(runsQ.error || setupQ.error)?.message || ''} After installing or updating the plugin, restart the gateway (hermes gateway restart).` })
  else if (!runsQ.data || !setupQ.data || !view) body = h(Skeleton, { className: 'hc-skel' })
  else if (view.kind === 'waiting') body = h('div', { className: 'hc-panel hc-waiting' },
    h('div', { className: 'hc-spinner' }), h('b', null, 'Convening the committee…'),
    h('div', { className: 'hc-sm hc-muted' }, 'Hermes is reading your request in the committee’s chat. The table appears here as soon as the committee opens, usually within a few seconds.'),
    h(Button, { size: 'sm', variant: 'ghost', onClick: () => { pending.draft = null; setView({ kind: 'setup' }) } }, 'Back to the brief'))
  else if (view.kind === 'setup') body = h(SetupView, { setup: setupQ.data, models: modelsQ.data, openHelp: () => setHelp(true), onConvened: () => setView({ kind: 'waiting' }) })
  else body = h(RunView, { runId: view.id, page: view.page, setPage: p => setView(v => ({ ...v, page: p })), openHelp: () => setHelp(true), onConvened: () => setView({ kind: 'waiting' }) })

  return h('div', { className: 'hc-root', ref: rootRef, 'data-scheme': scheme, style: { '--hc-on-acc': onAcc } },
    h('style', null, CSS),
    h('header', { className: 'hc-top' },
      h('div', { className: 'hc-logo2' }, '⚖ ', h('b', null, 'Committee')),
      h('nav', { className: 'hc-steps2' }, steps.map(([label, go, on], i) => h('button', { key: label, disabled: !on,
        className: `hc-step2${i === cur ? ' hc-on' : ''}${i < cur ? ' hc-done' : ''}`, onClick: go,
        title: on ? '' : i === 1 ? 'Convene a committee, or open one from History' : 'The decision appears when a committee finishes' },
        h('i', null, i < cur ? '✓' : i + 1), label))),
      h('div', { className: 'hc-hstat' },
        h('button', { className: 'hc-link hc-tint', onClick: () => setHelp(true) }, '? Help'),
        h('div', { className: 'hc-hist' },
          h('button', { className: 'hc-btn2 hc-btn-sm', onClick: () => setHist(!hist) }, `🕘 History${runs.length ? ` (${runs.length})` : ''} ▾`),
          hist ? h(HistoryMenu, { runs, onClose: () => setHist(false), onOpen: id => { setHist(false); openRun(id) }, onStandings: () => { setHist(false); setStandings(true) } }) : null))),
    h('div', { className: 'hc-page', key: `${view?.kind}-${view?.id || ''}` }, body),
    help ? h(Help, { setup: setupQ.data, onClose: () => setHelp(false) }) : null,
    standings ? h(StandingsDialog, { onClose: () => setStandings(false) }) : null)
}

const CSS = `
.hc-root{--hc-bg:var(--ui-bg-editor,#0e1014);--hc-panel:var(--ui-bg-secondary,#15181f);--hc-panel2:var(--ui-bg-tertiary,color-mix(in srgb,var(--hc-panel) 86%,var(--hc-fg)));--hc-line:var(--ui-stroke-secondary,#282d38);--hc-line2:color-mix(in srgb,var(--hc-line) 62%,var(--hc-fg));--hc-fg:var(--ui-text-primary,#ece8df);--hc-fg2:var(--ui-text-secondary,color-mix(in srgb,var(--hc-fg) 84%,var(--hc-bg)));--hc-mut:var(--ui-text-tertiary,#9a978e);--hc-acc:var(--ui-accent,#cfa94f);--hc-ok:var(--ui-success,#74c897);--hc-warn:var(--ui-yellow,#e8b155);--hc-bad:var(--dt-destructive,#e07563);--hc-ans:#6fb3d9;--hc-vote:#a690e6;--hc-deb:#e07563;--hc-web:#5fbfae;--hc-serif:Georgia,"Times New Roman",serif;--hc-mono:"Cascadia Mono",Consolas,ui-monospace,monospace;--hc-solid:var(--ui-bg-elevated,var(--hc-bg));
  height:100%;overflow:auto;padding:0 0 48px;font-size:13px;line-height:1.45;color:var(--hc-fg);position:relative}
.hc-root{--hc-m1:62%;--hc-m2:36%;--hc-m3:45%;--hc-m4:50%}
.hc-root[data-scheme=light]{--hc-ans:#1c78b5;--hc-vote:#9b3fc4;--hc-deb:#c2402f;--hc-web:#14826f;--hc-m1:78%;--hc-m2:52%;--hc-m3:62%;--hc-m4:66%}
.hc-mono{font-family:var(--hc-mono)}
.hc-top{display:flex;align-items:center;gap:20px;padding:10px 26px;border-bottom:1px solid var(--hc-line);position:sticky;top:0;z-index:25;background:color-mix(in srgb,var(--hc-bg) 92%,transparent);backdrop-filter:blur(8px)}
.hc-logo2{font-family:var(--hc-serif);font-size:18px;white-space:nowrap}.hc-logo2 b{color:var(--hc-acc);font-weight:400}
.hc-steps2{display:flex;gap:4px;margin:0 auto}
.hc-step2{background:none;border:0;color:var(--hc-mut)!important;padding:6px 13px;border-radius:999px;display:flex;gap:8px;align-items:center;cursor:pointer;transition:background .2s,color .2s}
.hc-step2:hover:not(:disabled){color:var(--hc-fg)!important}.hc-step2:disabled{opacity:.45;cursor:default}
.hc-step2 i{font-style:normal;width:21px;height:21px;border-radius:50%;border:1px solid var(--hc-line);display:grid;place-items:center;font-size:11px;transition:all .3s}
.hc-step2.hc-on{color:var(--hc-fg)!important;background:var(--hc-panel2)}.hc-step2.hc-on i{border-color:var(--hc-acc);color:var(--hc-acc)}
.hc-step2.hc-done i{background:var(--hc-acc);border-color:var(--hc-acc);color:var(--hc-on-acc)}
.hc-hstat{display:flex;gap:10px;align-items:center}
.hc-page{padding:20px 26px 0;max-width:1480px;margin:0 auto;animation:hc-fade .35s ease}
@keyframes hc-fade{from{opacity:0;transform:translateY(6px)}}
.hc-kicker{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--hc-acc)}
.hc-h1{font-family:var(--hc-serif);font-weight:400;font-size:30px;line-height:1.18;margin:6px 0 14px;color:var(--hc-fg)}
.hc-panel{background:var(--hc-panel);border:1px solid var(--hc-line);border-radius:14px}
.hc-ph{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--hc-line);font-weight:600;font-size:11px;letter-spacing:.13em;text-transform:uppercase;color:var(--hc-mut)}
.hc-phr{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:400}
.hc-pb{padding:14px 16px}
.hc-btn2{background:var(--hc-panel2);border:1px solid var(--hc-line);border-radius:10px;padding:7px 13px;cursor:pointer;transition:border-color .15s;white-space:nowrap}
.hc-btn2:hover{border-color:var(--hc-line2)}.hc-btn-sm{padding:5px 11px;font-size:12px}
.hc-btn2.hc-danger{border-color:var(--hc-bad);color:var(--hc-bad)!important}
.hc-convene{border:0;border-radius:11px;padding:10px 22px;font-weight:650!important;font-size:14px!important;background:var(--hc-acc);color:var(--hc-on-acc)!important;cursor:pointer;box-shadow:0 6px 22px -8px color-mix(in srgb,var(--hc-acc) 60%,transparent);transition:transform .15s,box-shadow .15s}
.hc-convene:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 10px 28px -8px color-mix(in srgb,var(--hc-acc) 75%,transparent)}
.hc-convene:disabled{opacity:.6;cursor:default}.hc-convene-lg{padding:12px 24px;font-size:15px!important}
/* brief */
.hc-brief{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:22px;align-items:start}
.hc-bq{width:100%;min-height:120px;background:var(--hc-panel);border:1px solid var(--hc-line);border-radius:14px;color:var(--hc-fg);font:20px/1.45 var(--hc-serif);padding:16px 18px;resize:vertical;outline:none;transition:border-color .2s}
.hc-bq:focus{border-color:var(--hc-acc)}
.hc-qmeta{display:flex;gap:10px;align-items:center;margin:6px 2px 0;font-size:11px;color:var(--hc-mut)}.hc-namein{flex:1;font-size:12px!important;padding:5px 9px!important}
.hc-evwrap .hc-ev{border:0;margin:0;background:none;padding:12px 16px}
.hc-share{display:flex;gap:8px;margin:0 16px 14px;padding:8px 11px;border-radius:9px;background:color-mix(in srgb,var(--hc-warn) 14%,var(--hc-panel));color:color-mix(in srgb,var(--hc-warn) 70%,var(--hc-fg));font-size:12.5px}
.hc-types{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.hc-rosterhead{display:grid;grid-template-columns:200px 1fr;gap:16px;align-items:center}
.hc-mini{width:100%;display:block}
.hc-mini-sp{stroke:var(--hc-line2);stroke-width:1.2}
.hc-mseat{cursor:pointer;animation:hc-pop .45s ease backwards}
@keyframes hc-pop{from{opacity:0;transform:scale(.6)}}
.hc-mseat circle{fill:var(--hc-panel2);stroke:var(--hc-line);stroke-width:1.5;transition:stroke .2s,fill .2s}
.hc-mseat:hover circle,.hc-mseat.hc-sel circle{stroke:var(--hc-acc)}
.hc-mseat text{fill:var(--hc-fg);font-size:11px;text-anchor:middle;dominant-baseline:central}
.hc-mchair rect{fill:color-mix(in srgb,var(--hc-acc) 15%,var(--hc-panel));stroke:var(--hc-acc);stroke-width:1.5}.hc-mchair.hc-sel rect{stroke-width:2.5}
.hc-drift circle{fill:var(--hc-warn);stroke:var(--hc-panel);stroke-width:1.5}
.hc-drift text{fill:var(--hc-panel);font-size:8px;font-weight:700}
.hc-drift-k{display:inline-grid;place-items:center;width:12px;height:12px;border-radius:50%;background:var(--hc-warn);color:var(--hc-panel);font-size:9px;margin-right:6px}
.hc-drift-tag{display:inline-block;margin-right:6px;padding:0 6px;border-radius:8px;font-size:10.5px;color:var(--hc-warn);border:1px solid color-mix(in srgb,var(--hc-warn) 50%,transparent)}
.hc-mseat.hc-agree circle{stroke:var(--hc-ok);fill:color-mix(in srgb,var(--hc-ok) 14%,var(--hc-panel))}
.hc-mseat.hc-dissent circle{stroke:var(--hc-warn);fill:color-mix(in srgb,var(--hc-warn) 14%,var(--hc-panel))}
.hc-mseat.hc-failed circle{stroke-dasharray:3 3;opacity:.6}
.hc-tbl{width:100%;border-collapse:collapse}
.hc-tbl th{font-weight:600;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--hc-mut);text-align:left;padding:8px;border-bottom:1px solid var(--hc-line)}
.hc-tbl td{padding:8px;border-bottom:1px solid var(--hc-line);vertical-align:top}
.hc-roster2 tbody tr{transition:background .15s}
.hc-seatrow{cursor:pointer}.hc-seatrow:hover,.hc-seatrow.hc-open{background:var(--hc-panel2)}
.hc-av2{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:var(--hc-panel2);border:1px solid var(--hc-line);font-size:10px;margin-right:8px;vertical-align:middle}
.hc-av-chair{border-color:var(--hc-acc);color:var(--hc-acc)}
.hc-ins{display:block;font-size:11.5px;color:var(--hc-mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:330px;margin-left:32px}
.hc-ins.hc-none{font-style:italic;opacity:.75}
.hc-pen{opacity:0;color:var(--hc-acc);margin-left:6px;transition:opacity .15s}.hc-seatrow:hover .hc-pen,.hc-seatrow.hc-open .hc-pen{opacity:1}
.hc-pill{display:inline-flex;align-items:center;gap:6px;font:12px var(--hc-mono);border:1px solid var(--hc-line);border-radius:999px;padding:2px 10px;background:var(--hc-bg);white-space:nowrap}
.hc-bill{font:11px var(--hc-mono);color:var(--hc-mut);white-space:nowrap}.hc-bill.hc-sub{color:var(--hc-ok)}
.hc-edit>td{padding:0!important;background:var(--hc-panel2)}
.hc-editor{padding:6px 14px 16px 40px;display:grid;gap:10px;animation:hc-open .3s ease;cursor:default}
@keyframes hc-open{from{opacity:0;transform:translateY(-4px)}}
.hc-row2{display:grid;grid-template-columns:1fr 240px;gap:12px}
.hc-elbl{display:flex;font-weight:600;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--hc-mut);margin-bottom:4px}
.hc-cnt{margin-left:auto;font-weight:400;letter-spacing:0}
.hc-editor .hc-in{background:var(--hc-bg)}
.hc-hint{font-size:12px;color:var(--hc-mut)}.hc-hint b{color:var(--hc-fg);font-weight:600}
.hc-efoot{display:flex;align-items:center;gap:10px}
.hc-addrow td{color:var(--hc-acc);cursor:pointer;font-size:13px}.hc-addrow:hover td{background:var(--hc-panel2)}
.hc-runrow{display:grid;grid-template-columns:1fr 1fr;gap:0 26px;margin-top:8px}
.hc-kv2{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--hc-line)}
.hc-kv2 small{display:block;color:var(--hc-mut);font-size:11.5px}
.hc-kv2 .hc-tlpop{left:auto;right:0}
.hc-sw{width:34px;height:19px;border-radius:10px;background:var(--hc-line2);position:relative;border:0;transition:background .2s;flex:none;cursor:pointer}
.hc-sw:after{content:"";position:absolute;width:15px;height:15px;border-radius:50%;background:#fff;top:2px;left:2px;transition:left .2s}
.hc-sw.hc-on{background:var(--hc-acc)}.hc-sw.hc-on:after{left:17px}
.hc-estbar{position:relative;height:22px;background:var(--hc-bg);border:1px solid var(--hc-line);border-radius:7px;margin:8px 0 4px}
.hc-rng{position:absolute;top:5px;bottom:5px;background:linear-gradient(90deg,var(--hc-ok),var(--hc-warn));border-radius:3px;transition:all .3s}
.hc-cap{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--hc-bad);transition:left .3s}
.hc-cap:after{content:"cap";position:absolute;top:-15px;left:-8px;font:10px var(--hc-mono);color:var(--hc-bad)}
.hc-warns{display:flex;flex-direction:column;gap:3px;margin-top:10px}
.hc-go{display:flex;align-items:center;gap:10px;margin-top:14px;padding-top:14px;border-top:1px solid var(--hc-line);flex-wrap:wrap}
/* convene */
.hc-recaprow{display:flex;align-items:flex-start;gap:12px;margin:4px 0 16px}
.hc-recap{font:18px/1.4 var(--hc-serif);color:var(--hc-fg2)}
.hc-conv{display:grid;grid-template-columns:250px minmax(0,1fr) 330px;gap:18px;align-items:start}
.hc-col{display:flex;flex-direction:column;gap:14px}
.hc-stages{list-style:none;margin:0;padding:12px 16px}
.hc-stages li{display:flex;gap:12px;padding:8px 0;color:var(--hc-mut);transition:color .3s;position:relative}
.hc-sdot{width:22px;height:22px;border-radius:50%;border:1px solid var(--hc-line);flex:none;display:grid;place-items:center;font-size:11px;transition:all .3s;background:var(--hc-bg)}
.hc-stages li:not(:last-child):after{content:"";position:absolute;left:10.5px;top:31px;bottom:-5px;width:1px;background:var(--hc-line)}
.hc-sbar{height:3px;border-radius:2px;background:var(--hc-panel2);margin-top:6px;overflow:hidden;width:100%;max-width:160px}
.hc-sbar i{display:block;height:100%;width:0;background:var(--hc-acc);transition:width .4s}
.hc-sbar.hc-indet i{width:40%!important;animation:hc-indet 1.6s ease-in-out infinite}
@keyframes hc-indet{0%{transform:translateX(-100%)}100%{transform:translateX(260%)}}
.hc-scnt{font:11px var(--hc-mono);margin-left:6px;color:var(--hc-mut)}
.hc-st-now{color:var(--hc-fg)!important}.hc-st-now .hc-sdot{border-color:var(--hc-acc);animation:hc-breathe 1.4s infinite}
.hc-st-done{color:var(--hc-fg)!important}.hc-st-done .hc-sdot{background:var(--hc-acc);border-color:var(--hc-acc);color:var(--hc-on-acc)}
.hc-st-done .hc-sbar i{background:var(--hc-ok)}.hc-st-skip{opacity:.55}.hc-st-stop .hc-sdot{animation:none;border-color:var(--hc-bad)}
@keyframes hc-breathe{0%,100%{box-shadow:0 0 0 3px color-mix(in srgb,var(--hc-acc) 18%,transparent)}50%{box-shadow:0 0 0 8px color-mix(in srgb,var(--hc-acc) 4%,transparent)}}
.hc-gauges{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px;padding:14px 16px;border-top:1px solid var(--hc-line)}
.hc-tile{display:flex;flex-direction:column;min-width:0}
.hc-tl{font-size:10.5px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--hc-mut)}
.hc-tv{font:400 19px/1.25 var(--hc-mono);font-variant-numeric:tabular-nums;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hc-tbar{height:3px;border-radius:2px;background:var(--hc-panel2);margin-top:6px;overflow:hidden}
.hc-tbar-off{visibility:hidden}
.hc-tbar i{display:block;height:100%;border-radius:2px;transition:width .5s linear}
.hc-tn{font-size:11px;color:var(--hc-mut);margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hc-research{display:flex;gap:10px;align-items:flex-start;margin-top:10px;padding-top:10px;border-top:1px dashed var(--hc-line)}
.hc-research>div{display:flex;flex-direction:column;gap:3px;min-width:0}
.hc-alert-src{background:color-mix(in srgb,var(--hc-acc) 10%,var(--hc-panel));border-color:color-mix(in srgb,var(--hc-acc) 45%,transparent)}
.hc-alert-src b{color:var(--hc-fg)}
.hc-srclist{display:flex;flex-direction:column;gap:6px;max-height:260px;overflow:auto;margin:2px 0}
.hc-src{display:flex;gap:8px;align-items:flex-start;padding:6px 8px;border-radius:8px;background:var(--hc-panel);border:1px solid var(--hc-line);cursor:pointer}
.hc-src input{margin-top:3px}
.hc-src-off{opacity:.5}
.hc-srcq{color:var(--hc-fg);opacity:.85;margin-top:2px}
.hc-srcrow{padding:8px 0;border-bottom:1px solid var(--hc-line)}
.hc-srcrow:last-child{border-bottom:0}
.hc-alert{margin:0 16px 12px;padding:10px 12px;border-radius:10px;display:flex;flex-direction:column;gap:5px;font-size:12.5px;background:color-mix(in srgb,var(--hc-warn) 13%,var(--hc-panel));border:1px solid color-mix(in srgb,var(--hc-warn) 50%,transparent);animation:hc-fade .3s}
.hc-alert b{color:color-mix(in srgb,var(--hc-warn) 75%,var(--hc-fg));font-weight:650}
.hc-alert-ask{border-width:2px;animation:hc-fade .3s,hc-alertpulse 1.6s ease-in-out infinite}
@keyframes hc-alertpulse{50%{box-shadow:0 0 0 4px color-mix(in srgb,var(--hc-warn) 22%,transparent)}}
.hc-alert-btns{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:3px}.hc-alert-btns .hc-btn2{padding:5px 11px;font-size:12px}
.hc-alert-sent{color:var(--hc-mut)}
.hc-ctl{display:flex;gap:8px;padding:0 16px 12px;flex-wrap:wrap}.hc-ctl>*{flex:1}
.hc-ctlnote{padding:0 16px 12px}
.hc-stop{border-radius:10px;padding:8px;font-weight:650;cursor:pointer;background:transparent;color:var(--hc-bad)!important;border:1px solid color-mix(in srgb,var(--hc-bad) 55%,transparent)}
.hc-stop:hover{background:color-mix(in srgb,var(--hc-bad) 12%,transparent)}.hc-stop:disabled{opacity:.6}
.hc-nb{padding:12px 16px 16px;min-height:150px}
.hc-nb>*{animation:hc-narr .45s ease backwards}.hc-nb>*:nth-child(2){animation-delay:.08s}.hc-nb>*:nth-child(3){animation-delay:.16s}
@keyframes hc-narr{from{opacity:0;transform:translateY(8px)}}
.hc-big{font:17px/1.38 var(--hc-serif);margin-top:6px}
.hc-ranks{list-style:none;margin:8px 0 0;padding:0;font-size:13px}
.hc-ranks li{display:flex;gap:8px;align-items:center;padding:2px 0;animation:hc-rin .35s ease backwards}
@keyframes hc-rin{from{opacity:0;transform:translateX(-6px)}}
.hc-ranks b{color:var(--hc-acc);font-family:var(--hc-serif);width:12px}.hc-rname{width:110px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hc-rb{height:5px;border-radius:3px;background:var(--hc-vote);transition:width .6s}
.hc-see{margin-top:14px;width:100%}
.hc-pulse2{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--hc-acc);animation:hc-breathe 1.4s infinite}
.hc-idle .hc-pulse2{animation:none;background:var(--hc-ok)}
.hc-stagewrap{position:relative;aspect-ratio:980/712}
.hc-room{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.hc-orbit{fill:none;stroke:color-mix(in srgb,var(--hc-acc) 25%,var(--hc-line));stroke-width:1.5;stroke-dasharray:2 7}
.hc-spoke{stroke:var(--hc-line2);stroke-width:1.5;opacity:.7}.hc-spoke.hc-lit{stroke:color-mix(in srgb,var(--hc-acc) 45%,var(--hc-line))}
.hc-beam{fill:none;stroke:var(--hc-acc);stroke-width:2.5;stroke-linecap:round;opacity:.9;filter:drop-shadow(0 0 4px color-mix(in srgb,var(--hc-acc) 55%,transparent));animation:hc-draw 1.3s cubic-bezier(.5,0,.3,1) forwards}
.hc-beam-done{animation:none;opacity:.35}
@keyframes hc-draw{to{stroke-dashoffset:0}}
.hc-node .hc-disc{fill:var(--hc-panel);stroke:var(--hc-line);stroke-width:2;transition:stroke .4s,fill .4s}
.hc-node .hc-ring{fill:none;stroke:var(--hc-ans);stroke-width:2;stroke-dasharray:6 8;opacity:0;transition:opacity .3s;transform-box:fill-box;transform-origin:center}
.hc-thinking .hc-ring{opacity:1;animation:hc-spin2 2.4s linear infinite}
.hc-voting .hc-ring{stroke:var(--hc-vote);opacity:1;animation:hc-spin2 1.6s linear infinite}
.hc-debating .hc-ring{stroke:var(--hc-deb);opacity:1;animation:hc-spin2 1.2s linear infinite}
@keyframes hc-spin2{to{transform:rotate(360deg)}}
.hc-node .hc-arc{fill:none;stroke:var(--hc-ans);stroke-width:3;stroke-linecap:round;transform:rotate(-90deg);transition:stroke-dashoffset .5s linear,opacity .4s;opacity:.9}
.hc-answered .hc-arc,.hc-agree .hc-arc,.hc-dissent .hc-arc,.hc-failed .hc-arc{opacity:0}
.hc-answered .hc-disc{stroke:var(--hc-acc)}
.hc-agree .hc-disc{stroke:var(--hc-ok);fill:color-mix(in srgb,var(--hc-ok) 13%,var(--hc-panel))}
.hc-dissent .hc-disc{stroke:var(--hc-warn);fill:color-mix(in srgb,var(--hc-warn) 13%,var(--hc-panel))}
.hc-failed .hc-disc{stroke-dasharray:5 5;opacity:.55}
.hc-ini{fill:var(--hc-fg);font-size:15px;font-weight:600;text-anchor:middle;dominant-baseline:central;transition:opacity .3s}
.hc-let{fill:var(--hc-acc);font:700 22px var(--hc-serif);text-anchor:middle;dominant-baseline:central;opacity:0;transition:opacity .3s}
.hc-masked .hc-ini{opacity:0}.hc-masked .hc-let{opacity:1}
.hc-node .hc-lbl{fill:var(--hc-fg);font-size:14px;transition:opacity .3s}.hc-node .hc-sub{fill:var(--hc-mut);font:12px var(--hc-mono);transition:opacity .3s}
.hc-node .hc-tk{fill:var(--hc-ans);font:11px var(--hc-mono)}
.hc-masked .hc-lbl,.hc-masked .hc-sub{opacity:.15}
.hc-stance{font-size:12.5px;font-weight:600;fill:var(--hc-mut);transition:fill .4s}
.hc-answered .hc-stance{fill:var(--hc-fg2)}.hc-agree .hc-stance{fill:var(--hc-ok)}.hc-dissent .hc-stance{fill:var(--hc-warn)}.hc-failed .hc-stance{fill:var(--hc-bad)}
.hc-chair .hc-disc{fill:color-mix(in srgb,var(--hc-acc) 12%,var(--hc-panel));stroke:var(--hc-acc)}
.hc-chair .hc-ring{stroke:var(--hc-acc)}.hc-chair.hc-thinking .hc-ring{animation:hc-march 1s linear infinite}
.hc-chair.hc-glow .hc-disc{animation:hc-glow2 1.2s ease-in-out infinite}
.hc-chair .hc-lbl{font-size:15px}.hc-chair.hc-decided .hc-disc{stroke:var(--hc-ok);fill:color-mix(in srgb,var(--hc-ok) 15%,var(--hc-panel))}
.hc-chair .hc-tk{fill:var(--hc-mut)}
@keyframes hc-glow2{50%{fill:color-mix(in srgb,var(--hc-acc) 35%,var(--hc-panel))}}
@keyframes hc-march{to{stroke-dashoffset:-28}}
.hc-ripple{fill:none;stroke:var(--hc-ok);stroke-width:2;transform-box:fill-box;transform-origin:center;opacity:0;animation:hc-ripple 1.6s ease-out forwards}
@keyframes hc-ripple{from{transform:scale(1);opacity:.9}to{transform:scale(1.9,2.6);opacity:0}}
.hc-fx-ballot{fill:var(--hc-vote);filter:drop-shadow(0 0 3px color-mix(in srgb,var(--hc-vote) 80%,transparent))}
.hc-fx-spark{fill:var(--hc-deb);filter:drop-shadow(0 0 4px color-mix(in srgb,var(--hc-deb) 90%,transparent))}
.hc-fx-out{fill:var(--hc-acc);filter:drop-shadow(0 0 4px color-mix(in srgb,var(--hc-acc) 80%,transparent))}
.hc-root[data-scheme=light] .hc-beam,.hc-root[data-scheme=light] .hc-fx-ballot,.hc-root[data-scheme=light] .hc-fx-spark,.hc-root[data-scheme=light] .hc-fx-out{filter:none}
.hc-lanes{position:relative;padding:4px 16px 10px}
.hc-axis{display:grid;grid-template-columns:170px 1fr 72px;font:10.5px var(--hc-mono);color:var(--hc-mut);padding:6px 0;border-bottom:1px solid var(--hc-line)}
.hc-ticks{position:relative;height:14px}.hc-ticks span{position:absolute;top:0;white-space:nowrap}.hc-tlim{color:var(--hc-acc)}
.hc-lane{display:grid;grid-template-columns:170px 1fr 72px;align-items:center;height:34px;border-bottom:1px dashed var(--hc-line)}
.hc-who{min-width:0;padding-right:8px}.hc-who b{display:block;font-size:12px;font-weight:600;line-height:1.2}
.hc-live{display:block;font:10.5px var(--hc-mono);color:var(--hc-ans);height:14px}
.hc-rail{position:relative;height:12px}
.hc-rail i{position:absolute;top:0;height:100%;border-radius:3px;min-width:2px;transition:left .6s ease,width .6s ease}
.hc-rail i.hc-running{transition:left .6s ease,width .5s linear}
.hc-rail i.hc-running{background-image:linear-gradient(90deg,transparent 60%,color-mix(in srgb,#fff 35%,transparent));background-blend-mode:overlay}
.hc-a{background:var(--hc-ans)}.hc-v{background:var(--hc-vote)}.hc-d{background:var(--hc-deb)}.hc-web{background:var(--hc-web)}.hc-c{background:var(--hc-acc)}
.hc-tok{font:11px var(--hc-mono);color:var(--hc-mut);text-align:right}
.hc-nowline{position:absolute;top:32px;bottom:10px;width:1px;background:var(--hc-acc);opacity:.7;transition:left .3s linear;pointer-events:none}
.hc-legend{display:flex;gap:14px;padding:0 16px 12px;font-size:11px;color:var(--hc-mut)}
.hc-legend i{display:inline-block;width:10px;height:6px;border-radius:2px;margin-right:5px;vertical-align:1px}
.hc-log{font:11.5px/1.65 var(--hc-mono);height:330px;overflow:auto;padding:10px 16px}
.hc-log>div{white-space:pre-wrap;animation:hc-fade .25s}
.hc-lt{color:var(--hc-mut)}.hc-lk{color:var(--hc-acc)}.hc-lok{color:var(--hc-ok)}.hc-lw{color:var(--hc-warn)}.hc-lr{color:var(--hc-bad)}.hc-ltt{color:var(--hc-fg2)}
.hc-log q{display:block;color:var(--hc-mut);font-family:inherit;font-family:system-ui,sans-serif;font-style:italic;quotes:none;padding-left:46px;font-size:12px;line-height:1.4;margin-bottom:3px}
.hc-matrix{display:grid;gap:3px;padding:12px 16px 14px;font:11px var(--hc-mono)}
.hc-mh{color:var(--hc-mut);display:grid;place-items:center}
.hc-mc{height:24px;border-radius:4px;background:var(--hc-panel2);display:grid;place-items:center;color:var(--hc-fg);transition:background .3s,transform .3s}
.hc-mc.hc-min{animation:hc-cell .35s ease}
@keyframes hc-cell{50%{transform:scale(1.1)}}
.hc-mself{outline:1.5px dashed var(--hc-mut);outline-offset:-2px}
/* decision */
.hc-ruling{display:grid;grid-template-columns:minmax(0,1fr) 230px;gap:28px;padding:26px 30px;position:relative;overflow:hidden}
.hc-ruling:before{content:"";position:absolute;inset:0;background:radial-gradient(620px 230px at 80% -20%,color-mix(in srgb,var(--hc-acc) 14%,transparent),transparent);pointer-events:none}
.hc-ruling .hc-h1{font-size:30px;margin:10px 0 12px}
.hc-bl{font:16.5px/1.6 var(--hc-serif);color:var(--hc-fg2);max-width:900px;margin:0}
.hc-actions2{display:flex;gap:8px;margin-top:18px;flex-wrap:wrap;align-items:center;position:relative}
.hc-confbox{display:flex;flex-direction:column;justify-content:center;gap:8px;border-left:1px solid var(--hc-line);padding-left:24px;position:relative}
.hc-scale{display:flex;gap:5px}.hc-scale i{flex:1;height:8px;border-radius:2px;background:var(--hc-panel2)}.hc-scale i.hc-on{background:var(--hc-acc)}
.hc-confbox b{font:24px var(--hc-serif);font-weight:400}
.hc-kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-top:14px}
.hc-kpi{padding:11px 15px}.hc-kpi span{font-size:11px;color:var(--hc-mut);text-transform:uppercase;letter-spacing:.09em}.hc-kpi b{display:block;font:20px var(--hc-mono);margin-top:3px;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hc-kpi b small{font-size:12px;color:var(--hc-mut)}
.hc-three{display:grid;grid-template-columns:1fr 1.35fr 1fr;gap:14px;margin-top:14px;align-items:stretch}
.hc-ol{margin:0;padding-left:18px}.hc-ol li{margin:6px 0}
.hc-lower{display:grid;grid-template-columns:280px minmax(0,1fr) minmax(0,1.3fr);gap:14px;margin-top:14px;align-items:start}
.hc-legend2{display:flex;gap:12px;font-size:12px;color:var(--hc-mut);padding:0 16px 14px;flex-wrap:wrap}
.hc-legend2 i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
.hc-rbars{padding-bottom:4px}
.hc-rrowb{display:grid;grid-template-columns:140px 1fr 34px;gap:10px;align-items:center;padding:4px 0;font-size:13px}
.hc-rbar{height:9px;border-radius:3px;background:var(--hc-panel2);overflow:hidden}.hc-rbar i{display:block;height:100%;background:var(--hc-vote)}
.hc-tabs2{display:flex;gap:2px;border-bottom:1px solid var(--hc-line);padding:0 10px;flex-wrap:wrap}
.hc-tab2{background:none;border:0;padding:10px;color:var(--hc-mut)!important;border-bottom:2px solid transparent;margin-bottom:-1px;cursor:pointer}
.hc-tab2.hc-on{color:var(--hc-fg)!important;border-color:var(--hc-acc)}
.hc-tabbody{padding:12px 16px;min-height:260px;max-height:620px;overflow:auto}
.hc-ansrow{display:grid;grid-template-columns:28px 150px 1fr;gap:10px;padding:9px 0;border:0;border-bottom:1px solid var(--hc-line);font-size:13px;width:100%;text-align:left;background:none;cursor:pointer}
.hc-ansrow:hover:not(.hc-static){background:var(--hc-panel2)}.hc-ansrow.hc-static{cursor:default}
.hc-ansrow:last-child{border-bottom:0}
.hc-rk{font:20px var(--hc-serif);color:var(--hc-acc)}
.hc-ansopen{display:flex;flex-direction:column;gap:10px;padding:10px 0 14px;border-bottom:1px solid var(--hc-line)}
.hc-dissent{border-left:3px solid var(--hc-warn);padding:6px 0 6px 16px;font:16px/1.55 var(--hc-serif);margin:0}
.hc-waiting{padding:40px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center}
@media (max-width:1320px){.hc-conv{grid-template-columns:240px minmax(0,1fr)}.hc-col-r{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr}.hc-lower{grid-template-columns:260px minmax(0,1fr)}.hc-lower>:last-child{grid-column:1/-1}.hc-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:1060px){.hc-brief,.hc-conv,.hc-three,.hc-lower,.hc-ruling{grid-template-columns:minmax(0,1fr)}.hc-col-r{grid-template-columns:1fr}.hc-confbox{border-left:0;padding-left:0}.hc-row2,.hc-rosterhead{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){.hc-root *{animation-duration:.01s!important;transition-duration:.01s!important}}
.hc-root *{box-sizing:border-box}
.hc-root button{font:inherit;color:inherit}
.hc-muted{color:var(--ui-text-tertiary)}.hc-sm{font-size:12.5px}.hc-xs{font-size:11.5px}
.hc-row{display:flex;align-items:center;gap:8px}.hc-wrap{flex-wrap:wrap}.hc-grow{flex:1}.hc-center{justify-content:center;text-align:center}
.hc-ellipsis{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.hc-card{background:var(--ui-bg-secondary);border:1px solid var(--ui-stroke-secondary);border-radius:14px}
.hc-pad{padding:12px 14px}
.hc-lbl{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ui-text-tertiary);font-weight:650;margin-bottom:6px}
.hc-flbl{font-size:11.5px;color:var(--ui-text-tertiary);margin-bottom:4px}
.hc-link{background:none;border:0;padding:2px 4px;cursor:pointer;color:var(--ui-accent)!important;font-size:12.5px}
.hc-link:hover{text-decoration:underline}
.hc-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:99px;border:1px solid var(--ui-stroke-secondary);background:transparent;font-size:12px;cursor:pointer;white-space:nowrap}
.hc-chip:hover{border-color:color-mix(in srgb,var(--ui-accent) 60%,var(--ui-stroke-secondary))}
.hc-chip.hc-on{border-color:var(--ui-accent);color:var(--ui-accent)!important;background:color-mix(in srgb,var(--ui-accent) 11%,transparent)}
.hc-chip.hc-static{cursor:default}
.hc-chip-sm{font-size:11px;padding:2px 8px}
.hc-pulse{width:7px;height:7px;border-radius:50%;background:currentColor;animation:hc-pulse 1.2s ease-in-out infinite}
@keyframes hc-pulse{50%{opacity:.25}}
.hc-dot{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block}.hc-dot-lg{width:11px;height:11px}
.hc-in{font:inherit;font-size:12.5px;color:var(--ui-text-primary);background:color-mix(in srgb,var(--ui-text-primary) 3%,transparent);border:1px solid var(--ui-stroke-secondary);border-radius:9px;padding:7px 10px;width:100%;resize:vertical}
.hc-in:focus,.hc-q:focus{outline:none;border-color:var(--ui-accent)}
.hc-guide{padding:14px 16px;margin-bottom:12px;background:linear-gradient(135deg,color-mix(in srgb,var(--ui-accent) 10%,var(--ui-bg-secondary)),var(--ui-bg-secondary))}
.hc-q{font:inherit;font-size:15.5px;width:100%;resize:vertical;border:1px solid transparent;border-radius:9px;background:transparent;color:var(--ui-text-primary);padding:6px 2px;margin:4px 0 6px}
.hc-qsmall{font-size:12.5px;color:var(--ui-text-tertiary);margin-top:6px}
.hc-bg{margin-top:4px;font-size:12px;color:var(--hc-mut)}
.hc-bg summary{cursor:pointer;width:max-content}
.hc-bg>div{margin-top:4px;padding:6px 10px;border-left:2px solid var(--hc-line);white-space:pre-wrap;color:var(--hc-fg2)}
.hc-dlgfoot{position:sticky;bottom:-18px;margin:12px -20px -18px;padding:10px 20px 14px;background:var(--hc-solid);border-top:1px solid var(--ui-border,rgba(127,127,127,.25))}
.hc-runtitle{font-size:13px;font-weight:600;color:var(--ui-text-secondary,inherit);margin-top:6px}
.hc-av{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-weight:800;font-size:12.5px;color:#fff;flex:none}
.hc-rrow{display:flex;align-items:center;gap:8px;padding:7px 4px;border:0;border-top:1px solid var(--ui-stroke-secondary);background:none;cursor:pointer;text-align:left;font-size:12.5px;border-radius:4px}
.hc-rrow.hc-on{background:color-mix(in srgb,var(--ui-accent) 12%,transparent)}
.hc-rrow:hover{background:color-mix(in srgb,var(--ui-accent) 8%,transparent)}
.hc-picker{position:relative}
.hc-pbtn{width:100%;display:flex;align-items:center;gap:9px;padding:9px 11px;border-radius:10px;border:1px solid var(--ui-stroke-secondary);background:color-mix(in srgb,var(--ui-text-primary) 3%,transparent);cursor:pointer;text-align:left}
.hc-pbtn:hover{border-color:var(--ui-accent)}
.hc-pop{position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:20;background:var(--hc-solid);border:1px solid var(--ui-stroke-secondary);border-radius:11px;box-shadow:0 14px 34px rgba(0,0,0,.4);max-height:340px;overflow:auto;padding:6px}
.hc-pg{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--ui-text-tertiary);padding:9px 8px 3px;display:flex;align-items:center;gap:6px}
.hc-pi{padding:5px 8px 5px 22px;border-radius:7px;cursor:pointer;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hc-pi:hover{background:color-mix(in srgb,var(--ui-accent) 13%,transparent)}
.hc-pi.hc-cur{color:var(--ui-accent);font-weight:650}
.hc-more{color:var(--ui-accent);font-size:11.5px}
.hc-lock{font-size:10px;padding:0 6px;border-radius:99px;border:1px solid var(--ui-stroke-secondary);text-transform:none;letter-spacing:0}
.hc-starters{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px}
.hc-ev{margin-top:10px;padding:10px 12px;border:1px dashed var(--ui-stroke-secondary);border-radius:11px;background:var(--hc-solid)}
.hc-ev-drag{border-color:var(--ui-accent);border-style:solid;box-shadow:0 0 0 3px color-mix(in srgb,var(--ui-accent) 25%,transparent)}
.hc-ev-hint{margin-top:6px;line-height:1.5}
.hc-evlist{margin-top:8px;display:flex;flex-direction:column;gap:2px;max-height:230px;overflow:auto}
.hc-evrow{display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:7px;min-width:0}
.hc-evrow:hover{background:var(--ui-bg-secondary)}
.hc-evname{max-width:260px;font-size:12.5px}
.hc-evoff{opacity:.5}.hc-evbad .hc-evname{color:var(--ui-text-secondary)}
.hc-evsp{width:13px;flex:none}
.hc-evrow input{margin:0;width:13px;height:13px;flex:none}
.hc-evsum{margin-top:8px;line-height:1.55;color:var(--ui-text-secondary)}
.hc-tl{position:relative;display:inline-flex}
.hc-tlpop{position:absolute;top:auto;right:auto;bottom:calc(100% + 8px);left:0;width:340px;max-height:min(70vh,560px);overflow-y:auto;padding:12px 14px;z-index:30}
.hc-chip-off{opacity:.75;border-style:dashed}
.hc-warn-t{color:var(--ui-yellow,#d29922)}
.hc-bad{color:var(--dt-destructive,#e5534b)}
.hc-gate{margin-top:12px;padding:12px 14px;border-color:color-mix(in srgb,var(--ui-yellow,#d29922) 55%,transparent)}
.hc-cmd{display:flex;align-items:center;gap:8px;margin-top:6px}
.hc-cmd code{flex:1;font-size:11.5px;padding:7px 9px;border-radius:7px;background:color-mix(in srgb,var(--ui-text-primary) 6%,transparent);overflow:auto;white-space:nowrap}
.hc-insight{display:flex;gap:9px;padding:9px 11px;border-radius:10px;background:color-mix(in srgb,var(--ui-accent) 8%,transparent);font-size:12.5px}
.hc-lead{font-size:13.5px}
.hc-list{margin:0;padding-left:18px}.hc-list li{margin:3px 0}
.hc-answer{padding:12px 14px;border-radius:10px;border:1px solid var(--ui-stroke-secondary);max-height:460px;overflow:auto}
.hc-md-h{font-weight:650;margin:10px 0 4px}.hc-md-p{margin:5px 0}
.hc-tabs{display:flex;gap:2px;border-bottom:1px solid var(--ui-stroke-secondary);margin:16px 0 12px;flex-wrap:wrap}
.hc-tab{padding:8px 13px;cursor:pointer;color:var(--ui-text-tertiary)!important;border:0;border-bottom:2px solid transparent;background:none;margin-bottom:-1px}
.hc-tab.hc-on{color:var(--ui-text-primary)!important;border-bottom-color:var(--ui-accent)}
.hc-grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hc-mx{border-collapse:separate;border-spacing:3px;font-size:12px}
.hc-mx td,.hc-mx th{width:42px;height:30px;text-align:center;border-radius:6px}
.hc-mx th{color:var(--ui-text-tertiary);font-weight:600}
.hc-self{outline:1.5px dashed var(--ui-text-secondary);outline-offset:-2px}
.hc-warn{padding:8px 10px;border-radius:9px;font-size:12px;background:color-mix(in srgb,var(--ui-yellow,#c08532) 13%,transparent)}
.hc-hist{position:relative}
.hc-hpop{position:absolute;right:0;top:calc(100% + 6px);width:460px;z-index:30;background:var(--hc-solid);border:1px solid var(--ui-stroke-secondary);border-radius:12px;box-shadow:0 14px 34px rgba(0,0,0,.4);padding:8px;display:flex;flex-direction:column;gap:6px}
.hc-hlist{max-height:360px;overflow:auto}
.hc-hrow{display:block;width:100%;text-align:left;padding:8px 10px;border-radius:8px;border:0;background:none;cursor:pointer}
.hc-hrow:hover{background:color-mix(in srgb,var(--ui-accent) 10%,transparent)}
.hc-overlay{position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.45);display:grid;place-items:center;padding:30px}
.hc-help{background:linear-gradient(var(--ui-bg-secondary),var(--ui-bg-secondary)),var(--hc-solid);width:min(720px,100%);max-height:85vh;overflow:auto;padding:18px 20px}
.hc-help-sec{margin-top:14px}.hc-help-sec p{margin:4px 0 6px}
.hc-stand{display:grid;grid-template-columns:20px minmax(0,1fr);gap:8px;margin:10px 0}
.hc-bar{height:7px;border-radius:4px;background:var(--ui-stroke-secondary);position:relative;overflow:hidden;margin:4px 0 3px}
.hc-bar i{position:absolute;inset:0 auto 0 0;border-radius:4px}
.hc-spinner{width:34px;height:34px;border-radius:50%;border:3px solid var(--ui-stroke-secondary);border-top-color:var(--ui-accent);animation:hc-spin .9s linear infinite}
@keyframes hc-spin{to{transform:rotate(360deg)}}
.hc-skel{height:420px;border-radius:14px}.hc-skel-sm{height:160px}
.hc-gsteps{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:10px}
.hc-gstep{display:flex;gap:10px}
.hc-gnum{width:26px;height:26px;border-radius:50%;flex:none;display:grid;place-items:center;font-weight:800;background:var(--hc-acc);color:var(--hc-on-acc)}
.hc-guide{margin-bottom:14px}
@media (max-width:980px){.hc-grid2,.hc-gsteps{grid-template-columns:1fr}}
/* the table (chamber glow) */
.hc-stagewrap2{display:flex;flex-direction:column;gap:0}
.hc-evbox2{position:relative;border:1px solid var(--hc-line);background:var(--hc-panel2);border-radius:14px;padding:10px 14px 12px;display:flex;flex-direction:column;gap:8px;transition:border-color .3s,box-shadow .3s;z-index:1}
.hc-evbox2.hc-sending{border-color:var(--hc-web);box-shadow:0 0 22px -6px color-mix(in srgb,var(--hc-web) 60%,transparent)}
.hc-evhead{display:flex;align-items:baseline;gap:10px;font-size:12.5px;color:var(--hc-mut)}
.hc-evhead b{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--hc-fg2);font-weight:650}
.hc-evcount{margin-left:auto;color:var(--hc-fg2)}
.hc-evrow{display:flex;flex-wrap:wrap;gap:7px;min-height:28px;align-items:center}
.hc-evnote{font-size:13px;color:var(--hc-mut)}
.hc-evsearch .hc-evnote{animation:hc-pulse 1.6s ease-in-out infinite}
.hc-ep{display:inline-flex;align-items:center;gap:7px;padding:4px 11px 4px 9px;border-radius:99px;border:1px solid color-mix(in srgb,var(--hc-ans) 60%,transparent);background:var(--hc-panel);font-size:13px;color:var(--hc-fg);max-width:260px;animation:hc-epin .45s ease both;cursor:default}
.hc-ep b{font:700 12px var(--hc-mono);color:var(--hc-ans)}
.hc-ep span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hc-ep::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--hc-ans);flex:none}
.hc-ep-web{border-color:color-mix(in srgb,var(--hc-web) 65%,transparent)}.hc-ep-web b{color:var(--hc-web)}.hc-ep-web::before{background:var(--hc-web)}
.hc-ep-more{border-style:dashed;color:var(--hc-mut)}.hc-ep-more::before{display:none}
@keyframes hc-epin{from{opacity:0;transform:translateY(8px) scale(.92)}}
.hc-evout{position:absolute;left:50%;bottom:-6px;width:11px;height:11px;margin-left:-5.5px;border-radius:50%;background:var(--hc-panel2);border:1.5px solid var(--hc-line2);transition:background .3s,border-color .3s,box-shadow .3s}
.hc-evbox2.hc-sending .hc-evout{background:var(--hc-web);border-color:var(--hc-web);box-shadow:0 0 10px var(--hc-web)}
.hc-table text{paint-order:stroke}
.hc-guide{fill:none;stroke:var(--hc-line2);stroke-dasharray:2 7}
.hc-feed{fill:none;stroke:var(--hc-line2);stroke-width:1.5;stroke-dasharray:2 5;opacity:.75;transition:stroke .3s,opacity .3s}
.hc-feed-chair{opacity:.45}
.hc-outlet{fill:var(--hc-panel2);stroke:var(--hc-line2);stroke-width:1.5;opacity:0}
.hc-feeds.hc-sending .hc-feed{stroke:var(--hc-web);opacity:.9}
.hc-tspoke{fill:none;stroke:var(--hc-line2);stroke-width:1.6;opacity:.75}
.hc-tenergy{fill:none;stroke:var(--hc-ans);stroke-width:2.4;stroke-linecap:round;stroke-dasharray:3 14;animation:hc-energy 1s linear infinite}
.hc-tenergy.hc-e-vote{stroke:var(--hc-vote)}.hc-tenergy.hc-e-deb{stroke:var(--hc-deb)}
@keyframes hc-energy{to{stroke-dashoffset:-34}}
.hc-debarc{fill:none;stroke:var(--hc-deb);stroke-opacity:.45;stroke-width:1.6;stroke-dasharray:4 6}
.hc-tseat{--sc:var(--hc-line2)}
.hc-tseat.hc-answered{--sc:var(--hc-acc)}.hc-tseat.hc-thinking{--sc:var(--hc-ans)}.hc-tseat.hc-voting{--sc:var(--hc-vote)}.hc-tseat.hc-debating{--sc:var(--hc-deb)}
.hc-tseat.hc-agree{--sc:var(--hc-ok)}.hc-tseat.hc-dissent{--sc:var(--hc-warn)}.hc-tseat.hc-failed{--sc:var(--hc-bad)}.hc-tseat.hc-waiting,.hc-tseat.hc-idle{--sc:var(--hc-mut)}
.hc-tseat.hc-masked.hc-answered{--sc:var(--hc-vote)}
.hc-podg{transition:transform .35s ease}
.hc-tseat.hc-active .hc-podg{transform:scale(1.07)}
.hc-thalo{fill:var(--sc);opacity:0;transition:opacity .4s}
.hc-tseat.hc-active .hc-thalo{animation:hc-halo 1.3s ease-in-out infinite}
.hc-tseat.hc-agree .hc-thalo,.hc-tseat.hc-dissent .hc-thalo{opacity:.25}
@keyframes hc-halo{0%,100%{opacity:.16}50%{opacity:.36}}
.hc-ttrack{fill:none;stroke:var(--hc-line);stroke-width:3.2}
.hc-tarc{fill:none;stroke:var(--sc);stroke-width:3.6;stroke-linecap:round;transform:rotate(-90deg);transition:stroke-dashoffset .5s linear,stroke .4s}
.hc-tcomet{fill:none;stroke:var(--hc-fg);stroke-opacity:.85;stroke-width:3.6;stroke-linecap:round;animation:hc-spin2 1.6s linear infinite}
.hc-tdisc{fill:var(--hc-panel2);stroke:var(--sc);stroke-width:1.6;transition:stroke .4s}
.hc-tseat .hc-ini{font-size:19px;font-weight:700}
.hc-tseat .hc-let{font-size:26px}
.hc-tseat .hc-lbl{font-size:18px;font-weight:600;stroke:var(--hc-bg);stroke-width:5px;stroke-linejoin:round}
.hc-tseat .hc-sub{font-size:13.5px;stroke:var(--hc-bg);stroke-width:5px;stroke-linejoin:round}
.hc-tseat .hc-stance{font-size:14.5px;font-weight:550;stroke:var(--hc-bg);stroke-width:5px;stroke-linejoin:round;fill:var(--hc-fg2)}
.hc-tseat.hc-agree .hc-stance{fill:var(--hc-ok)}.hc-tseat.hc-dissent .hc-stance{fill:var(--hc-warn)}.hc-tseat.hc-failed .hc-stance{fill:var(--hc-bad)}.hc-tseat.hc-waiting .hc-stance,.hc-tseat.hc-idle .hc-stance{fill:var(--hc-mut)}
.hc-tseat .hc-tk{font-size:13px;fill:var(--hc-mut);stroke:var(--hc-bg);stroke-width:5px;stroke-linejoin:round}
.hc-tseat.hc-waiting,.hc-tseat.hc-idle{opacity:.8}
.hc-bhalo{fill:var(--hc-acc);opacity:0;transition:opacity .4s}
.hc-bench.hc-bwrite .hc-bhalo,.hc-bench.hc-bresearch .hc-bhalo{animation:hc-halo 1.5s ease-in-out infinite}
.hc-bench.hc-bresearch .hc-bhalo{fill:var(--hc-web)}
.hc-bench.hc-bdone .hc-bhalo{opacity:.22;fill:var(--hc-ok)}
.hc-bdisc{fill:var(--hc-panel2);stroke:color-mix(in srgb,var(--hc-acc) 65%,transparent);stroke-width:1.6}
.hc-bench.hc-bresearch .hc-bdisc{stroke:var(--hc-web)}.hc-bench.hc-bdone .hc-bdisc{stroke:var(--hc-ok)}
.hc-bfill{fill:var(--hc-acc);opacity:.16;transition:width .6s linear}
.hc-bench.hc-bdone .hc-bfill{fill:var(--hc-ok)}
.hc-bscan{fill:var(--hc-web);opacity:.45;filter:blur(3px);animation:hc-scan 1.8s ease-in-out infinite alternate}
@keyframes hc-scan{to{transform:translateX(266px)}}
.hc-btitle{font-size:16px;font-weight:650;letter-spacing:.16em;fill:var(--hc-fg);text-anchor:middle}
.hc-bsub{font:13px var(--hc-mono);fill:var(--hc-mut);text-anchor:middle}
.hc-bstat{font-size:14px;fill:var(--hc-fg2);text-anchor:middle;stroke:var(--hc-bg);stroke-width:5px;stroke-linejoin:round}
.hc-bverdict{font:italic 17px var(--hc-serif);fill:var(--hc-fg);text-anchor:middle;stroke:var(--hc-bg);stroke-width:5px;stroke-linejoin:round;animation:hc-fade .8s ease both}
.hc-tripple{fill:none;stroke:var(--hc-acc);stroke-width:1.6;opacity:0;transform-box:fill-box;transform-origin:center;animation:hc-tripple 1.8s ease-out forwards}
@keyframes hc-tripple{from{transform:scale(1);opacity:.9}to{transform:scale(2.6,3.4);opacity:0}}
.hc-bubble rect{fill:var(--hc-panel);stroke:color-mix(in srgb,var(--hc-ans) 60%,transparent)}
.hc-bubble text{font-size:14px;fill:var(--hc-fg)}
.hc-bubble{animation:hc-bub 3.8s ease both}
@keyframes hc-bub{0%{opacity:0;transform:translateY(6px)}10%,85%{opacity:1;transform:none}100%{opacity:0}}
.hc-fx-ev{fill:var(--hc-web);filter:drop-shadow(0 0 4px color-mix(in srgb,var(--hc-web) 80%,transparent))}
.hc-fx-ev2{fill:var(--hc-ans)}
.hc-fx-web{fill:var(--hc-web);filter:drop-shadow(0 0 6px var(--hc-web))}
.hc-fx-ans{fill:var(--hc-ans);filter:drop-shadow(0 0 5px var(--hc-ans))}
.hc-fx-ok{fill:var(--hc-ok);filter:drop-shadow(0 0 6px var(--hc-ok))}.hc-fx-warn{fill:var(--hc-warn);filter:drop-shadow(0 0 6px var(--hc-warn))}
.hc-root[data-scheme=light] :is(.hc-fx-ev,.hc-fx-web,.hc-fx-ans,.hc-fx-ok,.hc-fx-warn){filter:none}
.hc-root[data-scheme=light] .hc-thalo,.hc-root[data-scheme=light] .hc-bhalo{filter:none}
.hc-root[data-scheme=light] .hc-tseat.hc-active .hc-thalo{animation:none;opacity:.12}
.hc-node.hc-waiting{opacity:.8}

/* buttons: one outlined-glow system. main · secondary · tinted · quiet · danger · chips */
.hc-root{--hc-fg2:color-mix(in srgb,var(--hc-fg) 82%,var(--hc-mut))}
.hc-convene,.hc-btn2,.hc-stop,.hc-link,.hc-b{position:relative;overflow:hidden;display:inline-flex;align-items:center;justify-content:center;gap:8px;border-radius:12px;font-weight:550;cursor:pointer;white-space:nowrap;outline:none;transition:border-color .2s,box-shadow .2s,background .2s,color .2s,transform .08s}
:is(.hc-convene,.hc-btn2,.hc-stop,.hc-link,.hc-b,.hc-chip,.hc-pbtn,.hc-step2,.hc-tab2):focus-visible{outline:none;box-shadow:0 0 0 2px var(--hc-bg),0 0 0 4px color-mix(in srgb,var(--hc-acc) 70%,transparent)!important}
:is(.hc-convene,.hc-btn2,.hc-stop,.hc-link,.hc-b,.hc-chip):active:not(:disabled){transform:translateY(1px)}
.hc-convene{padding:11px 22px;border:1px solid color-mix(in srgb,var(--hc-acc) 80%,transparent);color:var(--hc-fg)!important;
  background:linear-gradient(180deg,color-mix(in srgb,var(--hc-acc) 32%,transparent),color-mix(in srgb,var(--hc-acc) 15%,transparent));
  box-shadow:0 0 0 1px color-mix(in srgb,var(--hc-acc) 25%,transparent),0 0 24px -6px color-mix(in srgb,var(--hc-acc) 70%,transparent)}
.hc-convene::after{content:"";position:absolute;inset:0;background:linear-gradient(105deg,transparent 35%,rgba(255,255,255,.2) 50%,transparent 65%);transform:translateX(-120%);transition:transform .6s;pointer-events:none}
.hc-convene:hover:not(:disabled){transform:none;box-shadow:0 0 0 1px color-mix(in srgb,var(--hc-acc) 45%,transparent),0 0 34px -4px color-mix(in srgb,var(--hc-acc) 85%,transparent)}
.hc-convene:hover:not(:disabled)::after{transform:translateX(120%)}
.hc-bic{font-size:9px;width:20px;height:20px;border-radius:50%;display:grid;place-items:center;border:1px solid currentColor;padding-left:2px}
.hc-convene:hover:not(:disabled) .hc-bic{animation:hc-bpulse 1s ease infinite}
@keyframes hc-bpulse{50%{box-shadow:0 0 0 4px color-mix(in srgb,var(--hc-acc) 30%,transparent)}}
.hc-root[data-scheme=light] .hc-convene{box-shadow:0 0 0 1px color-mix(in srgb,var(--hc-acc) 20%,transparent),0 6px 18px -8px color-mix(in srgb,var(--hc-acc) 60%,transparent)}
.hc-btn2,.hc-b-secondary{padding:7px 13px;background:transparent;border:1px solid var(--hc-line2);color:var(--hc-fg)!important}
:is(.hc-btn2,.hc-b-secondary):hover:not(:disabled){border-color:color-mix(in srgb,var(--hc-acc) 70%,transparent);box-shadow:0 0 14px -6px var(--hc-acc)}
.hc-b-sm,.hc-btn-sm{padding:5px 11px;font-size:12px;border-radius:10px}
.hc-link,.hc-b-quiet{padding:5px 9px;border-radius:10px;background:transparent;border:1px solid transparent;color:var(--hc-fg2)!important;font-size:12.5px}
:is(.hc-link,.hc-b-quiet):hover:not(:disabled){color:var(--hc-fg)!important;border-color:var(--hc-line);text-decoration:none}
.hc-link.hc-tint{border-color:color-mix(in srgb,var(--hc-acc) 45%,transparent);color:color-mix(in srgb,var(--hc-acc) 80%,var(--hc-fg))!important}
.hc-link.hc-tint:hover:not(:disabled){background:color-mix(in srgb,var(--hc-acc) 12%,transparent);border-color:color-mix(in srgb,var(--hc-acc) 65%,transparent)}
.hc-stop,.hc-b-danger,.hc-btn2.hc-danger{padding:7px 13px;background:transparent;border:1px solid color-mix(in srgb,var(--hc-bad) 55%,transparent);color:var(--hc-bad)!important}
.hc-link.hc-bad{color:var(--hc-bad)!important}
:is(.hc-stop,.hc-b-danger,.hc-btn2.hc-danger,.hc-link.hc-bad):hover:not(:disabled){border-color:color-mix(in srgb,var(--hc-bad) 60%,transparent);background:color-mix(in srgb,var(--hc-bad) 10%,transparent);box-shadow:0 0 18px -6px var(--hc-bad)}
:is(.hc-convene,.hc-btn2,.hc-stop,.hc-b):disabled:not(.hc-busy){opacity:1;cursor:not-allowed;background:color-mix(in srgb,var(--hc-fg) 6%,var(--hc-panel))!important;color:var(--hc-mut)!important;border-color:var(--hc-line)!important;box-shadow:none!important}
.hc-link:disabled{opacity:1;color:var(--hc-mut)!important;cursor:not-allowed}
.hc-convene.hc-busy{cursor:progress;opacity:1}
.hc-bspin{width:13px;height:13px;border-radius:50%;border:2px solid currentColor;border-right-color:transparent;animation:hc-bspin .7s linear infinite}
@keyframes hc-bspin{to{transform:rotate(360deg)}}
.hc-chip{border-color:var(--hc-line);color:var(--hc-mut)!important;transition:border-color .2s,box-shadow .2s,color .2s}
.hc-chip:hover:not(.hc-static){color:var(--hc-fg)!important;border-color:var(--hc-line2)}
.hc-chip.hc-on{color:var(--hc-fg)!important;border-color:var(--hc-acc);background:color-mix(in srgb,var(--hc-acc) 10%,transparent);box-shadow:0 0 12px -5px var(--hc-acc),inset 0 0 0 1px color-mix(in srgb,var(--hc-acc) 40%,transparent)}
.hc-chip.hc-static{color:var(--hc-fg2)!important}
.hc-pbtn{transition:border-color .2s,box-shadow .2s}.hc-pbtn:hover{border-color:color-mix(in srgb,var(--hc-acc) 70%,transparent);box-shadow:0 0 14px -6px var(--hc-acc)}
.hc-step2.hc-on{box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--hc-acc) 35%,transparent)}
@media (prefers-reduced-motion: reduce){.hc-convene::after,.hc-epill{animation:none!important;transition:none!important}.hc-convene:hover .hc-bic{animation:none}}

`

export default {
  id: PLUGIN_ID,
  name: 'Hermes Committee',
  description: 'The Committee page: set up a committee of AI models, follow it live, and read the verdict.',
  register(ctx) {
    CTX = ctx
    ctx.onEvent(`plugin.${PLUGIN_ID}.run.updated`, ({ payload }) => {
      queryClient.invalidateQueries({ queryKey: [...KEY, 'runs'] })
      if (payload?.run_id) queryClient.invalidateQueries({ queryKey: [...KEY, 'run', payload.run_id] })
      if (payload?.status === 'done') queryClient.invalidateQueries({ queryKey: [...KEY, 'standings'] })
    })
    ctx.onEvent(`plugin.${PLUGIN_ID}.settings.changed`, () => queryClient.invalidateQueries({ queryKey: [...KEY, 'setup'] }))
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => h(CommitteePage) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 56, data: { path: ROUTE, label: 'Committee', codicon: 'law' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'hermes-committee.open', label: 'Committee: New committee', keywords: ['council', 'committee', 'llm council', 'verdict', 'board', 'ranking'], run: () => host.navigate(ROUTE) } }
    ])
  }
}
