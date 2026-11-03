"""Setup planning for the SI-M Bridge.

This module never changes system state. It inspects what is available on the
host, then *plans* one reviewed exposure route and renders the operator steps.
Applying them stays an explicit human action -- that is the point: every
operator picks their own reachability model for their own network.
"""
import ipaddress
import re
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

# Exposure modes, ordered from "least moving parts" to "most moving parts".
# `device_capable` marks routes the Orb firmware can actually reach: it speaks
# HTTPS with system CAs only, so loopback and plain LAN HTTP are excluded.
MODES = {
    'tailscale': {
        'title': 'Tailscale Serve (nur Tailnet-Clients)',
        'summary': 'Nur fuer Tailnet-Clients; ein normaler WLAN-Orb erreicht Tailscale Serve nicht.',
        'device_capable': False,
    },
    'caddy': {
        'title': 'Caddy mit eigener Domain',
        'summary': 'Caddy holt automatisch ein Let\'s-Encrypt-Zertifikat; Port 80/443 muessen erreichbar sein.',
        'device_capable': True,
    },
    'nginx': {
        'title': 'nginx mit eigener Domain',
        'summary': 'Vorhandenes nginx plus Zertifikat (z.B. certbot). Mehr Handarbeit, volle Kontrolle.',
        'device_capable': True,
    },
    'cloudflare': {
        'title': 'Cloudflare Tunnel',
        'summary': 'Ausgehender Tunnel, keine Portfreigabe. Braucht einen Cloudflare-Account und eine Domain.',
        'device_capable': True,
    },
    'existing': {
        'title': 'Bereits vorhandener HTTPS-Endpunkt',
        'summary': 'Du hast schon einen Reverse Proxy oder eine oeffentliche URL; wir validieren sie nur.',
        'device_capable': True,
    },
    'loopback': {
        'title': 'Nur lokal (Tests)',
        'summary': 'Bindet ausschliesslich an 127.0.0.1. Ein Orb kann diesen Endpunkt NICHT erreichen.',
        'device_capable': False,
    },
}

DEFAULT_PORT = 8766


def _run(argv, timeout=8):
    """Run a probe command without a shell; never raises."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False, check=False)
        return done.returncode, done.stdout, done.stderr
    except (OSError, subprocess.SubprocessError):
        return 127, '', ''


def probe_tailscale():
    """Return the tailnet DNS name if tailscaled is up, else None."""
    binary = shutil.which('tailscale')
    if not binary:
        return {'available': False, 'dns_name': None, 'reason': 'tailscale ist nicht installiert'}
    code, out, _ = _run([binary, 'status', '--json'])
    if code != 0:
        return {'available': False, 'dns_name': None, 'reason': 'tailscaled laeuft nicht oder ist nicht angemeldet'}
    try:
        status = json.loads(out)
    except ValueError:
        return {'available': False, 'dns_name': None, 'reason': 'Status nicht lesbar'}
    if status.get('BackendState') != 'Running':
        return {'available': False, 'dns_name': None,
                'reason': 'BackendState=%s' % status.get('BackendState')}
    name = (status.get('Self') or {}).get('DNSName') or ''
    name = name.rstrip('.')
    if not name.endswith('.ts.net'):
        # MagicDNS/HTTPS disabled: usable with a manual cert, but not automatic.
        return {'available': False, 'dns_name': None,
                'reason': 'kein MagicDNS-Name (HTTPS-Zertifikate im Tailnet deaktiviert?)'}
    return {'available': True, 'dns_name': name, 'reason': 'ok'}


def probe_binary(name):
    path = shutil.which(name)
    return {'available': bool(path), 'path': path}


def validate_public_url(url):
    """Device-facing URL must be https and carry no credentials or query."""
    if not isinstance(url, str) or not url:
        raise ValueError('URL fehlt')
    if any(ord(c) < 33 or ord(c) > 126 for c in url) or any(c in url for c in ('\\', '%')):
        raise ValueError('URL enthaelt unerlaubte Zeichen')
    parts = urlsplit(url)
    if parts.scheme != 'https':
        raise ValueError('Geraete-URL muss https sein (die Firmware vertraut nur System-CAs)')
    if '@' in parts.netloc or parts.fragment or parts.query or parts.path not in ('', '/'):
        raise ValueError('URL darf keine Zugangsdaten, Pfade, Query oder Fragment enthalten')
    host = parts.hostname
    if not host:
        raise ValueError('URL ohne Host')
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError('ungueltiger URL-Port') from exc
    if ':' in parts.netloc.rsplit(']', 1)[-1] and port is None:
        raise ValueError('ungueltiger URL-Port')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or not all(
        label and len(label) <= 63 and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label)
        for label in host.split('.')):
        raise ValueError('ungueltiger Hostname')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_link_local):
        raise ValueError('Loopback-/Link-Local-Adresse ist fuer den Orb nicht erreichbar')
    return url.rstrip('/')


def plan_exposure(mode, *, domain=None, port=DEFAULT_PORT, url=None, tailnet_name=None):
    """Build the plan for one exposure mode. Pure function, no side effects."""
    if mode not in MODES:
        raise ValueError('unbekannter Modus: %s' % mode)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('ungueltiger Port')

    plan = {
        'mode': mode,
        'title': MODES[mode]['title'],
        'device_capable': MODES[mode]['device_capable'],
        'port': port,
        'prerequisites': [],
        'missing': [],
        'steps': [],
        'files': {},
        'base_url': None,
        'notes': [],
    }

    def need(ok, label):
        plan['prerequisites'].append({'check': label, 'ok': bool(ok)})
        if not ok:
            plan['missing'].append(label)

    if mode == 'loopback':
        plan['base_url'] = 'http://127.0.0.1:%d' % port
        plan['notes'].append('Nur fuer Tests auf demselben Rechner. Der Orb kann hier nicht koppeln.')
        plan['steps'] = ['python cli.py --db "$SIM_CONNECTOR_DB" serve']
        return plan

    if mode == 'tailscale':
        probe = probe_tailscale() if tailnet_name is None else {'available': bool(tailnet_name), 'dns_name': tailnet_name}
        need(probe['available'], 'Tailscale laeuft mit MagicDNS (%s)' % probe.get('reason', ''))
        name = probe.get('dns_name')
        if name:
            validate_public_url('https://' + name)
            if not name.endswith('.ts.net'):
                raise ValueError('Tailnet-Name muss auf .ts.net enden')
            plan['base_url'] = 'https://%s' % name
            plan['steps'] = [
                'tailscale serve --bg --https=443 http://127.0.0.1:%d' % port,
                'tailscale serve status  # zeigt die aktive Zuordnung',
            ]
            plan['notes'].append('Nur Tailnet-Clients erreichen Tailscale Serve. Ein WLAN-Orb ohne Tailscale-Client kann diese URL NICHT erreichen; gleiches WLAN oder ein Subnet-Router in Gegenrichtung helfen nicht. Kein QR-Pairing damit.')
        else:
            plan['base_url'] = 'https://<dein-node>.<dein-tailnet>.ts.net'
        return plan

    # Every remaining mode needs a real domain.
    need(bool(domain), 'Domain oder Hostname fuer den Endpunkt')
    if domain:
        if not isinstance(domain, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', domain):
            raise ValueError('ungueltige Domain')
        plan['base_url'] = validate_public_url('https://' + domain)

    if mode == 'caddy':
        probe = probe_binary('caddy')
        need(probe['available'], 'caddy ist installiert')
        need(False, 'DNS und Ports 80/443 extern pruefen (nicht automatisch geprueft)')
        plan['files']['Caddyfile'] = (
            '%s {\n'
            '    reverse_proxy 127.0.0.1:%d\n'
            '}\n' % (domain or 'bridge.example.com', port)
        )
        plan['steps'] = [
            'Caddyfile nach /etc/caddy/Caddyfile pruefen und einlesen',
            'systemctl reload caddy',
            'Port 80 und 443 im Router auf diesen Host weiterleiten',
        ]
        plan['notes'].append('Caddy besorgt und erneuert das Zertifikat selbststaendig.')
        return plan

    if mode == 'nginx':
        need(probe_binary('nginx')['available'], 'nginx ist installiert')
        need(False, 'TLS-Zertifikat und externe Erreichbarkeit pruefen (nicht automatisch geprueft)')
        host = domain or 'bridge.example.com'
        plan['files']['si-m-bridge.conf'] = (
            'server {\n'
            '    listen 443 ssl http2;\n'
            '    server_name %s;\n'
            '\n'
            '    ssl_certificate     /etc/letsencrypt/live/%s/fullchain.pem;\n'
            '    ssl_certificate_key /etc/letsencrypt/live/%s/privkey.pem;\n'
            '\n'
            '    client_max_body_size 256k;\n'
            '\n'
            '    location / {\n'
            '        proxy_pass http://127.0.0.1:%d;\n'
            '        proxy_http_version 1.1;\n'
            '        proxy_set_header Host $host;\n'
            '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
            '        proxy_set_header X-Forwarded-Proto $scheme;\n'
            '        proxy_buffering off;\n'
            '        proxy_read_timeout 120s;\n'
            '    }\n'
            '}\n' % (host, host, host, port)
        )
        plan['steps'] = [
            'certbot certonly --nginx -d %s' % host,
            'Konfiguration nach /etc/nginx/sites-available/ legen und verlinken',
            'nginx -t && systemctl reload nginx',
        ]
        plan['notes'].append('proxy_buffering off ist wichtig: die Audioantwort wird gestreamt.')
        return plan

    if mode == 'cloudflare':
        need(probe_binary('cloudflared')['available'], 'cloudflared ist installiert')
        need(False, 'Cloudflare-Account, Tunnel und DNS-Route pruefen (nicht automatisch geprueft)')
        host = domain or 'bridge.example.com'
        plan['files']['cloudflared-config.yml'] = (
            'tunnel: si-m-bridge\n'
            'credentials-file: /etc/cloudflared/si-m-bridge.json\n'
            '\n'
            'ingress:\n'
            '  - hostname: %s\n'
            '    service: http://127.0.0.1:%d\n'
            '  - service: http_status:404\n' % (host, port)
        )
        plan['steps'] = [
            'cloudflared tunnel login',
            'cloudflared tunnel create si-m-bridge',
            'cloudflared tunnel route dns si-m-bridge %s' % host,
            'cloudflared tunnel run si-m-bridge  (oder als Dienst einrichten)',
        ]
        plan['notes'].append('Kein Port nach aussen; der Host baut die Verbindung selbst auf.')
        return plan

    if mode == 'existing':
        need(bool(url), 'fertige HTTPS-URL deines Endpunkts')
        if url:
            plan['base_url'] = validate_public_url(url)
        need(False, 'TLS-Vertrauen und Erreichbarkeit vom Orb-Netz pruefen (nicht automatisch geprueft)')
        plan['steps'] = ['Erreichbarkeit testen: curl -sS -o /dev/null -w "%{http_code}" <url>/v1/inbox/meta']
        plan['notes'].append('Der Proxy muss ungepufferte Antworten durchlassen (Audio-Stream).')
        return plan

    return plan


def render_plan(plan):
    """Human-readable summary; safe to paste into a terminal transcript."""
    lines = [
        'Erreichbarkeit: %s' % plan['title'],
        'Geraetetauglich: %s' % ('ja' if plan['device_capable'] else 'NEIN - nur zum Testen'),
        'Basis-URL: %s' % plan['base_url'],
        '',
    ]
    if plan['prerequisites']:
        lines.append('Voraussetzungen:')
        for item in plan['prerequisites']:
            lines.append('  [%s] %s' % ('x' if item['ok'] else ' ', item['check']))
        lines.append('')
    if plan['missing']:
        lines.append('Fehlt noch:')
        for item in plan['missing']:
            lines.append('  - %s' % item)
        lines.append('')
    if plan['steps']:
        lines.append('Schritte:')
        for index, step in enumerate(plan['steps'], 1):
            lines.append('  %d. %s' % (index, step))
        lines.append('')
    for name, content in plan['files'].items():
        lines.append('--- %s ---' % name)
        lines.append(content.rstrip())
        lines.append('')
    for note in plan['notes']:
        lines.append('Hinweis: %s' % note)
    return '\n'.join(lines).rstrip() + '\n'


def write_plan_files(plan, outdir):
    """Write generated proxy configs for review. Refuses to overwrite."""
    written = []
    if not plan['files']:
        return written
    target = Path(outdir)
    target.mkdir(parents=True, exist_ok=True)
    for name, content in plan['files'].items():
        path = target / name
        if path.exists():
            raise FileExistsError('existiert bereits: %s' % path)
        path.write_text(content, encoding='utf-8')
        os.chmod(path, 0o644)
        written.append(path)
    return written


def env_lines(plan, *, profile, home, owner, device):
    """The environment block the operator puts next to the service."""
    base = plan['base_url'] or 'https://<dein-host>'
    return [
        'HERMES_HOME=%s' % home,
        'SIM_CONNECTOR_HERMES_HOME=%s' % home,
        'SIM_HERMES_PROFILE=%s' % profile,
        'SIM_CONNECTOR_DB=%s/sim-inbox.sqlite' % home,
        'SIM_CONNECTOR_OWNER=%s' % owner,
        'SIM_CONNECTOR_PROFILE=%s' % profile,
        'SIM_CONNECTOR_DEVICE=%s' % device,
        'SIM_BRIDGE_BASE_URL=%s' % base,
    ]
