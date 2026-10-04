const { createApp, ref, computed, watch, onMounted, onUnmounted, nextTick } = Vue
const MAX_TOASTS = 3

createApp({
    setup() {
        // UI state
        const loading = ref(false)
        const reminders = ref([])
        const deliveryIssues = ref([])
        const toasts = ref([])
        const pluginContext = ref(null)
        const locale = ref('zh')
        const loadedSessionId = ref('')
        const displayedSessionId = ref('')
        const mutationBusy = ref(false)
        const loadError = ref('')
        const deliveryStatusKnown = ref(false)
        const startupError = ref('')
        let requestSequence = 0
        let toastSequence = 0
        let unsubscribeContext = null
        let disposed = false
        const timers = new Set()
        const toastTimers = new Map()
        const t = (key) => {
            const messages = window.ReminderDashboardMessages
            return messages?.[locale.value]?.[key] || messages?.zh?.[key] || key
        }

        // Form state
        const sessionId = ref('')
        const availableSessions = ref([])
        const selectedUserId = ref(null)

        // Dropdown states
        const showSessionDropdown = ref(false)

        // Computed Dropdown logic
        const filteredSessions = computed(() => {
            if (!sessionId.value) return availableSessions.value
            // Show all sessions when the typed ID exactly matches an existing session.
            if (availableSessions.value.some(s => s.id === sessionId.value)) {
                return availableSessions.value
            }
            return availableSessions.value.filter(s => s.id.toLowerCase().includes(sessionId.value.toLowerCase()))
        })

        const selectSession = (s) => {
            if (mutationBusy.value) return
            sessionId.value = s.id
            selectedUserId.value = null // Reset the user filter.
            showSessionDropdown.value = false
            fetchReminders() // Fetch reminders for the selected session.
        }

        // Modal state
        const showConfirmModal = ref(false)
        const confirmMessage = ref('')
        const deleteToken = ref('')
        const pendingOperation = ref(null)
        const modalElement = ref(null)
        let previousFocus = null
        const needsDeleteToken = computed(() => pendingOperation.value?.kind === 'token')
        const confirmTitle = computed(() => t(needsDeleteToken.value ? 'tokenTitle'
            : pendingOperation.value?.kind === 'delivery' ? `${pendingOperation.value.decision}Title` : 'deleteTitle'))
        const confirmButton = computed(() => mutationBusy.value ? t('submitting')
            : t(pendingOperation.value?.decision || 'delete'))
        const dataReady = computed(() => !loading.value && !!loadedSessionId.value
            && loadedSessionId.value === sessionId.value.trim())
        const actionsDisabled = computed(() => !dataReady.value || mutationBusy.value)
        const hasCurrentSnapshot = computed(() => !!displayedSessionId.value
            && displayedSessionId.value === sessionId.value.trim())

        // Counts reflect the filtered view.
        const pendingJobIds = computed(() => new Set(deliveryIssues.value.map(issue => issue.job_id)))
        const activeCount = computed(() => deliveryStatusKnown.value
            ? filteredReminders.value.filter(r => !r.paused && !pendingJobIds.value.has(r.job_id)).length : null)
        const pausedCount = computed(() => filteredReminders.value.filter(r => r.paused).length)
        const importantCount = computed(() => filteredReminders.value.filter(r => r.important).length)

        // Derived computing
        const currentSessionUsers = computed(() => {
            const s = availableSessions.value.find(s => s.id === sessionId.value)
            return s && Array.isArray(s.users) ? s.users : []
        })

        const filteredReminders = computed(() => {
            if (!selectedUserId.value) return reminders.value
            return reminders.value.filter(r => r.creator_id === selectedUserId.value)
        })
        const filteredDeliveryIssues = computed(() => selectedUserId.value === null ? deliveryIssues.value
            : deliveryIssues.value.filter(issue =>
                (issue.reminder?.owner_id || issue.reminder?.creator_id) === selectedUserId.value))

        watch(sessionId, () => {
            requestSequence += 1
            loadedSessionId.value = ''
            displayedSessionId.value = ''
            reminders.value = []
            deliveryIssues.value = []
            selectedUserId.value = null
            deliveryStatusKnown.value = false
            loadError.value = ''
            loading.value = false
            closeModal(true)
        }, { flush: 'sync' })

        const dismissToast = id => {
            const timer = toastTimers.get(id)
            if (timer !== undefined) {
                clearTimeout(timer)
                timers.delete(timer)
                toastTimers.delete(id)
            }
            toasts.value = toasts.value.filter(toast => toast.id !== id)
        }

        const showToast = (title, message, type = 'success') => {
            if (disposed) return
            const existing = toasts.value.find(toast => toast.title === title
                && toast.message === message && toast.type === type)
            if (existing) {
                dismissToast(existing.id)
            } else if (toasts.value.length >= MAX_TOASTS) {
                // Successful operations must not displace visible error messages.
                const oldestSuccess = toasts.value.find(toast => toast.type !== 'error')
                if (!oldestSuccess && type !== 'error') return
                dismissToast((oldestSuccess || toasts.value[0]).id)
            }
            const toast = existing || { id: ++toastSequence, title, message, type }
            toasts.value.push(toast)
            const timer = setTimeout(() => {
                if (toastTimers.get(toast.id) === timer) dismissToast(toast.id)
            }, 4000)
            toastTimers.set(toast.id, timer)
            timers.add(timer)
        }

        const pluginApi = () => {
            if (!window.PluginPageContext || !pluginContext.value) {
                throw new Error('PluginPageContext 尚未就绪')
            }
            return window.PluginPageContext.api
        }

        const responseData = (data, list = false) => {
            if (!data || !['ok', 'error'].includes(data.status)) {
                throw new Error(typeof data?.detail === 'string' ? data.detail : t('invalidResponse'))
            }
            if (data.status === 'error') throw new Error(data.msg || t('invalidResponse'))
            if (list && (!Array.isArray(data.data) || data.data.some(item => !item || typeof item !== 'object'))) {
                throw new Error(t('invalidResponse'))
            }
            return list ? data.data : data
        }

        const readApi = async (endpoint, params) => {
            let timer
            try {
                return await Promise.race([
                    pluginApi().get(endpoint, params),
                    new Promise((_, reject) => {
                        timer = setTimeout(() => reject(new Error(t('readTimeout'))), 12000)
                        timers.add(timer)
                    }),
                ])
            } finally {
                clearTimeout(timer)
                timers.delete(timer)
            }
        }

        const fetchReminders = async (allowDuringMutation = false) => {
            if (mutationBusy.value && allowDuringMutation !== true) return
            showSessionDropdown.value = false
            const sid = sessionId.value.trim()
            if (!sid) {
                displayedSessionId.value = ''
                reminders.value = [] // Clear stale results on invalid input.
                deliveryIssues.value = []
                return showToast('参数校验失败', '必须提供目标频率基站 (Session ID)', 'error')
            }
            if (sessionId.value !== sid) sessionId.value = sid
            const sequence = ++requestSequence
            const isCurrent = () => sequence === requestSequence && sid === sessionId.value.trim()
            loading.value = true
            loadedSessionId.value = ''
            // Keep the current session's DOM height stable during background refresh.
            if (!hasCurrentSnapshot.value) {
                reminders.value = []
                deliveryIssues.value = []
            }
            deliveryStatusKnown.value = false
            loadError.value = ''
            try {
                const data = await readApi(`reminders/${encodeURIComponent(sid)}`, { _t: Date.now() })
                if (!isCurrent()) return
                const nextReminders = responseData(data, true)
                const nextDelivery = await fetchDeliveryIssues(sid, isCurrent)
                if (!isCurrent()) return
                reminders.value = nextReminders
                deliveryIssues.value = nextDelivery.issues
                deliveryStatusKnown.value = nextDelivery.known
                displayedSessionId.value = sid
                loadedSessionId.value = sid
                if (!nextDelivery.known) showToast(t('deliveryUnavailable'), nextDelivery.error, 'error')
            } catch (e) {
                if (!isCurrent()) return
                reminders.value = [] // Clear stale data after a connection failure.
                deliveryIssues.value = []
                displayedSessionId.value = ''
                loadError.value = e.message || t('unavailable')
                showToast(t('operationFailed'), loadError.value, 'error')
            } finally {
                if (isCurrent()) loading.value = false
            }
        }

        const fetchDeliveryIssues = async (sid, isCurrent) => {
            try {
                const data = await readApi(`deliveries/${encodeURIComponent(sid)}`, { _t: Date.now() })
                if (!isCurrent()) return
                return { issues: responseData(data, true), known: true }
            } catch (e) {
                if (!isCurrent()) return
                return { issues: [], known: false, error: e.message }
            }
        }

        const openModal = (operation, message) => {
            if (!showConfirmModal.value) previousFocus = document.activeElement
            pendingOperation.value = Object.freeze(operation)
            confirmMessage.value = message
            deleteToken.value = ''
            showConfirmModal.value = true
            nextTick(() => modalElement.value?.querySelector('button')?.focus({ preventScroll: true }))
        }

        const reviewDelivery = (decision, issue) => {
            if (actionsDisabled.value) return
            const current = deliveryIssues.value.find(item => item.delivery_id === issue?.delivery_id)
            if (!['retry', 'dismiss'].includes(decision) || !current || !current.delivery_id
                || current.status === 'awaiting_llm') {
                return showToast(t('operationFailed'), t('invalidTarget'), 'error')
            }
            openModal({kind: 'delivery', decision, sid: loadedSessionId.value,
                deliveryId: current.delivery_id, content: current.reminder?.content || ''}, t(`${decision}Warning`))
        }

        const runMutation = async (endpoint, payload) => {
            if (actionsDisabled.value || payload.session_id !== loadedSessionId.value) return
            const focusToRestore = showConfirmModal.value ? previousFocus : null
            mutationBusy.value = true
            try {
                const data = await pluginApi().post(endpoint, payload)
                if (disposed) return
                if (!data || !['ok', 'error'].includes(data.status)) throw new Error(t('invalidResponse'))
                if (data.status === 'ok') {
                    closeModal(true, false)
                    const message = sessionId.value.trim() === payload.session_id ? data.msg
                        : `${payload.session_id}: ${data.msg || ''}`
                    showToast(endpoint.startsWith('deliveries/') ? t('saved') : t('taskSaved'), message || '')
                    if (sessionId.value.trim() === payload.session_id) await fetchReminders(true)
                } else {
                    const message = typeof data.msg === 'string' ? data.msg : t('invalidResponse')
                    if (endpoint === 'reminders/delete' && message.includes('请确认删除令牌')
                        && sessionId.value.trim() === payload.session_id) {
                        openModal({kind: 'token', sid: payload.session_id, jobId: payload.job_id}, message)
                    } else {
                        showToast(t('operationFailed'), message, 'error')
                    }
                }
            } catch (e) {
                closeModal(true, false)
                loadedSessionId.value = ''
                loadError.value = t('unknownOutcome')
                showToast(t('operationFailed'), `${t('unknownOutcome')} ${e.message || ''}`, 'error')
            } finally {
                mutationBusy.value = false
                nextTick(() => {
                    if (!disposed && !showConfirmModal.value && focusToRestore?.isConnected
                        && !focusToRestore.disabled) focusToRestore.focus({ preventScroll: true })
                })
            }
        }

        const doAction = async (action, jobId) => {
            if (actionsDisabled.value) return
            const task = reminders.value.find(item => item.job_id === jobId)
            if (!['delete', 'pause', 'resume'].includes(action) || !task || !jobId) {
                return showToast(t('operationFailed'), t('invalidTarget'), 'error')
            }
            const sid = loadedSessionId.value
            if (action === 'delete') {
                return openModal({kind: 'delete', sid, jobId, content: task.content}, t('deleteWarning'))
            }
            await runMutation(`reminders/${action}`, {session_id: sid, job_id: jobId})
        }

        const closeModal = (force = false, restoreFocus = true) => {
            if (mutationBusy.value && force !== true) return
            const focusToRestore = showConfirmModal.value ? previousFocus : null
            previousFocus = null
            showConfirmModal.value = false
            pendingOperation.value = null
            deleteToken.value = ''
            if (restoreFocus) nextTick(() => {
                if (!disposed && !showConfirmModal.value && focusToRestore?.isConnected
                    && !focusToRestore.disabled) focusToRestore.focus({ preventScroll: true })
            })
        }

        const confirmDelete = async () => {
            const operation = pendingOperation.value
            if (!operation || actionsDisabled.value || operation.sid !== loadedSessionId.value) return
            if (operation.kind === 'delivery') {
                return runMutation(`deliveries/${operation.decision}`, {
                    session_id: operation.sid, delivery_id: operation.deliveryId,
                })
            }
            if (operation.kind === 'delete') {
                return runMutation('reminders/delete', {
                    session_id: operation.sid, job_id: operation.jobId, force: false,
                })
            }
            if (!deleteToken.value.trim()) {
                return showToast('校验失败', '请输入防御解除令牌', 'warning')
            }
            await runMutation('reminders/confirm-delete', {
                session_id: operation.sid, confirm_token: deleteToken.value.trim(),
            })
        }

        const trapModalFocus = (event) => {
            const controls = modalElement.value?.querySelectorAll('button:not(:disabled), input:not(:disabled)')
            if (!controls?.length) return
            const first = controls[0], last = controls[controls.length - 1]
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus({ preventScroll: true }) }
            if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus({ preventScroll: true }) }
        }

        const formatRepeat = (repeat, interval) => {
            const map = {
                'none': '单次部署',
                'daily': '每日战术循环',
                'weekly': '周常矩阵',
                'monthly': '月度计划',
                'yearly': '年度方针',
                'interval': `高频轮询 ${interval || 0}m`
            }
            return map[repeat] || '单次部署'
        }

        const fetchSessions = async () => {
            try {
                const data = await readApi('sessions', { _t: Date.now() })
                availableSessions.value = responseData(data, true).filter(session => typeof session.id === 'string')
            } catch (e) {
                showToast(t('sessionFailed'), e.message, 'error')
            }
        }

        // Initial fetch
        onMounted(async () => {
            const fallback = document.getElementById('startup-fallback')
            if (fallback) fallback.hidden = true
            let contextTimer
            try {
                if (!window.PluginPageContext) throw new Error(t('contextFailed'))
                pluginContext.value = await Promise.race([
                    window.PluginPageContext.ready(),
                    new Promise((_, reject) => {
                        contextTimer = setTimeout(() => reject(new Error(t('contextTimeout'))), 8000)
                        timers.add(contextTimer)
                    }),
                ])
                if (disposed) return
                const updateLocale = context => { locale.value = String(context?.locale || 'zh').startsWith('en') ? 'en' : 'zh' }
                updateLocale(pluginContext.value)
                if (window.PluginPageContext.onContext) unsubscribeContext = window.PluginPageContext.onContext(updateLocale)
            } catch (e) {
                startupError.value = e.message || t('contextFailed')
                return
            } finally {
                clearTimeout(contextTimer)
                timers.delete(contextTimer)
            }

            fetchSessions()

            const urlParams = new URLSearchParams(window.location.search)
            const sid = urlParams.get('sid')
            if (sid) {
                sessionId.value = sid
                fetchReminders()
            }
        })

        onUnmounted(() => {
            disposed = true
            requestSequence += 1
            for (const timer of timers) clearTimeout(timer)
            timers.clear()
            toastTimers.clear()
            unsubscribeContext?.()
        })

        return {
            loading, reminders, deliveryIssues, filteredDeliveryIssues, pendingJobIds, toasts, dismissToast, availableSessions,
            sessionId, selectedUserId, currentSessionUsers, filteredReminders,
            showSessionDropdown,
            filteredSessions,
            selectSession,
            activeCount, pausedCount, importantCount,
            fetchReminders, doAction, reviewDelivery, formatRepeat,
            showConfirmModal, confirmMessage, deleteToken, closeModal, confirmDelete,
            pendingOperation, needsDeleteToken, confirmTitle, confirmButton, modalElement, trapModalFocus,
            mutationBusy, actionsDisabled, loadError, deliveryStatusKnown, startupError, hasCurrentSnapshot, t
        }
    }
}).mount('#app')
