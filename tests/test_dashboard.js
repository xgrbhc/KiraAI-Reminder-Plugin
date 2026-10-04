// Run with: node --test tests/test_dashboard.js (no npm dependencies).
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

const web = path.join(__dirname, '..', 'web')
const source = fs.readFileSync(path.join(web, 'app.js'), 'utf8')
const translations = fs.readFileSync(path.join(web, 'i18n.js'), 'utf8')
const record = (id = 'job1', owner = 'u1') => ({ job_id: id, creator_id: owner, owner_id: owner, content: 'Test reminder' })
const issue = (id = 'delivery1', owner = 'u1') => ({ delivery_id: id, job_id: 'job1', status: 'unconfirmed', reminder: record('job1', owner) })
const plain = value => JSON.parse(JSON.stringify(value))
const deferred = () => {
    let resolve, reject
    const promise = new Promise((yes, no) => { resolve = yes; reject = no })
    return { promise, resolve, reject }
}

function dashboard(options = {}) {
    let ui, mount, unmount, contextListener
    const watches = new Map(), timers = new Map(), calls = []
    let nextTimer = 0
    const api = {
        get: async endpoint => {
            calls.push({ method: 'get', endpoint })
            if (options.get) return options.get(endpoint)
            return { status: 'ok', data: endpoint === 'sessions' ? [{ id: 'A:dm:u1' }]
                : endpoint.startsWith('deliveries/') ? [issue()] : [record()] }
        },
        post: async (endpoint, payload) => {
            calls.push({ method: 'post', endpoint, payload: plain(payload) })
            return options.post ? options.post(endpoint, payload) : { status: 'ok', msg: 'done' }
        },
    }
    const scope = {
        Vue: {
            ref(initial) {
                let value = initial
                const object = { get value() { return value }, set value(next) {
                    const old = value
                    value = next
                    if (old !== next) watches.get(object)?.(next, old)
                } }
                return object
            },
            computed: get => ({ get value() { return get() } }),
            watch: (target, callback) => watches.set(target, callback),
            onMounted: callback => { mount = callback },
            onUnmounted: callback => { unmount = callback },
            nextTick: callback => Promise.resolve().then(callback),
            createApp: component => ({ mount: () => { ui = component.setup() } }),
        },
        window: {
            location: { search: '' },
            confirm: () => { throw new Error('Native confirmation must not be used') },
            PluginPageContext: options.noContext ? undefined : {
                api, ready: options.ready || (async () => ({ locale: options.locale || 'zh' })),
                onContext: callback => { contextListener = callback; return () => { contextListener = null } },
            },
        },
        document: { activeElement: null, getElementById: () => null },
        setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id },
        clearTimeout: id => timers.delete(id),
        URLSearchParams, Date, Set, console,
    }
    vm.runInNewContext(translations, scope)
    vm.runInNewContext(source, scope)
    return { ui, calls, timers, mount, unmount, scope,
        changeLocale: locale => contextListener?.({ locale }),
        async load(sid = 'A:dm:u1') { await mount(); ui.sessionId.value = sid; await ui.fetchReminders() },
        posts: () => calls.filter(call => call.method === 'post'),
    }
}

for (const decision of ['retry', 'dismiss']) {
    test(`${decision}: opens in-page confirmation, cancel sends no request`, async () => {
        const h = dashboard(); await h.load()
        h.ui.reviewDelivery(decision, h.ui.deliveryIssues.value[0])
        assert.equal(h.ui.showConfirmModal.value, true)
        assert.match(h.ui.confirmMessage.value, decision === 'retry' ? /重复/ : /移除/)
        assert.equal(h.posts().length, 0)
        h.ui.closeModal()
        assert.equal(h.ui.showConfirmModal.value, false)
        assert.equal(h.posts().length, 0)
    })
    test(`${decision}: confirmed payload retains exact session and delivery`, async () => {
        const h = dashboard(); await h.load()
        h.ui.reviewDelivery(decision, h.ui.deliveryIssues.value[0])
        await h.ui.confirmDelete()
        assert.deepEqual(h.posts(), [{ method: 'post', endpoint: `deliveries/${decision}`,
            payload: { session_id: 'A:dm:u1', delivery_id: 'delivery1' } }])
        assert.equal(h.ui.showConfirmModal.value, false)
    })
}

test('double confirmation and repeated task clicks submit only once', async () => {
    const pending = deferred(), h = dashboard({ post: () => pending.promise }); await h.load()
    h.ui.reviewDelivery('retry', h.ui.deliveryIssues.value[0])
    const first = h.ui.confirmDelete()
    await h.ui.confirmDelete()
    await h.ui.doAction('pause', 'job1')
    h.ui.closeModal()
    assert.equal(h.posts().length, 1)
    assert.equal(h.ui.mutationBusy.value, true)
    assert.equal(h.ui.showConfirmModal.value, true)
    pending.resolve({ status: 'ok', msg: 'done' }); await first
    assert.equal(h.ui.mutationBusy.value, false)
})

test('switching or editing session clears stale data and pending confirmations', async () => {
    const h = dashboard(); await h.load()
    h.ui.reviewDelivery('retry', h.ui.deliveryIssues.value[0])
    h.ui.sessionId.value = 'B:dm:u1'
    assert.equal(h.ui.showConfirmModal.value, false)
    assert.equal(h.ui.reminders.value.length, 0)
    assert.equal(h.ui.actionsDisabled.value, true)
    await h.ui.confirmDelete()
    await h.ui.doAction('delete', 'job1')
    assert.equal(h.posts().length, 0)
})

test('late old-session response cannot replace current-session records', async () => {
    const old = deferred()
    const h = dashboard({ get: endpoint => endpoint === 'reminders/A%3Adm%3Au1' ? old.promise
        : { status: 'ok', data: endpoint.startsWith('reminders/') ? [record('new')] : [] } })
    await h.mount(); h.ui.sessionId.value = 'A:dm:u1'
    const first = h.ui.fetchReminders()
    h.ui.sessionId.value = 'B:dm:u1'; await h.ui.fetchReminders()
    old.resolve({ status: 'ok', data: [record('old')] }); await first
    assert.equal(h.ui.reminders.value[0].job_id, 'new')
    assert.equal(h.ui.loading.value, false)
    assert.equal(h.ui.actionsDisabled.value, false)
})

test('late delivery response cannot overwrite a new session', async () => {
    const old = deferred(), reached = deferred()
    const h = dashboard({ get: endpoint => {
        if (endpoint === 'deliveries/A%3Adm%3Au1') { reached.resolve(); return old.promise }
        return { status: 'ok', data: endpoint.startsWith('reminders/') ? [record('new')]
            : endpoint.startsWith('deliveries/') ? [issue('new')] : [] }
    } })
    await h.mount(); h.ui.sessionId.value = 'A:dm:u1'
    const first = h.ui.fetchReminders(); await reached.promise
    h.ui.sessionId.value = 'B:dm:u1'; await h.ui.fetchReminders()
    old.resolve({ status: 'ok', data: [issue('old')] }); await first
    assert.equal(h.ui.deliveryIssues.value[0].delivery_id, 'new')
})

test('important deletion retains backend token challenge and validation', async () => {
    const h = dashboard({ post: endpoint => endpoint === 'reminders/delete'
        ? { status: 'error', msg: '重要提醒\n请确认删除令牌: abc' } : { status: 'ok', msg: 'done' } })
    await h.load(); await h.ui.doAction('delete', 'job1')
    assert.equal(h.posts().length, 0)
    await h.ui.confirmDelete()
    assert.equal(h.ui.needsDeleteToken.value, true)
    await h.ui.confirmDelete()
    assert.equal(h.posts().length, 1)
    h.ui.deleteToken.value = ' abc '; await h.ui.confirmDelete()
    assert.deepEqual(h.posts()[1].payload, { session_id: 'A:dm:u1', confirm_token: 'abc' })
    assert.equal(h.ui.showConfirmModal.value, false)
})

test('user filtering applies to reminders, deliveries, and counts', async () => {
    const h = dashboard(); await h.load()
    h.ui.reminders.value = [record('job1'), record('job2', 'u2')]
    h.ui.deliveryIssues.value = [issue(), issue('delivery2', 'u2')]
    h.ui.selectedUserId.value = 'u2'
    assert.equal(h.ui.filteredReminders.value.length, 1)
    assert.equal(h.ui.filteredDeliveryIssues.value[0].delivery_id, 'delivery2')
    assert.equal(h.ui.importantCount.value, 0)
})

test('missing delivery status is not displayed as a known active count', async () => {
    const h = dashboard({ get: endpoint => ({ status: 'ok', data: endpoint === 'sessions' ? []
        : endpoint.startsWith('deliveries/') ? null : [record()] }) })
    await h.load()
    assert.equal(h.ui.reminders.value.length, 1)
    assert.equal(h.ui.deliveryStatusKnown.value, false)
    assert.equal(h.ui.activeCount.value, null)
    assert(h.ui.toasts.value.length)
})

test('malformed and failed requests show errors, not an empty success state', async () => {
    const h = dashboard({ get: async () => { throw new Error('offline') } }); await h.load()
    assert.equal(h.ui.loadError.value, 'offline')
    assert.equal(h.ui.actionsDisabled.value, true)
    assert.equal(h.ui.loading.value, false)
    assert.equal(new Set(h.ui.toasts.value.map(toast => toast.id)).size, h.ui.toasts.value.length)
})

test('stalled reads stop loading with an actionable error', async () => {
    const h = dashboard({ get: endpoint => endpoint === 'sessions'
        ? {status: 'ok', data: []} : new Promise(() => {}) })
    await h.mount(); h.ui.sessionId.value = 'A:dm:u1'
    const read = h.ui.fetchReminders()
    for (const timer of h.timers.values()) if (timer.delay === 12000) timer.callback()
    await read
    assert.equal(h.ui.loading.value, false)
    assert.equal(h.ui.actionsDisabled.value, true)
    assert.match(h.ui.loadError.value, /超时/)
})

test('unknown mutation outcome disables further actions until a refresh', async () => {
    const h = dashboard({ post: () => { throw new Error('connection lost') } }); await h.load()
    h.ui.reviewDelivery('retry', h.ui.deliveryIssues.value[0]); await h.ui.confirmDelete()
    assert.equal(h.ui.actionsDisabled.value, true)
    assert.match(h.ui.loadError.value, /可能已经生效/)
    await h.ui.doAction('pause', 'job1')
    assert.equal(h.posts().length, 1)
    await h.ui.fetchReminders()
    assert.equal(h.ui.actionsDisabled.value, false)
})

test('locale updates include all newly added confirmation text', async () => {
    const h = dashboard({ locale: 'en' }); await h.load()
    h.ui.reviewDelivery('retry', h.ui.deliveryIssues.value[0])
    assert.equal(h.ui.confirmTitle.value, 'Confirm reminder retry')
    assert.match(h.ui.confirmMessage.value, /already have handled/)
    const messages = h.scope.window.ReminderDashboardMessages
    assert.deepEqual(Object.keys(messages.zh).sort(), Object.keys(messages.en).sort())
    h.changeLocale('zh')
    assert.equal(h.ui.confirmTitle.value, '确认重试提醒')
    h.unmount(); assert.equal(h.timers.size, 0)
})

test('missing or timed-out page context yields visible initialization errors', async () => {
    const missing = dashboard({ noContext: true }); await missing.mount()
    assert.match(missing.ui.startupError.value, /初始化失败/)
    const stalled = dashboard({ ready: () => new Promise(() => {}) })
    const mount = stalled.mount()
    for (const timer of stalled.timers.values()) if (timer.delay === 8000) timer.callback()
    await mount
    assert.match(stalled.ui.startupError.value, /超时/)
})

test('template has no unsupported inline timer and uses a sandbox-safe dialog', () => {
    const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    assert(!source.includes('window.confirm'))
    assert(!html.includes('@blur="setTimeout'))
    assert(html.includes('role="dialog"'))
    assert(html.includes('id="startup-fallback"'))
    assert(html.includes('filteredDeliveryIssues'))
    assert(html.includes('v-if="loading && !hasCurrentSnapshot"'))
})

test('same-session refresh retains both lists until the complete snapshot arrives', async () => {
    const pending = deferred(), reached = deferred()
    let refreshing = false
    const h = dashboard({ get: endpoint => {
        if (refreshing && endpoint.startsWith('deliveries/')) { reached.resolve(); return pending.promise }
        return { status: 'ok', data: endpoint.startsWith('reminders/') ? [record(refreshing ? 'new' : 'job1')]
            : endpoint.startsWith('deliveries/') ? [issue()] : [] }
    } })
    await h.load(); refreshing = true
    const read = h.ui.fetchReminders(); await reached.promise
    assert.equal(h.ui.loading.value, true)
    assert.equal(h.ui.hasCurrentSnapshot.value, true)
    assert.equal(h.ui.actionsDisabled.value, true)
    assert.equal(h.ui.reminders.value[0].job_id, 'job1')
    assert.equal(h.ui.deliveryIssues.value[0].delivery_id, 'delivery1')
    pending.resolve({ status: 'ok', data: [issue('new-delivery')] }); await read
    assert.equal(h.ui.reminders.value[0].job_id, 'new')
    assert.equal(h.ui.deliveryIssues.value[0].delivery_id, 'new-delivery')
    assert.equal(h.ui.actionsDisabled.value, false)
})

test('successful task action refresh keeps the list visible and cannot submit again', async () => {
    const pending = deferred(), reached = deferred()
    let refreshing = false
    const h = dashboard({
        get: endpoint => {
            if (refreshing && endpoint.startsWith('reminders/')) { reached.resolve(); return pending.promise }
            return { status: 'ok', data: endpoint.startsWith('reminders/') ? [record()]
                : endpoint.startsWith('deliveries/') ? [issue()] : [] }
        },
        post: () => { refreshing = true; return { status: 'ok', msg: 'done' } },
    })
    await h.load()
    const action = h.ui.doAction('pause', 'job1'); await reached.promise
    assert.equal(h.ui.hasCurrentSnapshot.value, true)
    assert.equal(h.ui.reminders.value.length, 1)
    assert.equal(h.ui.deliveryIssues.value.length, 1)
    await h.ui.doAction('resume', 'job1')
    assert.equal(h.posts().length, 1)
    pending.resolve({ status: 'ok', data: [{ ...record(), paused: true }] }); await action
    assert.equal(h.ui.reminders.value[0].paused, true)
    assert.equal(h.ui.mutationBusy.value, false)
})

test('failed refresh removes the unusable snapshot and leaves actions disabled', async () => {
    let fail = false
    const h = dashboard({ get: endpoint => {
        if (fail && endpoint.startsWith('reminders/')) throw new Error('offline')
        return { status: 'ok', data: endpoint.startsWith('reminders/') ? [record()] : [] }
    } })
    await h.load(); fail = true
    await h.ui.fetchReminders()
    assert.equal(h.ui.hasCurrentSnapshot.value, false)
    assert.equal(h.ui.reminders.value.length, 0)
    assert.equal(h.ui.actionsDisabled.value, true)
    assert.equal(h.ui.loadError.value, 'offline')
})

test('closing a dialog restores focus without scrolling and does not reuse stale focus', async () => {
    const h = dashboard(); await h.load()
    const focuses = []
    h.scope.document.activeElement = { isConnected: true, focus: options => focuses.push(plain(options)) }
    h.ui.modalElement.value = { querySelector: () => ({ focus: options => focuses.push(plain(options)) }) }
    h.ui.reviewDelivery('retry', h.ui.deliveryIssues.value[0]); await Promise.resolve()
    h.ui.closeModal(); await Promise.resolve()
    assert.deepEqual(focuses, [{ preventScroll: true }, { preventScroll: true }])
    await h.ui.doAction('pause', 'job1')
    assert.equal(focuses.length, 2)
})

test('token dialog keeps original trigger focus and restores only after submission unlocks', async () => {
    const h = dashboard({ post: endpoint => endpoint === 'reminders/delete'
        ? { status: 'error', msg: '请确认删除令牌: abc' } : { status: 'ok', msg: 'done' } })
    await h.load()
    const focuses = []
    h.scope.document.activeElement = {
        isConnected: true,
        focus: options => focuses.push({ options: plain(options), busy: h.ui.mutationBusy.value }),
    }
    await h.ui.doAction('delete', 'job1')
    h.scope.document.activeElement = { isConnected: true, focus: () => assert.fail('Must not focus a modal trigger') }
    await h.ui.confirmDelete()
    assert.equal(focuses.length, 0)
    h.ui.deleteToken.value = 'abc'; await h.ui.confirmDelete(); await Promise.resolve()
    assert.deepEqual(focuses, [{ options: { preventScroll: true }, busy: false }])
})

test('toast count is capped and displaced notifications release their timers', async () => {
    let sequence = 0
    const h = dashboard({ post: () => ({ status: 'ok', msg: `result-${++sequence}` }) }); await h.load()
    for (let i = 0; i < 7; i++) await h.ui.doAction('pause', 'job1')
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.message)), ['result-5', 'result-6', 'result-7'])
    assert.equal(h.timers.size, 3)
})

test('identical notifications merge and renew one timer without stale expiry', async () => {
    const h = dashboard(); await h.load()
    await h.ui.doAction('pause', 'job1')
    const id = h.ui.toasts.value[0].id
    const [oldTimer, oldExpiry] = [...h.timers.entries()].find(([, timer]) => timer.delay === 4000)
    await h.ui.doAction('pause', 'job1')
    assert.equal(h.ui.toasts.value.length, 1)
    assert.equal(h.ui.toasts.value[0].id, id)
    assert.equal(h.timers.has(oldTimer), false)
    assert.equal(h.timers.size, 1)
    oldExpiry.callback()
    assert.equal(h.ui.toasts.value.length, 1)
    const expiry = [...h.timers.values()].find(timer => timer.delay === 4000)
    expiry.callback()
    assert.equal(h.ui.toasts.value.length, 0)
    assert.equal(h.timers.size, 0)
})

test('different operation results are not merged just because their titles match', async () => {
    const h = dashboard({ post: endpoint => ({ status: 'ok', msg: endpoint }) }); await h.load()
    await h.ui.doAction('pause', 'job1')
    await h.ui.doAction('resume', 'job1')
    assert.equal(h.ui.toasts.value.length, 2)
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.message)), ['reminders/pause', 'reminders/resume'])
})

test('dismissal hides only the selected notification and never changes data or submits', async () => {
    const h = dashboard({ post: endpoint => ({ status: 'ok', msg: endpoint }) }); await h.load()
    await h.ui.doAction('pause', 'job1'); await h.ui.doAction('resume', 'job1')
    const reminders = plain(h.ui.reminders.value), deliveries = plain(h.ui.deliveryIssues.value)
    const posts = h.posts().length, remaining = h.ui.toasts.value[1].id
    h.ui.dismissToast(h.ui.toasts.value[0].id)
    h.ui.dismissToast(-1)
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.id)), [remaining])
    assert.equal(h.timers.size, 1)
    assert.equal(h.posts().length, posts)
    assert.deepEqual(plain(h.ui.reminders.value), reminders)
    assert.deepEqual(plain(h.ui.deliveryIssues.value), deliveries)
})

test('success notifications do not displace errors and new errors replace oldest successes', async () => {
    let error = false, sequence = 0
    const h = dashboard({ post: () => ({ status: error ? 'error' : 'ok', msg: `result-${++sequence}` }) }); await h.load()
    await h.ui.doAction('pause', 'job1')
    error = true; await h.ui.doAction('pause', 'job1')
    error = false
    await h.ui.doAction('pause', 'job1'); await h.ui.doAction('pause', 'job1')
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.message)), ['result-2', 'result-3', 'result-4'])
    error = true; await h.ui.doAction('pause', 'job1')
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.message)), ['result-2', 'result-4', 'result-5'])
    assert.equal(h.timers.size, 3)
})

test('an all-error stack suppresses success notices but keeps the newest errors bounded', async () => {
    let error = true, sequence = 0
    const h = dashboard({ post: () => ({ status: error ? 'error' : 'ok', msg: `result-${++sequence}` }) }); await h.load()
    for (let i = 0; i < 3; i++) await h.ui.doAction('pause', 'job1')
    error = false; await h.ui.doAction('pause', 'job1')
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.message)), ['result-1', 'result-2', 'result-3'])
    error = true; await h.ui.doAction('pause', 'job1')
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.message)), ['result-2', 'result-3', 'result-5'])
    assert.equal(h.timers.size, 3)
})

test('same text with a different severity remains distinct', async () => {
    const h = dashboard({ post: () => ({ status: 'error', msg: 'same' }) }); await h.load()
    await h.ui.doAction('pause', 'job1')
    await h.ui.doAction('pause', 'job1')
    assert.equal(h.ui.toasts.value.length, 1)
    h.ui.toasts.value[0].type = 'success'
    await h.ui.doAction('pause', 'job1')
    assert.equal(h.ui.toasts.value.length, 2)
    assert.deepEqual(plain(h.ui.toasts.value.map(toast => toast.type)), ['success', 'error'])
})

test('unmount clears notification timers and late requests cannot create more', async () => {
    const pending = deferred(), h = dashboard({ post: () => pending.promise }); await h.load()
    const request = h.ui.doAction('pause', 'job1')
    h.unmount()
    pending.reject(new Error('late offline')); await request
    assert.equal(h.timers.size, 0)
    assert.equal(h.ui.toasts.value.length, 0)
})

test('toast close button is wired with matching Chinese and English labels', async () => {
    const h = dashboard(); await h.load()
    const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    assert(html.includes('@click="dismissToast(toast.id)"'))
    assert(html.includes(':aria-label="t(\'closeToast\')"'))
    assert.equal(h.ui.t('closeToast'), '关闭通知')
    h.changeLocale('en')
    assert.equal(h.ui.t('closeToast'), 'Dismiss notification')
    assert.deepEqual(Object.keys(h.scope.window.ReminderDashboardMessages.zh).sort(),
        Object.keys(h.scope.window.ReminderDashboardMessages.en).sort())
})

test('toast replacement does not retain outgoing notices in a transition group', () => {
    const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    assert(!html.includes('<transition-group'))
    assert(html.includes('data-toast'))
})

test('toast styling avoids background blur and white hover fills without removing keyboard focus', () => {
    const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    const css = fs.readFileSync(path.join(web, 'style.css'), 'utf8')
    const panel = css.match(/\.glass-panel\.toast-panel\s*\{([^}]+)\}/)[1]
    const close = html.match(/<button[^>]*class="toast-close [^"]*"[^>]*>/)[0]
    assert(html.includes('class="glass-panel toast-panel '))
    assert.match(panel, /background:\s*#[a-f\d]{6};/i)
    assert.match(panel, /\bbackdrop-filter:\s*none;/)
    assert.match(panel, /-webkit-backdrop-filter:\s*none;/)
    assert.match(css, /\.toast-close\s*\{\s*background:\s*transparent;/)
    assert(close.includes('hover:text-white'))
    assert(!close.includes('hover:bg-'))
    assert.match(css, /button:focus-visible\s*\{[^}]*outline:\s*2px solid/)
})
