import os


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mrc-emulator-dev-key')
    ROCE_UDP_PORT = int(os.environ.get('ROCE_UDP_PORT', '4971'))
    LISTEN_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
    LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '8080'))
    DEBUG = os.environ.get('FLASK_DEBUG', '1') == '1'

    DSCP_NO_TRIM = int(os.environ.get('DSCP_NO_TRIM', '0'))
    DSCP_TRIMMABLE = int(os.environ.get('DSCP_TRIMMABLE', '4'))
    DSCP_TRIMMABLE_RETX = int(os.environ.get('DSCP_TRIMMABLE_RETX', '8'))
    DSCP_TRIMMED = int(os.environ.get('DSCP_TRIMMED', '12'))
    DSCP_TRIMMED_LASTHOP = int(os.environ.get('DSCP_TRIMMED_LASTHOP', '16'))
    DSCP_CONTROL = int(os.environ.get('DSCP_CONTROL', '46'))
