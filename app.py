"""
MRC emu — OCP MRC End-Host Emulator v1.0

Web-based GUI for configuring and operating an MRC endpoint emulator.
Run with: python3 app.py
Access at: http://<host>:8080
"""

import os
from flask import Flask
from config import Config


class ReverseProxyMiddleware:
    """Set SCRIPT_NAME from URL_PREFIX env var or X-Forwarded-Prefix header."""

    def __init__(self, app):
        self.app = app
        self.prefix = os.environ.get('URL_PREFIX', '').rstrip('/')

    def __call__(self, environ, start_response):
        prefix = (environ.get('HTTP_X_FORWARDED_PREFIX', '')
                  or environ.get('HTTP_X_SCRIPT_NAME', '')
                  or self.prefix)
        if prefix:
            environ['SCRIPT_NAME'] = prefix.rstrip('/')
        return self.app(environ, start_response)

from core.runtime import detect_runtime, get_network_manager, RuntimeMode
from core.ev_engine import EVProfileManager
from core.qp_manager import QPManager
from core.congestion import QPCCManager
from core.probing import ProbeManager


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    runtime = detect_runtime()
    app.runtime = runtime
    app.config['OFFLINE_MODE'] = runtime.mode == RuntimeMode.OFFLINE

    app.ev_manager = EVProfileManager()
    app.qp_manager = QPManager()
    app.net_manager = get_network_manager(runtime)
    app.cc_manager = QPCCManager()
    app.probe_manager = ProbeManager()

    from routes.host import host_bp
    from routes.network import network_bp
    from routes.ev import ev_bp
    from routes.qp import qp_bp
    from routes.packets import packets_bp
    from routes.cc import cc_bp
    from routes.probing import probing_bp
    from routes.topology import topology_bp
    from routes.traffic import traffic_bp
    from routes.faults import faults_bp
    from routes.pathstate import pathstate_bp
    from routes.host_agent import host_agent_bp
    from routes.controller import controller_bp

    app.register_blueprint(host_bp)
    app.register_blueprint(network_bp)
    app.register_blueprint(ev_bp)
    app.register_blueprint(qp_bp)
    app.register_blueprint(packets_bp)
    app.register_blueprint(cc_bp)
    app.register_blueprint(probing_bp)
    app.register_blueprint(topology_bp)
    app.register_blueprint(traffic_bp)
    app.register_blueprint(faults_bp)
    app.register_blueprint(pathstate_bp)
    app.register_blueprint(host_agent_bp)
    app.register_blueprint(controller_bp)

    app.wsgi_app = ReverseProxyMiddleware(app.wsgi_app)

    print(f'MRC emu v1.0 — {runtime.mode.name} mode ({runtime.reason})')

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host=Config.LISTEN_HOST, port=Config.LISTEN_PORT, debug=Config.DEBUG)
