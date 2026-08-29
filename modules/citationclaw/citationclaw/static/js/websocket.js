// WebSocket连接管理器
const _wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

class WebSocketManager {
    constructor(url = `${_wsProto}//${window.location.host}/ws`) {
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
                    console.log(`${this.reconnectDelay/1000}秒后尝试重连 (${this.reconnectAttempts}/${this.maxReconnectAttempts})...`);
                    setTimeout(() => this.connect(), this.reconnectDelay);
                } else {
                    this.updateStatus('连接失败', 'danger');
                }
            };
        } catch (error) {
            console.error('WebSocket连接失败:', error);
            this.updateStatus('连接失败', 'danger');
        }
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
