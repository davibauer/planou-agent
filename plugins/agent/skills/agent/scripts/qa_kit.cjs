// Helpers for the ephemeral script of the qa behavior (PLN0116). The script lives in the session's scratchpad, is never
// committed, and runs with the line that `qa_env.py up` prints (RODAR: QA_URL=... NODE_PATH=<node_modules of the
// worktree> node <script>), so `require('@playwright/test')` and `require('@axe-core/playwright')` come from the
// branch under test. Playwright is required lazily: guard() and config() work without it.
//
//   const kit = require(process.env.QA_KIT)
//   const qa = kit.config()                        // url, widths, minTarget, axeTags, served (refuses the served app)
//   const r = kit.report(__dirname)                // r.check(label, ok, detail); r.finish() writes resultado.json
//   const browser = await kit.launch()
//   for (const w of qa.widths) { const page = await kit.pageAt(browser, w); ...; await kit.screens(page, r, `tela-${w}`, qa) }
'use strict'
const fs = require('node:fs')
const path = require('node:path')

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
  return {
    url: guard(env.QA_URL, served),
    widths,
    minTarget: Number(env.QA_MIN_TARGET || 44),
    axeTags: list(env.QA_AXE_TAGS, ['wcag2a', 'wcag2aa', 'wcag21aa']),
    served,
  }
}

async function launch() {
  const { chromium } = require('@playwright/test')
  return chromium.launch()
}

/** A page at one width: touch below 1024 px, mobile below 600 px (the phone), pt-BR. */
async function pageAt(browser, width, extra = {}) {
  const touch = width < 1024
  const ctx = await browser.newContext({
    baseURL: config().url, viewport: { width, height: width < 600 ? 1400 : 1000 }, hasTouch: touch,
    isMobile: touch && width < 600, locale: 'pt-BR', ...extra,
  })
  return ctx.newPage()
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
    check(label, ok, detail = '') {
      checks.push({ label, ok: !!ok, detail })
      console.log(`${ok ? 'OK   ' : 'FALHA'} ${label}${detail ? `: ${detail}` : ''}`)
      return !!ok
    },
    finish() {
      const failed = checks.filter((c) => !c.ok).length
      fs.writeFileSync(path.join(dir, 'resultado.json'), JSON.stringify({ checks, failed }, null, 2))
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

module.exports = { guard, config, launch, pageAt, axe, smallTargets, overflow, report, screens }
