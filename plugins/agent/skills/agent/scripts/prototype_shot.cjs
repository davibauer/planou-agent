// Renders a local HTML prototype to PNG at each width and theme (prototype behavior, PLN0054). Playwright is not a
// dependency of the plugin: it comes from the project's node_modules through NODE_PATH (the prototype behavior's
// node_dir), as with qa_kit.cjs, and is required lazily, so the pure part (args, plan, guard) works without it.
//
//   NODE_PATH=<repo>/<node_dir> node prototype_shot.cjs <prototipo.html> <pasta> [--widths 1360,834,390]
//                                                       [--themes light,dark] [--name prototipo]
//
// One full-page PNG per width and theme, named <name>-<width>-<theme>.png, one path per line on stdout, then
// "PROTOTIPO: <n> PNGs em <pasta>". The theme goes both as prefers-color-scheme and as data-theme on <html>, so a page
// that themes either way flips. Only a local .html file: a URL (http, claude.ai or any other) is refused, because the
// prototype goes to the card as PNG and never as a link.
'use strict'
const fs = require('node:fs')
const path = require('node:path')
const { pathToFileURL } = require('node:url')

const THEMES = ['light', 'dark']
const DEFAULTS = { widths: [1360, 834, 390], themes: THEMES, name: 'prototipo' }
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,60}$/

function fail(msg) { throw new Error(msg) }

/** The HTML file must be a local .html/.htm path, never a URL. Returns the absolute path. */
function guard(file) {
  if (!file) fail('falta o arquivo .html do prototipo')
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(file)) fail(`so arquivo local, nunca link: ${JSON.stringify(file)}`)
  if (!/\.html?$/i.test(file)) fail(`o prototipo e um arquivo .html: ${JSON.stringify(file)}`)
  return path.resolve(file)
}

function parseArgs(argv) {
  const out = { ...DEFAULTS, rest: [] }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    const val = () => (i + 1 < argv.length ? argv[++i] : fail(`${a} precisa de um valor`))
    if (a === '--widths') out.widths = val().split(',').map((x) => Number(x.trim()))
    else if (a === '--themes') out.themes = val().split(',').map((x) => x.trim()).filter(Boolean)
    else if (a === '--name') out.name = val()
    else if (a.startsWith('--')) fail(`opcao desconhecida ${a}`)
    else out.rest.push(a)
  }
  if (out.rest.length !== 2) fail('uso: prototype_shot.cjs <prototipo.html> <pasta> [--widths ...] [--themes ...] [--name ...]')
  if (!out.widths.length || out.widths.some((w) => !Number.isInteger(w) || w < 200 || w > 4000)) fail(`--widths invalido: ${out.widths}`)
  if (!out.themes.length || out.themes.some((t) => !THEMES.includes(t))) fail(`--themes: ${THEMES.join(' e/ou ')}`)
  if (!NAME_RE.test(out.name)) fail(`--name: letras minusculas, numeros e hifen (${JSON.stringify(out.name)})`)
  const [file, dir] = out.rest
  return { file: guard(file), dir: path.resolve(dir), widths: out.widths, themes: out.themes, name: out.name }
}

/** The shots to take: [{width, theme, height, file}], widths outer, themes inner. Phone (<600) gets a phone-sized window. */
function plan(o) {
  const shots = []
  for (const width of o.widths) {
    for (const theme of o.themes) {
      shots.push({ width, theme, height: width < 600 ? 844 : 900, file: path.join(o.dir, `${o.name}-${width}-${theme}.png`) })
    }
  }
  return shots
}

async function render(o) {
  if (!fs.existsSync(o.file)) fail(`nao existe: ${o.file}`)
  fs.mkdirSync(o.dir, { recursive: true })
  const { chromium } = require('@playwright/test')
  const browser = await chromium.launch()
  try {
    for (const s of plan(o)) {
      const touch = s.width < 1024
      const ctx = await browser.newContext({
        viewport: { width: s.width, height: s.height }, colorScheme: s.theme, hasTouch: touch,
        isMobile: touch && s.width < 600, locale: 'pt-BR', deviceScaleFactor: 1,
      })
      const page = await ctx.newPage()
      await page.goto(pathToFileURL(o.file).href, { waitUntil: 'load' })
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), s.theme)
      await page.evaluate(() => (document.fonts ? document.fonts.ready : null))
      await page.screenshot({ path: s.file, fullPage: true })
      await ctx.close()
      console.log(s.file)
    }
  } finally {
    await browser.close()
  }
  console.log(`PROTOTIPO: ${plan(o).length} PNGs em ${o.dir}`)
}

module.exports = { guard, parseArgs, plan, THEMES, DEFAULTS }

if (require.main === module) {
  let o
  try { o = parseArgs(process.argv.slice(2)) } catch (e) { console.error(e.message); process.exit(2) }
  render(o).catch((e) => { console.error(e.message); process.exit(1) })
}
