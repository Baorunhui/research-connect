// WebSocket连接管理器
const _wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

class WebSocketManager {
    constructor(url = `${_wsProto}//${window.location.host}${window.CCR_PUBLIC_API_BASE || ''}/ws`) {
        this.url = url;
        this.ws = null;
        this.handlers = {
            log: [],
            progress: [],
            url_captured: [],
            history: []
        };
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.reconnectDelay = 3000; // 3秒
    }

    connect() {
        if (window.CCR_PUBLIC_API_BASE && !window.CCR_WS_ENABLED) {
            this.connectPolling();
            return;
        }
        try {
            this.ws = new WebSocket(this.url);

            this.ws.onopen = () => {
                console.log('WebSocket已连接');
                this.reconnectAttempts = 0;
                this.updateStatus('已连接', 'success');
            };

            this.ws.onmessage = (event) => {
                let message;
                try {
                    message = JSON.parse(event.data);
                } catch (e) {
                    console.error('WebSocket: malformed message', e);
                    return;
                }
                const handlers = this.handlers[message.type] || [];
                handlers.forEach(handler => handler(message.data));
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket错误:', error);
                this.updateStatus('连接错误', 'danger');
            };

            this.ws.onclose = () => {
                console.log('WebSocket已断开');
                this.updateStatus('已断开', 'warning');

                // 自动重连
                if (this.reconnectAttempts < this.maxReconnectAttempts) {
                    this.reconnectAttempts++;
                    setTimeout(() => this.connect(), this.reconnectDelay);
                } else if (!this.pollTimer) {
                    this.connectPolling({ catchCompleted: true });
                }
            };
        } catch (error) {
            console.error('WebSocket连接失败:', error);
            this.updateStatus('连接失败', 'danger');
        }
    }

    connectPolling(options) {
        if (this.pollTimer) return;
        const catchCompleted = !!(options && options.catchCompleted);
        this.updateStatus('改为定时刷新', 'warning');
        this.emit('log', {
            timestamp: new Date().toISOString(),
            level: 'WARNING',
            message: '实时连接中断，改为每 2 秒刷新进度。查完后结果仍会显示在这里。'
        });
        let previousStatus = 'idle';
        let announced = false;
        const poll = async () => {
            try {
                const base = String(window.CCR_PUBLIC_API_BASE || '').replace(/\/$/, '');
                const response = await fetch(base + '/api/task/status');
                if (!response.ok) throw new Error('HTTP ' + response.status);
                const data = await response.json();
                const logs = Array.isArray(data.logs) ? data.logs : [];
                // The status payload is only the latest lines. Replaying them is safe;
                // the page skips a line it already showed, including after the list stops growing.
                logs.forEach(item => this.emit('log', item));
                if (data.progress) this.emit('progress', data.progress);
                const status = data.status || (data.is_running ? 'running' : 'idle');
                const runBtn = document.getElementById('idx-run-btn');
                const waiting = !!(runBtn && runBtn.disabled);
                if (!announced && status === 'completed' && data.result
                    && (previousStatus === 'running' || (catchCompleted && waiting))) {
                    announced = true;
                    this.emit('all_done', data.result);
                }
                if (previousStatus === 'running' && (status === 'failed' || status === 'cancelled')) {
                    this.emit('task_finished', {status, message: data.error || '任务已结束'});
                }
                previousStatus = status;
                this.updateStatus('改为定时刷新', 'success');
            } catch (error) {
                this.updateStatus('进度刷新失败', 'warning');
            }
        };
        poll();
        this.pollTimer = setInterval(poll, 2000);
    }

    emit(type, data) {
        (this.handlers[type] || []).forEach(handler => handler(data));
    }

    on(event, handler) {
        if (!this.handlers[event]) {
            this.handlers[event] = [];
        }
        this.handlers[event].push(handler);
    }

    off(type, handler) {
        this.handlers[type] = (this.handlers[type] || []).filter(h => h !== handler);
    }

    disconnect() {
        if (this.pollTimer) clearInterval(this.pollTimer);
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
        this.handlers = {};
    }

    updateStatus(text, type) {
        const statusEl = document.getElementById('ws-status');
        if (statusEl) {
            statusEl.textContent = text;
            statusEl.className = `badge bg-${type === 'success' ? 'success' : type === 'warning' ? 'warning' : 'danger'} float-end`;
        }
    }
}

// 全局WebSocket实例
let wsManager = null;
