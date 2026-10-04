// Helpers for the ephemeral script of the qa behavior (PLN0116). The script lives in the session's scratchpad, is never
// committed, and runs with the line that `qa_env.py up` prints (RODAR: QA_URL=... NODE_PATH=<node_modules of the
// worktree> node <script>), so `require('@playwright/test')` and `require('@axe-core/playwright')` come from the
// branch under test. Playwright is required lazily: guard() and config() work without it.
//
//   const kit = require(process.env.QA_KIT)
//   const qa = kit.config()                        // url, widths, minTarget, axeTags, served (refuses the served app)
//   const r = kit.report(__dirname)                // r.check(label, ok, detail); r.finish() writes resultado.json
//   const browser = await kit.launch()
//   for (const w of qa.widths) {
//     const page = await kit.pageAt(browser, w, {}, r)   // records the video only at qa.videoWidth (one width, 1360 by default)
//     try { ...; await kit.screens(page, r, `tela-${w}`, qa) } finally { await kit.closePage(page, r) }   // saves roteiro-<w>.webm
//   }
'use strict'
const fs = require('node:fs')
const path = require('node:path')

// Same ceiling as the video `attach` of watch_core (planou.py VIDEO_KINDS, PLN0302). Playwright's default recording scales
// the page down to fit 800x800 (VP8): about 0.75 MB a minute at 1360 px, so a short script stays far below it.
const VIDEO_MAX_BYTES = 50 * 1024 * 1024

function guard(url, served) {
  if (!/^https?:\/\//i.test(url || '')) throw new Error(`QA_URL precisa ser http(s): ${JSON.stringify(url)}`)
  for (const p of served || []) if (new RegExp(p, 'i').test(url)) throw new Error(`${url} e o app servido: recusado`)
  return url
}

function config(env = process.env) {
  const list = (v, d) => (v ? v.split(',').map((x) => x.trim()).filter(Boolean) : d)
  const served = env.QA_SERVED ? JSON.parse(env.QA_SERVED) : []
  const widths = list(env.QA_WIDTHS, ['1360', '834', '390']).map(Number)
  if (widths.some((w) => !Number.isInteger(w) || w <= 0)) throw new Error(`QA_WIDTHS invalido: ${env.QA_WIDTHS}`)
  const videoWidth = env.QA_VIDEO_WIDTH ? Number(env.QA_VIDEO_WIDTH) : defaultVideoWidth(widths)
  if (!Number.isInteger(videoWidth) || videoWidth < 0) throw new Error(`QA_VIDEO_WIDTH invalido: ${env.QA_VIDEO_WIDTH}`)
  return {
    url: guard(env.QA_URL, served),
    widths,
    minTarget: Number(env.QA_MIN_TARGET || 44),
    axeTags: list(env.QA_AXE_TAGS, ['wcag2a', 'wcag2aa', 'wcag21aa']),
    served,
    videoWidth: widths.includes(videoWidth) ? videoWidth : 0,   // 0 = no video
  }
}

/** The one width whose run is recorded: 1360 when tested, else the largest. */
function defaultVideoWidth(widths) {
  return widths.includes(1360) ? 1360 : Math.max(...widths)
}

/** What happens to a recorded video of `bytes`: kept for the explicit attach, or dropped over the attach ceiling. */
function videoVerdict(bytes, max = VIDEO_MAX_BYTES) {
  const mb = (bytes / 1024 / 1024).toFixed(1)
  if (!bytes) return { keep: false, reason: 'video vazio' }
  if (bytes > max) return { keep: false, reason: `${mb} MB passa do limite de ${Math.round(max / 1024 / 1024)} MB do anexo` }
  return { keep: true, mb }
}

async function launch() {
  const { chromium } = require('@playwright/test')
  return chromium.launch()
}

/** A page at one width: touch below 1024 px, mobile below 600 px (the phone), pt-BR. With the report `r` and the width
 *  equal to the config's videoWidth, the context records a video (Playwright's default size, at most 800 px), saved by
 *  closePage(). Only that one width records. */
async function pageAt(browser, width, extra = {}, r = null) {
  const qa = config()
  const touch = width < 1024
  const record = r && qa.videoWidth && width === qa.videoWidth
  const ctx = await browser.newContext({
    baseURL: qa.url, viewport: { width, height: width < 600 ? 1400 : 1000 }, hasTouch: touch,
    isMobile: touch && width < 600, locale: 'pt-BR',
    ...(record ? { recordVideo: { dir: path.join(r.dir, '.video') } } : {}), ...extra,
  })
  if (record) r.video = { width, pending: true }
  return ctx.newPage()
}

/** Closes the page's context (which finishes its video) and, if it recorded, saves roteiro-<width>.webm next to the
 *  script: kept up to the attach ceiling, deleted above it. The result goes to r.video and to resultado.json; it is never
 *  a check, so a missing or oversized video does not fail the QA. Call it in a finally, before browser.close(). */
async function closePage(page, r = null) {
  const video = page.video && page.video()
  await page.context().close()
  if (!video || !r) return null
  const width = page.viewportSize().width
  const file = path.join(r.dir, `roteiro-${width}.webm`)
  try {
    await video.saveAs(file)
    await video.delete().catch(() => {})
    try { fs.rmdirSync(path.join(r.dir, '.video')) } catch {}   // only when empty
    const v = videoVerdict(fs.statSync(file).size)
    if (!v.keep) fs.rmSync(file, { force: true })
    r.video = v.keep ? { width, file, bytes: fs.statSync(file).size, mb: v.mb } : { width, skipped: v.reason }
  } catch (e) {
    r.video = { width, skipped: `video nao salvo: ${String(e.message || e).split('\n')[0]}` }
  }
  console.log(r.video.file ? `VIDEO ${path.basename(file)} (${r.video.mb} MB)` : `SEM VIDEO: ${r.video.skipped}`)
  return r.video
}

/** Accessibility violations of the page (axe), one short line each. */
async function axe(page, tags) {
  const AxeBuilder = require('@axe-core/playwright').default
  const result = await new AxeBuilder({ page }).withTags(tags).analyze()
  return result.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(' ')).slice(0, 3).join(' | ')}`)
}

/** Visible buttons, links and fields under `min` px tall inside `scope`. */
async function smallTargets(page, min, scope = 'body') {
  return page.locator(scope).first().evaluate((root, min) => {
    const out = []
    root.querySelectorAll('button, a, select, input, textarea, [role="button"]').forEach((el) => {
      const r = el.getBoundingClientRect()
      if (r.width === 0 && r.height === 0) return
      if (r.height < min) out.push(`${el.tagName.toLowerCase()} "${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30)}" ${Math.round(r.width)}x${Math.round(r.height)}`)
    })
    return out
  }, min)
}

/** Horizontal overflow of the page, in px (0 = none). */
async function overflow(page) {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
}

/** Collects the checks and writes resultado.json next to the script; finish() returns the exit code (1 = something failed). */
function report(dir) {
  const checks = []
  return {
    dir,
    video: null,
    check(label, ok, detail = '') {
      checks.push({ label, ok: !!ok, detail })
      console.log(`${ok ? 'OK   ' : 'FALHA'} ${label}${detail ? `: ${detail}` : ''}`)
      return !!ok
    },
    finish() {
      const failed = checks.filter((c) => !c.ok).length
      if (this.video && this.video.pending) this.video = { width: this.video.width, skipped: 'gravado, mas sem kit.closePage(page, r)' }
      fs.writeFileSync(path.join(dir, 'resultado.json'), JSON.stringify({ checks, failed, video: this.video }, null, 2))
      if (this.video && this.video.skipped) console.log(`SEM VIDEO: ${this.video.skipped}`)
      console.log(`QA: ${checks.length - failed}/${checks.length} ok`)
      return failed ? 1 : 0
    },
  }
}

/** Screenshot plus the screen checks of one width: axe, 44 px targets on touch widths, no horizontal overflow. */
async function screens(page, r, name, qa, scope = 'body') {
  const width = page.viewportSize().width
  await page.screenshot({ path: path.join(r.dir, `${name}.png`), fullPage: true })
  const violations = await axe(page, qa.axeTags)
  r.check(`${name}: axe`, violations.length === 0, violations.join(', '))
  if (width < 1024) {
    const small = await smallTargets(page, qa.minTarget, scope)
    r.check(`${name}: alvos de ${qa.minTarget} px`, small.length === 0, small.slice(0, 5).join(', '))
  }
  const over = await overflow(page)
  r.check(`${name}: sem rolagem horizontal`, over <= 0, over > 0 ? `${over} px` : '')
}

module.exports = { guard, config, defaultVideoWidth, videoVerdict, VIDEO_MAX_BYTES, launch, pageAt, closePage, axe, smallTargets,
  overflow, report, screens }
