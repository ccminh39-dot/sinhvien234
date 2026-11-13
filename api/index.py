import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from wsgi_server import app, application
from web_server import AgentServerHandler

class handler(AgentServerHandler):
    pass
