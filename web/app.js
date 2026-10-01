const { createApp, ref, computed, onMounted } = Vue

createApp({
    setup() {
        // UI state
        const loading = ref(false)
        const reminders = ref([])
        const toasts = ref([])
        const pluginContext = ref(null)

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
            sessionId.value = s.id
            selectedUserId.value = null // Reset the user filter.
            showSessionDropdown.value = false
            fetchReminders() // Fetch reminders for the selected session.
        }

        // Modal state
        const showConfirmModal = ref(false)
        const confirmMessage = ref('')
        const currentJobId = ref('')
        const deleteToken = ref('')

        // Counts reflect the filtered view.
        const activeCount = computed(() => filteredReminders.value.filter(r => !r.paused).length)
        const pausedCount = computed(() => filteredReminders.value.filter(r => r.paused).length)
        const importantCount = computed(() => filteredReminders.value.filter(r => r.important).length)

        // Derived computing
        const currentSessionUsers = computed(() => {
            const s = availableSessions.value.find(s => s.id === sessionId.value)
            return s ? s.users || [] : []
        })

        const filteredReminders = computed(() => {
            if (!selectedUserId.value) return reminders.value
            return reminders.value.filter(r => r.creator_id === selectedUserId.value)
        })

        const showToast = (title, message, type = 'success') => {
            const id = Date.now()
            toasts.value.push({ id, title, message, type })
            setTimeout(() => {
                toasts.value = toasts.value.filter(t => t.id !== id)
            }, 4000)
        }

        const pluginApi = () => {
            if (!window.PluginPageContext || !pluginContext.value) {
                throw new Error('PluginPageContext 尚未就绪')
            }
            return window.PluginPageContext.api
        }

        const fetchReminders = async () => {
            showSessionDropdown.value = false

            if (!sessionId.value.trim()) {
                reminders.value = [] // Clear stale results on invalid input.
                return showToast('参数校验失败', '必须提供目标频率基站 (Session ID)', 'error')
            }
            loading.value = true
            try {
                const data = await pluginApi().get(`reminders/${encodeURIComponent(sessionId.value)}`, { _t: Date.now() })
                if (data.status === 'ok') {
                    reminders.value = data.data
                } else {
                    reminders.value = [] // Clear data when access is denied.
                    showToast('越权或拦截', data.msg, 'error')
                }
            } catch (e) {
                reminders.value = [] // Clear stale data after a connection failure.
                showToast('链路断开', '无法握手微服务子节点', 'error')
            } finally {
                loading.value = false
            }
        }

        const doAction = async (action, jobId, force = true) => {
            try {
                const data = await pluginApi().post(`reminders/${action}`, {
                    session_id: sessionId.value,
                    job_id: jobId,
                    force: force
                })
                if (data.status === 'ok') {
                    showToast('指令下达成功', data.msg)
                    await fetchReminders() // reload
                } else {
                    if (data.msg.includes('⚠') || data.msg.includes('请确认删除令牌')) {
                        // Keep the rejection reason compact in the confirmation dialog.
                        confirmMessage.value = data.msg.replace(/\\n/g, '  ')
                        currentJobId.value = jobId
                        deleteToken.value = '' // reset input
                        showConfirmModal.value = true
                    } else {
                        showToast('防火墙阻断', data.msg, 'error')
                    }
                }
            } catch (e) {
                showToast('网络波动', e.message, 'error')
            }
        }

        const closeModal = () => {
            showConfirmModal.value = false
        }

        const confirmDelete = async () => {
            if (!deleteToken.value.trim()) {
                return showToast('校验失败', '请输入防御解除令牌', 'warning')
            }
            try {
                const data = await pluginApi().post('reminders/confirm-delete', {
                    session_id: sessionId.value,
                    confirm_token: deleteToken.value.trim()
                })
                if (data.status === 'ok') {
                    closeModal()
                    showToast('确认成功', data.msg)
                    await fetchReminders()
                } else {
                    showToast('令牌校验失败', data.msg, 'error')
                }
            } catch (e) {
                showToast('网络波动', e.message, 'error')
            }
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
                const data = await pluginApi().get('sessions', { _t: Date.now() })
                if (data.status === 'ok') {
                    availableSessions.value = data.data
                }
            } catch (e) {
                console.error("[KiraAI] 无法拉取存量会话列表", e)
            }
        }

        // Initial fetch
        onMounted(async () => {
            if (window.PluginPageContext) {
                pluginContext.value = await window.PluginPageContext.ready()
            } else {
                showToast('上下文缺失', '请从 KiraAI WebUI 侧边栏打开本页面', 'error')
                return
            }

            fetchSessions()

            const urlParams = new URLSearchParams(window.location.search)
            const sid = urlParams.get('sid')
            if (sid) {
                sessionId.value = sid
                fetchReminders()
            }
        })

        return {
            loading, reminders, toasts, availableSessions,
            sessionId, selectedUserId, currentSessionUsers, filteredReminders,
            showSessionDropdown,
            filteredSessions,
            selectSession,
            activeCount, pausedCount, importantCount,
            fetchReminders, doAction, formatRepeat,
            showConfirmModal, confirmMessage, deleteToken, closeModal, confirmDelete
        }
    }
}).mount('#app')
