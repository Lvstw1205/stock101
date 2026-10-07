"""Small host adapter: caller enforces AIRI's existing origin/token checks."""
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from . import service

# The portfolio chart asset shares the existing local origin/token boundary.

ASSETS={'/stock101/quant.js':'quant.js','/stock101/agents.js':'agents.js','/stock101':'crypto.html','/stock101/':'crypto.html','/stock101/legacy':'index.html','/stock101/crypto.js':'crypto.js','/stock101/crypto.css':'crypto.css','/stock101/app.js':'app.js','/stock101/style.css':'style.css','/stock101/launcher.js':'launcher.js'}


ASSETS['/stock101/portfolio-charts.js']='portfolio-charts.js'
ASSETS['/stock101/learning.js']='learning.js'


def handle_get(app,handler):
    path=urlsplit(handler.path).path
    if path in ASSETS:
        name=ASSETS[path];raw=(app.ROOT/'web/stock101'/name).read_bytes()
        kind='text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
        handler.send_response(200);handler.headers_common();handler.send_header('Content-Type',kind+'; charset=utf-8');handler.send_header('Content-Length',str(len(raw)));handler.end_headers();handler.wfile.write(raw)
        return True
    if path.startswith('/api/stock101/'):
        try:
            query={k:v[0] for k,v in parse_qs(urlsplit(handler.path).query).items()}
            handler.send_json(service.get(app,path.removeprefix('/api/stock101/'),query))
        except (ValueError,KeyError,TypeError) as e:handler.send_json({'error':str(e)},400)
        except Exception:handler.send_json({'error':'Stock101 data service unavailable. Try again later.'},503)
        return True
    return False
