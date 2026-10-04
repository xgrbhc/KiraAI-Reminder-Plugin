// Run with: node --test tests/test_*.js (no npm dependencies).
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const vm = require('node:vm')

const web = path.join(__dirname, '..', 'web')
const read = relative => fs.readFileSync(path.join(web, relative), 'utf8')
const config = JSON.parse(read('package.json'))
const manifest = JSON.parse(read('vendor/asset-manifest.json'))

test('dashboard scripts and styles are shipped relative assets, without CDN fallback', () => {
    const html = read('index.html')
    const resources = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map(match => match[1])
    assert.equal(resources.length, 7)
    for (const relative of resources) {
        assert.ok(relative.startsWith('./'), relative)
        assert.ok(fs.statSync(path.join(web, relative)).size > 0, relative)
    }
    assert.doesNotMatch(html, /https?:\/\/|cdn\.|unpkg|fonts\.googleapis/)
    assert.ok(html.indexOf('vue.global.prod.js') < html.indexOf('./app.js'))
})

test('component defaults precede Tailwind overrides, preserving semantic borders, circles and shadows', () => {
    const styles = [...read('index.html').matchAll(/<link rel="stylesheet" href="([^"]+)">/g)]
        .map(match => match[1])
    assert.deepEqual(styles, [
        './vendor/outfit/outfit.css', './vendor/fontawesome/css/icons.min.css',
        './style.css', './vendor/tailwind.min.css',
    ], 'Do not load glass-panel defaults after utility overrides')
    const css = read('vendor/tailwind.min.css')
    for (const name of [
        '.border-l-4', '.border-l-emerald-500', '.border-l-red-500',
        '.border-l-yellow-400', '.border-l-transparent', '.border-yellow-500\\/30',
        '.rounded-full', '.shadow-lg', '.shadow-2xl',
    ]) assert.ok(css.includes(name), name)
    assert.ok(read('style.css').includes('.glass-panel.toast-panel'))
})

test('task hover changes other edges without resetting the semantic left border', () => {
    const hover = read('style.css').match(/\.task-card:hover\s*\{([^}]+)\}/)?.[1]
    assert.ok(hover)
    assert.doesNotMatch(hover, /(?:^|[;\n])\s*border(?:-(?:width|style|color|left(?:-(?:width|style|color))?))?\s*:/)
    for (const edge of ['top', 'right', 'bottom']) assert.ok(hover.includes(`border-${edge}-color:`))
})

test('every CSS font URL resolves locally, preserving all five Outfit weights', () => {
    for (const relative of Object.keys(manifest.files).filter(name => name.endsWith('.css'))) {
        const cssPath = path.join(web, 'vendor', relative)
        for (const match of fs.readFileSync(cssPath, 'utf8').matchAll(/url\(([^)]+)\)/g)) {
            const url = match[1].replace(/^['"]|['"]$/g, '')
            assert.doesNotMatch(url, /^(?:https?:|\/\/|\/)/, url)
            const resolved = path.resolve(path.dirname(cssPath), url)
            assert.ok(resolved.startsWith(path.join(web, 'vendor') + path.sep))
            assert.ok(fs.statSync(resolved).size > 0, url)
        }
    }
    for (const weight of [300, 400, 500, 600, 700]) {
        assert.ok(read('vendor/outfit/outfit.css').includes(`font-weight: ${weight};`))
    }
})

test('package versions, lockfile and shipped asset hashes match', () => {
    const lock = JSON.parse(read('package-lock.json'))
    assert.deepEqual(manifest.packages, config.devDependencies)
    assert.deepEqual(lock.packages[''].devDependencies, config.devDependencies)
    for (const [name, version] of Object.entries(config.devDependencies)) {
        assert.match(version, /^\d+\.\d+\.\d+$/)
        assert.equal(lock.packages[`node_modules/${name}`].version, version)
        assert.match(lock.packages[`node_modules/${name}`].integrity, /^sha512-/)
    }
    for (const [relative, expected] of Object.entries(manifest.files)) {
        const bytes = fs.readFileSync(path.join(web, 'vendor', relative))
        assert.equal(bytes.length, expected.bytes, relative)
        assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), expected.sha256, relative)
    }
    for (const license of ['vue', 'tailwindcss', 'outfit', 'fontawesome-free']) {
        assert.ok(read(`vendor/licenses/${license}.txt`).length > 500)
    }
})

test('Vue production global build retains the in-DOM template compiler', () => {
    const scope = { console }
    vm.runInNewContext(read('vendor/vue.global.prod.js'), scope)
    assert.equal(scope.Vue.version, config.devDependencies.vue)
    assert.equal(typeof scope.Vue.createApp, 'function')
    assert.equal(typeof scope.Vue.compile, 'function')
})

test('compiled Tailwind contains dynamic colors, responsive layout and keyboard focus styles', () => {
    const css = read('vendor/tailwind.min.css')
    for (const selector of [
        '.text-indigo-400', '.text-gray-500', '.text-emerald-500',
        '.rotate-180', '.animate-pulse', '.opacity-50', '.bg-indigo-500',
        '.md\\:p-12', '.xl\\:flex-row',
        '.hover\\:text-white', '.group-hover\\:opacity-100',
    ]) assert.ok(css.includes(selector), selector)
    const content = require(path.join(web, 'tailwind.config.cjs')).content
    assert.equal(content.relative, true)
    assert.ok(content.files.includes('./index.html'))
    assert.ok(content.files.includes('./app.js'))
    assert.ok(read('style.css').includes('button:focus-visible'))
})

test('all template icons exist in the bundled free icon styles', () => {
    const css = read('vendor/fontawesome/css/icons.min.css')
    const nonIcon = new Set(['fa-solid', 'fa-regular', 'fa-spin', 'fa-bounce'])
    for (const attribute of read('index.html').matchAll(/\bclass="([^"]+)"/g)) {
        for (const match of attribute[1].matchAll(/\bfa-[a-z]+(?:-[a-z]+)*\b/g)) {
            if (!nonIcon.has(match[0])) assert.ok(css.includes(`.${match[0]}:`), match[0])
        }
    }
    assert.doesNotMatch(read('index.html'), /fa-shield-check/)
})

test('session controls and menu fit their container at narrow widths', () => {
    const html = read('index.html')
    const container = html.match(/id="session-select-container" class="([^"]+)"/)[1].split(/\s+/)
    for (const name of ['min-w-0', 'w-full', 'sm:w-auto', 'max-w-full']) assert.ok(container.includes(name))
    const input = html.match(/<input v-model="sessionId"[^>]* class="([^"]+)"/)[1].split(/\s+/)
    for (const name of ['min-w-0', 'flex-1', 'w-full', 'sm:w-64']) assert.ok(input.includes(name))
    assert.ok(!input.includes('w-64'))
    const menu = html.match(/v-show="showSessionDropdown" class="([^"]+)"/)[1].split(/\s+/)
    for (const name of ['w-full', 'sm:w-80', 'max-w-full']) assert.ok(menu.includes(name))
    assert.ok(!menu.includes('w-80'))
    assert.match(html, /@click="fetchReminders" class="[^"]*w-full sm:w-auto shrink-0/)
})

test('long reminder metadata wraps while card actions retain their size and visibility', () => {
    const html = read('index.html')
    assert.match(read('style.css'), /\.content-wrap\s*\{\s*overflow-wrap:\s*anywhere;\s*\}/)
    assert.match(html, /<div class="min-w-0 flex-1">\s*<div class="flex flex-wrap items-center/)
    assert.match(html, /<h3 class="content-wrap [^"]*"[^>]*>\{\{ task\.content \}\}/)
    assert.match(html, /v-if="task.category" class="min-w-0 max-w-full content-wrap/)
    assert.match(html, /<span class="min-w-0 content-wrap">\{\{ task\.creator_name/)
    assert.match(html, /<span class="min-w-0 content-wrap">\{\{ task\.action \}\}/)
    assert.match(html, /v-for="u in currentSessionUsers"[^>]*class="min-w-0 max-w-full/)
    assert.match(html, /class="shrink-0 flex items-center justify-end [^"]*" style="opacity: 1;"/)
    for (const action of ['pause', 'resume', 'delete']) {
        const classes = html.match(new RegExp(`@click="doAction\\('${action}', task.job_id\\)" class="([^"]+)"`))[1]
        for (const name of ['w-12', 'h-12', 'rounded-full']) assert.ok(classes.split(/\s+/).includes(name))
    }
})

test('mobile spacing and bounded dialog styles are present in rebuilt assets', () => {
    const html = read('index.html')
    assert.match(html, /<body class="p-4 sm:p-6 md:p-12 overflow-x-hidden">/)
    assert.match(html, /class="task-card glass-panel p-4 pb-6 sm:p-6 sm:pb-8/)
    assert.match(html, /class="dialog-panel glass-panel p-4 sm:p-8/)
    assert.match(html, /class="glass-panel toast-panel px-4 sm:px-6 py-4 flex items-center gap-3 sm:gap-4/)
    assert.match(read('style.css'), /\.dialog-panel\s*\{[^}]*max-height:\s*calc\(100dvh - 2rem\);[^}]*overflow-y:\s*auto;/)
    const css = read('vendor/tailwind.min.css')
    for (const selector of [
        '.min-w-0', '.max-w-full', '.shrink-0', '.flex-wrap',
        '.sm\\:w-auto', '.sm\\:w-64', '.sm\\:w-80',
        '.sm\\:p-6', '.sm\\:p-8', '.sm\\:pb-8', '.sm\\:px-6', '.sm\\:gap-4',
    ]) assert.ok(css.includes(selector), selector)
})
