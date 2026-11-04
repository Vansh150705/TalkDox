// Browser smoke test of the real UI against a running backend (use backend/scripts/run_offline_demo.py).
//   node e2e/smoke.mjs [baseUrl] [screenshotDir]
import { createRequire } from 'node:module'
import fs from 'node:fs'
import path from 'node:path'
const require = createRequire(import.meta.url)
const { chromium } = require('playwright')

const base = process.argv[2] || 'http://127.0.0.1:5173'
const shots = process.argv[3] || ''
const fixtures = path.resolve(process.argv[4] || path.join(path.dirname(new URL(import.meta.url).pathname), 'fixtures'))
let failures = 0
const check = (ok, msg) => { console.log(`${ok ? 'PASS' : 'FAIL'}  ${msg}`); if (!ok) failures++ }
const shot = async (page, name) => { if (shots) await page.screenshot({ path: path.join(shots, name + '.png') }) }

const browser = await chromium.launch({ args: ['--no-sandbox'] })
const ctx = await browser.newContext({ viewport: { width: 1360, height: 860 }, deviceScaleFactor: 1 })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', e => errors.push(String(e)))
page.on('console', m => { if (m.type() === 'error' && !/favicon|Failed to load resource/.test(m.text())) errors.push(m.text()) })

// 1. landing has no fabricated claims
await page.goto(base + '/')
const landing = await page.textContent('body')
check(!/FAISS|bypass bot|Gemini 2\.5 Flash|any website/i.test(landing), 'landing page free of unverifiable claims')
await shot(page, '01-landing')

// 2. onboarding + upload
await page.goto(base + '/upload')
await page.waitForSelector('text=Add your sources')
check(await page.isVisible('text=Add what you want to understand'), 'first-run onboarding shown')
await page.click('text=Skip')
await page.setInputFiles('input[type=file]', fs.readdirSync(fixtures).filter(f => !f.startsWith('.')).map(f => path.join(fixtures, f)))
await page.click('button:has-text("Add 3 files")')
await page.waitForSelector('.badge.completed', { timeout: 60000 })
await page.waitForFunction(() => document.querySelectorAll('.badge.completed, .badge.partial').length >= 3, null, { timeout: 60000 })
check(true, 'three files processed with live status')
await shot(page, '02-upload-done')

// 3. extraction report
await page.click('.src-actions button:has-text("Extraction report")')
check(await page.isVisible('.report'), 'extraction report opens')

// 4. chat with citations
await page.click('text=Start chatting →')
await page.waitForSelector('textarea[aria-label="Your question"]')
await page.fill('textarea[aria-label="Your question"]', 'How long is parental leave?')
await page.keyboard.press('Enter')
await page.waitForSelector('.msg.ai .cite-card', { timeout: 30000 })
const answer = await page.textContent('.msg.ai .bubble')
check(/16 weeks/.test(answer), 'answer contains the fact')
check(await page.isVisible('.cite-chip'), 'citation chip rendered in the answer')
check(/p\. 2/.test(await page.textContent('.cite-card')), 'citation shows page 2')
await shot(page, '03-chat-answer')

// 5. citation opens the viewer at the passage
await page.click('.cite-card >> text=View in source')
await page.waitForSelector('.viewer .passage.focus')
check(/16 weeks/.test(await page.textContent('.viewer .passage.focus')), 'source viewer highlights the cited passage')
await shot(page, '04-viewer')
await page.keyboard.press('Escape')

// 6. not-in-source question is declined
await page.fill('textarea[aria-label="Your question"]', 'What is the retail price of the moon?')
await page.keyboard.press('Enter')
await page.waitForSelector('.bubble.noev', { timeout: 30000 })
check(/(couldn.t find|do(es)? not contain)/i.test(await page.textContent('.bubble.noev')), 'unanswerable question is declined honestly')
await shot(page, '05-no-evidence')

// 7. tools
await page.click('role=tab[name=/Study tools/]')
await page.click('.toolcard:has-text("Timeline")')
check(await page.isVisible('text=Using 3 selected sources'), 'tool reflects selected sources')
await shot(page, '06-tools')

// 8. insights / privacy
await page.click('role=tab[name=/Insights/]')
await page.waitForSelector('text=Privacy & data')
check(await page.isVisible('text=Delete all my data'), 'privacy controls visible')

// 9. mobile layout
await page.setViewportSize({ width: 390, height: 800 })
await page.click('role=tab[name=/Chat/]')
const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)
check(!overflow, 'no horizontal overflow at 390px')
await page.click('button[aria-label="Open sources and history"]')
await page.waitForTimeout(500)
const box = await page.locator('.sidebar').boundingBox()
check(box && box.x >= 0 && box.width > 250, 'sidebar opens fully as a drawer on mobile')
await shot(page, '07-mobile')

check(errors.length === 0, `no console/page errors ${errors.slice(0, 3).join(' | ')}`)
await browser.close()
console.log(failures ? `\n${failures} check(s) failed` : '\nAll UI checks passed')
process.exit(failures ? 1 : 0)
