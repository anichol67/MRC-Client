"""
OCP MRC End-Host Emulator — Flask Application

Web-based GUI for configuring and operating an MRC endpoint emulator.
Run with: python app.py
Access at: http://<host>:5000
"""

from flask import Flask
from config import Config

from core.ev_engine import EVProfileManager
from core.qp_manager import QPManager
from core.network_config import NetworkConfigManager
from core.congestion import QPCCManager
from core.probing import ProbeManager


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    app.ev_manager = EVProfileManager()
    app.qp_manager = QPManager()
    app.net_manager = NetworkConfigManager()
    app.cc_manager = QPCCManager()
    app.probe_manager = ProbeManager()

    from routes.host import host_bp
    from routes.network import network_bp
    from routes.ev import ev_bp
    from routes.qp import qp_bp
    from routes.packets import packets_bp
    from routes.cc import cc_bp
    from routes.probing import probing_bp

    app.register_blueprint(host_bp)
    app.register_blueprint(network_bp)
    app.register_blueprint(ev_bp)
    app.register_blueprint(qp_bp)
    app.register_blueprint(packets_bp)
    app.register_blueprint(cc_bp)
    app.register_blueprint(probing_bp)

    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host=Config.LISTEN_HOST, port=Config.LISTEN_PORT, debug=Config.DEBUG)
