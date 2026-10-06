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

for (const decision of ['dismiss']) {
    test(`${decision}: opens in-page confirmation, cancel sends no request`, async () => {
        const h = dashboard(); await h.load()
        h.ui.reviewDelivery(decision, h.ui.deliveryIssues.value[0])
        assert.equal(h.ui.showConfirmModal.value, true)
        assert.match(h.ui.confirmMessage.value, /不删除原待办/)
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
    h.ui.reviewDelivery('dismiss', h.ui.deliveryIssues.value[0])
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
    h.ui.reviewDelivery('dismiss', h.ui.deliveryIssues.value[0])
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

test('manual scan refreshes sessions, users and both lists without changing selections or writing', async () => {
    let refreshing = false
    const h = dashboard({ get: endpoint => ({ status: 'ok', data: endpoint === 'sessions'
        ? [{ id: 'A:dm:u1', users: refreshing ? [{ id: 'u1', name: 'Updated' }, { id: 'u2', name: 'New user' }]
            : [{ id: 'u1', name: 'Original' }] }, ...(refreshing ? [{ id: 'B:gm:g1', users: [] }] : [])]
        : endpoint.startsWith('deliveries/') ? [issue(refreshing ? 'new-delivery' : 'delivery1')]
        : refreshing ? [record(), record('job2', 'u2')] : [record()] }) })
    await h.load(); h.ui.selectedUserId.value = 'u1'; refreshing = true
    await h.ui.scanNetwork()
    assert.equal(h.ui.sessionId.value, 'A:dm:u1')
    assert.equal(h.ui.selectedUserId.value, 'u1')
    assert.deepEqual(plain(h.ui.currentSessionUsers.value), [{ id: 'u1', name: 'Updated' }, { id: 'u2', name: 'New user' }])
    assert.equal(h.ui.availableSessions.value.length, 2)
    assert.equal(h.ui.reminders.value.length, 2)
    assert.equal(h.ui.filteredReminders.value.length, 1)
    assert.equal(h.ui.deliveryIssues.value[0].delivery_id, 'new-delivery')
    assert.equal(h.calls.filter(call => call.endpoint === 'sessions').length, 2)
    assert.equal(h.ui.scanning.value, false)
    assert.equal(h.ui.actionsDisabled.value, false)
    assert.equal(h.posts().length, 0)
    assert.equal(h.timers.size, 0)
})

test('manual scan can discover sessions without choosing one automatically', async () => {
    const h = dashboard(); await h.mount()
    await h.ui.scanNetwork()
    assert.equal(h.ui.sessionId.value, '')
    assert.equal(h.ui.availableSessions.value[0].id, 'A:dm:u1')
    assert.equal(h.calls.filter(call => call.endpoint === 'sessions').length, 2)
    assert.equal(h.calls.filter(call => call.endpoint.startsWith('reminders/')).length, 0)
    assert.equal(h.posts().length, 0)
    assert.equal(h.ui.scanning.value, false)
})

for (const malformed of [false, true]) {
    test(`failed metadata scan preserves users and does not invalidate reminder data (malformed=${malformed})`, async () => {
        let refreshing = false
        const h = dashboard({ get: endpoint => {
            if (refreshing && endpoint === 'sessions') {
                if (malformed) return { status: 'ok', data: null }
                throw new Error('metadata offline')
            }
            return { status: 'ok', data: endpoint === 'sessions'
                ? [{ id: 'A:dm:u1', users: [{ id: 'u1', name: 'Original' }] }]
                : endpoint.startsWith('deliveries/') ? [] : [record(refreshing ? 'new' : 'job1')] }
        } })
        await h.load(); h.ui.selectedUserId.value = 'u1'; refreshing = true
        await h.ui.scanNetwork()
        assert.equal(h.ui.currentSessionUsers.value[0].name, 'Original')
        assert.equal(h.ui.selectedUserId.value, 'u1')
        assert.equal(h.ui.reminders.value[0].job_id, 'new')
        assert.equal(h.ui.actionsDisabled.value, false)
        assert.equal(h.ui.loadError.value, '')
        assert.equal(h.ui.scanning.value, false)
        assert.equal(h.ui.toasts.value[0].title, h.ui.t('sessionFailed'))
    })
}

test('duplicate manual scans do not overlap and metadata does not prolong the write lock', async () => {
    const metadata = deferred(), read = deferred()
    let refreshing = false
    const h = dashboard({ get: endpoint => {
        if (refreshing && endpoint === 'sessions') return metadata.promise
        if (refreshing && endpoint.startsWith('reminders/')) return read.promise
        return { status: 'ok', data: endpoint === 'sessions' ? [{ id: 'A:dm:u1' }]
            : endpoint.startsWith('deliveries/') ? [] : [record()] }
    } })
    await h.load(); refreshing = true
    const scan = h.ui.scanNetwork(), calls = h.calls.length
    await h.ui.scanNetwork(); await h.ui.scanNetwork()
    assert.equal(h.calls.length, calls)
    assert.equal(h.ui.scanning.value, true)
    assert.equal(h.ui.actionsDisabled.value, true)
    read.resolve({ status: 'ok', data: [record()] })
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(h.ui.loading.value, false)
    assert.equal(h.ui.scanning.value, true)
    assert.equal(h.ui.actionsDisabled.value, false)
    await h.ui.doAction('pause', 'job1')
    assert.equal(h.posts().length, 1)
    assert.equal(h.calls.filter(call => call.endpoint === 'sessions').length, 2)
    metadata.resolve({ status: 'ok', data: [{ id: 'A:dm:u1' }] }); await scan
    assert.equal(h.ui.scanning.value, false)
})

test('late initialization metadata cannot replace a newer manual scan', async () => {
    const initial = deferred()
    let metadataReads = 0
    const h = dashboard({ get: endpoint => endpoint === 'sessions'
        ? ++metadataReads === 1 ? initial.promise : { status: 'ok', data: [{ id: 'New:dm:u1' }] }
        : { status: 'ok', data: [] } })
    await h.mount(); h.ui.sessionId.value = 'A:dm:u1'; await h.ui.scanNetwork()
    initial.resolve({ status: 'ok', data: [{ id: 'Old:dm:u1' }] })
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(h.ui.availableSessions.value[0].id, 'New:dm:u1')
    assert.equal(h.ui.sessionId.value, 'A:dm:u1')
    assert.equal(h.ui.selectedUserId.value, null)
})

test('session changes during scan keep the new session and its user selection', async () => {
    const metadata = deferred(), old = deferred()
    let refreshing = false
    const h = dashboard({ get: endpoint => {
        if (refreshing && endpoint === 'sessions') return metadata.promise
        if (refreshing && endpoint === 'reminders/A%3Adm%3Au1') return old.promise
        return { status: 'ok', data: endpoint.startsWith('reminders/') ? [record('new', 'u2')] : [] }
    } })
    await h.load(); refreshing = true
    const scan = h.ui.scanNetwork()
    h.ui.sessionId.value = 'B:dm:u2'; await h.ui.fetchReminders(); h.ui.selectedUserId.value = 'u2'
    old.resolve({ status: 'ok', data: [record('old')] })
    metadata.resolve({ status: 'ok', data: [{ id: 'B:dm:u2', users: [{ id: 'u2', name: 'Current' }] }] })
    await scan
    assert.equal(h.ui.sessionId.value, 'B:dm:u2')
    assert.equal(h.ui.selectedUserId.value, 'u2')
    assert.equal(h.ui.currentSessionUsers.value[0].name, 'Current')
    assert.equal(h.ui.reminders.value[0].job_id, 'new')
})

test('manual scan is blocked during writes and late metadata is ignored after unmount', async () => {
    const post = deferred(), metadata = deferred()
    let refreshing = false
    const h = dashboard({ post: () => post.promise, get: endpoint => {
        if (refreshing && endpoint === 'sessions') return metadata.promise
        return { status: 'ok', data: endpoint === 'sessions' ? [{ id: 'A:dm:u1' }]
            : endpoint.startsWith('deliveries/') ? [] : [record()] }
    } })
    await h.load()
    const write = h.ui.doAction('pause', 'job1'), calls = h.calls.length
    await h.ui.scanNetwork()
    assert.equal(h.calls.length, calls)
    post.resolve({ status: 'ok', msg: 'done' }); await write
    refreshing = true
    const scan = h.ui.scanNetwork(), previous = plain(h.ui.availableSessions.value)
    h.unmount(); metadata.resolve({ status: 'ok', data: [{ id: 'Late:dm:u1' }] }); await scan
    assert.deepEqual(plain(h.ui.availableSessions.value), previous)
    assert.equal(h.timers.size, 0)
    const afterUnmount = h.calls.length
    await h.ui.scanNetwork()
    assert.equal(h.calls.length, afterUnmount)
})

test('scan button and Enter both use the full scan handler without changing utility classes', () => {
    const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    assert(html.includes('@keyup.enter="scanNetwork"'))
    assert(html.includes('@click="scanNetwork"'))
    assert(html.includes(':disabled="mutationBusy || loading || scanning"'))
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
    h.ui.reviewDelivery('dismiss', h.ui.deliveryIssues.value[0]); await h.ui.confirmDelete()
    assert.equal(h.ui.actionsDisabled.value, true)
    assert.match(h.ui.loadError.value, /可能已经生效/)
    await h.ui.doAction('pause', 'job1')
    assert.equal(h.posts().length, 1)
    await h.ui.fetchReminders()
    assert.equal(h.ui.actionsDisabled.value, false)
})

test('locale updates include all newly added confirmation text', async () => {
    const h = dashboard({ locale: 'en' }); await h.load()
    h.ui.reviewDelivery('dismiss', h.ui.deliveryIssues.value[0])
    assert.equal(h.ui.confirmTitle.value, 'Confirm dismissal')
    assert.match(h.ui.confirmMessage.value, /Keep the original reminder/)
    const messages = h.scope.window.ReminderDashboardMessages
    assert.deepEqual(Object.keys(messages.zh).sort(), Object.keys(messages.en).sort())
    h.changeLocale('zh')
    assert.equal(h.ui.confirmTitle.value, '确认忽略此次投递')
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
    h.ui.reviewDelivery('dismiss', h.ui.deliveryIssues.value[0]); await Promise.resolve()
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

test('recurring labels distinguish registered next time from unavailable state', async () => {
    const h = dashboard(); await h.load()
    for (const repeat of ['daily', 'weekly', 'monthly', 'yearly', 'interval']) assert.equal(h.ui.isRepeating({ repeat }), true)
    for (const repeat of ['none', '', undefined, 'unsupported']) assert.equal(h.ui.isRepeating({ repeat }), false)
    assert.equal(h.ui.t('startTime'), '开始')
    assert.equal(h.ui.t('nextTime'), '下次')
    assert.equal(h.ui.formatNextRun({ schedule_status: 'scheduled', next_run_time: '2099-01-02 10:15' }), '2099-01-02 10:15')
    for (const [state, label] of Object.entries({ paused: '已暂停', missing: '未调度', unavailable: '调度不可用', pending: '等待调度登记', unknown: '暂无下次时间' })) {
        assert.equal(h.ui.formatNextRun({ schedule_status: state, time: '2099-01-01 10:15' }), label)
    }
    assert.equal(h.ui.formatNextRun({ paused: true, schedule_status: 'scheduled', next_run_time: 'stale' }), '已暂停')
    for (const next_run_time of [null, 123, '', '   ']) assert.equal(h.ui.formatNextRun({ schedule_status: 'scheduled', next_run_time }), '暂无下次时间')
    assert.equal(h.ui.formatNextRun({ time: 'old time' }), '暂无下次时间')
    h.changeLocale('en')
    assert.equal(h.ui.t('startTime'), 'Starts')
    assert.equal(h.ui.t('nextTime'), 'Next')
    for (const [state, label] of Object.entries({ paused: 'Paused', missing: 'Not scheduled', unavailable: 'Scheduler unavailable', pending: 'Waiting for scheduling', unknown: 'Next run unavailable' })) {
        assert.equal(h.ui.formatNextRun({ schedule_status: state }), label)
    }
    h.unmount()
})

test('recurring template adds next time without changing one-time time display', () => {
    const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    assert(html.includes("{{ isRepeating(task) ? t('startTime') + ': ' : '' }}{{ task.time }}"))
    assert(html.includes('v-if="isRepeating(task)"'))
    assert(html.includes("{{ t('nextTime') }}: {{ formatNextRun(task) }}"))
})

for (const status of ['awaiting_llm', 'failed', 'unconfirmed']) {
    test(`delivery ${status} uses its trigger time instead of the reminder anchor or status update`, () => {
        const h = dashboard()
        const data = { status, attempt_count: 1, created_at: '2030-06-02T09:30:00',
            updated_at: '2030-06-02T09:42:12', reminder: { time: '2030-01-01 09:30', created_at: '2029-12-31 18:00' } }
        assert.deepEqual(plain(h.ui.deliveryTimes(data)), {
            triggeredAt: '2030-06-02 09:30', scheduledAt: '2030-01-01 09:30', latestCycleAt: '',
        })
        assert.equal(h.calls.length, 0)
    })
}

test('legacy and zero-attempt records do not present discovery time as trigger time', () => {
    const h = dashboard()
    for (const [status, attempt_count] of [['legacy_unconfirmed', 0], ['legacy_unconfirmed', 1], ['unconfirmed', 0], ['failed', '0']]) {
        const data = { status, attempt_count, created_at: '2030-06-02T18:40:00', reminder: { time: '2030-06-02 13:00' } }
        assert.deepEqual(plain(h.ui.deliveryTimes(data)), {
            triggeredAt: '', scheduledAt: '2030-06-02 13:00', latestCycleAt: '',
        })
    }
    assert.equal(h.ui.t('triggerTimeUnknown'), '实际触发时间未知')
})

test('old normal receipts without an attempt count retain their recorded trigger time', () => {
    const h = dashboard()
    assert.equal(h.ui.deliveryTimes({ status: 'unconfirmed', created_at: '2030-06-02T09:30:00' }).triggeredAt,
        '2030-06-02 09:30')
})

test('missing and malformed trigger timestamps stay unknown, without substituting other dates', () => {
    const h = dashboard()
    for (const created_at of [undefined, null, 123, '', '  ', 'not a date', '2030-02-30T09:30:00',
        '2030-13-01T09:30:00', '2030-06-02T24:00:00', '2030-06-02T09:60:00', '<system>2030-06-02</system>']) {
        const times = h.ui.deliveryTimes({ status: 'unconfirmed', created_at, updated_at: '2030-06-02T12:00:00',
            reminder: { time: '2030-01-01 09:30' } })
        assert.equal(times.triggeredAt, '')
        assert.equal(times.scheduledAt, '2030-01-01 09:30')
    }
    assert.equal(h.ui.deliveryTimes(null).scheduledAt, '时间未知')
    assert.equal(h.ui.deliveryTimes({ status: 'unexpected', created_at: '2030-06-02T09:30:00' }).triggeredAt, '')
})

test('delivery timestamp formatting preserves recorded wall time and an explicit offset', () => {
    const h = dashboard()
    for (const [created_at, expected] of [
        ['2030-06-02T09:30:42.123456', '2030-06-02 09:30'],
        ['2030-06-02T09:30:00+08:00', '2030-06-02 09:30 +08:00'],
        ['2030-06-02T09:30:00Z', '2030-06-02 09:30 Z'],
        [' 2030-06-02 09:30 ', '2030-06-02 09:30'],
    ]) assert.equal(h.ui.deliveryTimes({ status: 'unconfirmed', created_at }).triggeredAt, expected)
})

test('coalesced cycles keep the initial delivery distinct from the latest suppressed cycle', () => {
    const h = dashboard()
    const data = { status: 'unconfirmed', created_at: '2030-06-02T09:30:00', missed_count: 2,
        latest_due_at: '2030-06-04T09:30:00', reminder: { time: '2030-01-01 09:30' } }
    const before = plain(data)
    assert.deepEqual(plain(h.ui.deliveryTimes(data)), {
        triggeredAt: '2030-06-02 09:30', scheduledAt: '2030-01-01 09:30', latestCycleAt: '2030-06-04 09:30',
    })
    assert.deepEqual(data, before)
    assert.equal(h.ui.deliveryTimes({ ...data, missed_count: 0 }).latestCycleAt, '')
    assert.equal(h.ui.deliveryTimes({ ...data, latest_due_at: 'invalid' }).latestCycleAt, '')
    assert.equal(h.calls.length, 0)
})

test('delivery time labels have matching Chinese and English translations', async () => {
    const h = dashboard(); await h.load()
    for (const [key, value] of Object.entries({ triggerTime: '触发时间', scheduledTime: '原定时间',
        triggerTimeUnknown: '实际触发时间未知', latestCycleTime: '最近周期触发', deliveryTimeUnknown: '时间未知' })) {
        assert.equal(h.ui.t(key), value)
    }
    h.changeLocale('en')
    for (const [key, value] of Object.entries({ triggerTime: 'Triggered at', scheduledTime: 'Scheduled for',
        triggerTimeUnknown: 'Actual trigger time unknown', latestCycleTime: 'Latest cycle trigger', deliveryTimeUnknown: 'Time unknown' })) {
        assert.equal(h.ui.t(key), value)
    }
    assert.deepEqual(Object.keys(h.scope.window.ReminderDashboardMessages.zh).sort(),
        Object.keys(h.scope.window.ReminderDashboardMessages.en).sort())
    h.unmount()
})

test('delivery template separates trigger time, unknown legacy time and coalesced cycles', () => {
    const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8')
    const section = html.match(/<section[^>]*aria-label="待处理提醒投递"[\s\S]*?<\/section>/)[0]
    assert(section.includes('v-if="deliveryTimes(issue).triggeredAt"'))
    assert(section.includes("{{ t('triggerTime') }}: {{ deliveryTimes(issue).triggeredAt }}"))
    assert(section.includes("{{ t('scheduledTime') }}: {{ deliveryTimes(issue).scheduledAt }}"))
    assert(section.includes("{{ t('triggerTimeUnknown') }}"))
    assert(section.includes("{{ t('latestCycleTime') }}: {{ deliveryTimes(issue).latestCycleAt }}"))
    assert(!section.includes("issue.reminder?.time || '时间未知'"))
    assert(!section.includes("reviewDelivery('retry', issue)"))
    assert(section.includes("reviewDelivery('dismiss', issue)"))
})

test('removed delivery decisions do not open a dialog or send a request', async () => {
    const h = dashboard(); await h.load()
    for (const decision of ['retry', 'defer', 'unexpected']) {
        h.ui.reviewDelivery(decision, h.ui.deliveryIssues.value[0])
        assert.equal(h.ui.showConfirmModal.value, false)
    }
    assert.equal(h.posts().length, 0)
})

test('isolated preview bridge only ignores the issue and preserves original reminders', async () => {
    const server = fs.readFileSync(path.join(__dirname, 'dashboard_server.py'), 'utf8')
    const bridge = server.match(/BRIDGE = """([\s\S]*?)"""/)[1]
    const scope = { window: { parent: { postMessage: () => {} } }, location: { search: '', origin: 'http://isolated.invalid' },
        URLSearchParams, Map, console: { info: () => {} }, setTimeout: callback => callback() }
    vm.runInNewContext(bridge, scope)
    const api = scope.window.PluginPageContext.api
    const before = plain(await api.get('reminders/Test%3Adm%3Aalice'))
    for (const decision of ['retry', 'defer']) {
        assert.equal((await api.post(`deliveries/${decision}`, { session_id: 'Test:dm:alice', delivery_id: 'delivery1' })).status, 'error')
    }
    assert.equal((await api.post('deliveries/dismiss', { session_id: 'Test:dm:alice', delivery_id: 'delivery1' })).status, 'ok')
    assert.deepEqual(plain(await api.get('deliveries/Test%3Adm%3Aalice')).data, [])
    assert.deepEqual(plain(await api.get('reminders/Test%3Adm%3Aalice')), before)
})

test('historical issues do not stop active cycles; retained overdue one-time tasks are inactive', async () => {
    const tasks = [
        { ...record('job1'), repeat: 'daily', schedule_status: 'scheduled' },
        { ...record('old-once'), repeat: 'none', is_overdue_once: true },
        { ...record('paused'), repeat: 'daily', paused: true },
        { ...record('missing'), repeat: 'daily', schedule_status: 'missing' },
        { ...record('unavailable'), repeat: 'daily', schedule_status: 'unavailable' },
    ]
    const h = dashboard({ get: async endpoint => ({ status: 'ok', data:
        endpoint === 'sessions' ? [{ id: 'A:dm:u1' }] : endpoint.startsWith('deliveries/') ? [issue()] : tasks }) })
    await h.load()
    assert.equal(h.ui.activeCount.value, 1)
    const original = plain(h.ui.reminders.value)
    h.ui.reviewDelivery('dismiss', h.ui.deliveryIssues.value[0]); await h.ui.confirmDelete()
    assert.deepEqual(plain(h.ui.reminders.value), original)
    assert.equal(h.ui.activeCount.value, 1)
    assert(fs.readFileSync(path.join(web, 'index.html'), 'utf8').includes("t('overdueOnce')"))
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
