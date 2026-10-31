"""Operator-only CLI. Run from project root; never paste QR into shared logs."""
import argparse
import json
import os
import sys


def _ask(prompt, default=None):
    suffix = ' [%s]' % default if default else ''
    try:
        answer = input('%s%s: ' % (prompt, suffix)).strip()
    except EOFError:
        answer = ''
    return answer or (default or '')


def run_setup(args):
    """Interactive or scripted setup. Writes nothing but the requested configs."""
    from setup_wizard import MODES, plan_exposure, render_plan, write_plan_files, env_lines

    home = args.hermes_home or os.environ.get('SIM_CONNECTOR_HERMES_HOME') or os.environ.get('HERMES_HOME')
    profile = args.profile or os.environ.get('SIM_HERMES_PROFILE')
    owner = args.owner
    device = args.device

    mode = args.exposure
    if not mode:
        print('Wo soll der Orb den Verbindungsknoten erreichen?\n')
        order = ['tailscale', 'caddy', 'nginx', 'cloudflare', 'existing', 'loopback']
        for index, key in enumerate(order, 1):
            entry = MODES[key]
            print('  %d) %s' % (index, entry['title']))
            print('     %s' % entry['summary'])
        print()
        choice = _ask('Auswahl (1-%d)' % len(order), '1')
        try:
            mode = order[int(choice) - 1]
        except (ValueError, IndexError):
            raise SystemExit('ungueltige Auswahl')

    domain = args.domain
    url = args.url
    if mode in ('caddy', 'nginx', 'cloudflare') and not domain:
        if args.exposure:
            raise SystemExit('--domain ist fuer diesen Modus erforderlich')
        domain = _ask('Domain (z.B. bridge.example.com)')
    if mode == 'existing' and not url:
        if args.exposure:
            raise SystemExit('--url ist fuer diesen Modus erforderlich')
        url = _ask('Bereits erreichbare HTTPS-URL')

    plan = plan_exposure(mode, domain=domain, port=args.port, url=url, tailnet_name=args.tailnet)
    print()
    print(render_plan(plan))

    if not plan['device_capable']:
        print('WARNUNG: Dieser Endpunkt ist fuer den Orb nicht erreichbar.')
    if plan['missing']:
        print('Der Plan ist unvollstaendig. Erst die fehlenden Voraussetzungen klaeren.')

    if plan['files'] and not args.no_write:
        target = args.out_dir
        try:
            written = write_plan_files(plan, target)
        except FileExistsError as exc:
            raise SystemExit(str(exc))
        print('\nGeschrieben (bitte vor dem Aktivieren pruefen): %s' % ', '.join(str(p) for p in written))
    elif plan['files']:
        print('\nKeine Dateien geschrieben (--no-write).')

    if home and profile:
        print('\nUmgebung fuer den Dienst:')
        for line in env_lines(plan, profile=profile, home=home,
                              owner=owner or 'owner-a', device=device or 'orb-a'):
            print('  %s' % line)
    else:
        print('\nHinweis: --hermes-home und --profile setzen, um den passenden Umgebungsblock zu erzeugen.')

    if plan['base_url'] and plan['device_capable'] and plan['base_url'].startswith('https://') and 'example.com' not in plan['base_url']:
        print('\nNaechster Schritt nach dem Proxy:')
        print('  python cli.py --db "$SIM_CONNECTOR_DB" pair --owner %s --profile %s --device %s --base-url %s'
              % (owner or 'owner-a', profile or '<profil>', device or 'orb-a', plan['base_url']))
    return 0


def main():
    p = argparse.ArgumentParser(prog='si-m-bridge')
    p.add_argument('--db', required=False)
    sub = p.add_subparsers(dest='cmd', required=True)

    setup = sub.add_parser('setup', help='Erreichbarkeit planen und Einrichtung anzeigen')
    setup.add_argument('--exposure', choices=['tailscale', 'caddy', 'nginx', 'cloudflare', 'existing', 'loopback'])
    setup.add_argument('--domain')
    setup.add_argument('--url')
    setup.add_argument('--tailnet', help='Tailnet-DNS-Name, falls die automatische Ermittlung nicht greift')
    setup.add_argument('--port', type=int, default=8766)
    setup.add_argument('--hermes-home')
    setup.add_argument('--profile')
    setup.add_argument('--owner')
    setup.add_argument('--device')
    setup.add_argument('--out-dir', default='./generated')
    setup.add_argument('--no-write', action='store_true')

    pair = sub.add_parser('pair')
    for k in ('owner', 'profile', 'device', 'base-url'):
        pair.add_argument('--' + k, required=True)
    revoke = sub.add_parser('revoke')
    for k in ('owner', 'profile', 'device'):
        revoke.add_argument('--' + k, required=True)
    sub.add_parser('doctor')
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8766)
    args = p.parse_args()

    if args.cmd == 'setup':
        return run_setup(args)

    if not args.db:
        p.error('--db ist erforderlich')

    from connector import Store, create_app, safe_url, canonical_store
    from audio_bridge import CommandAdapter, LocalSpeechHTTP, WhisperLocal, pcm_from_wav
    try:
        db = canonical_store(args.db)
        if canonical_store(os.environ['SIM_CONNECTOR_DB']) != db:
            raise ValueError('CLI and plugin inbox store mismatch')
    except (KeyError, OSError, ValueError) as exc:
        p.error('inbox store not configured: ' + str(exc))
    if args.cmd in ('pair', 'serve', 'doctor'):
        from hermes_runtime import HermesRuntime
        try:
            runtime = HermesRuntime(os.environ['SIM_HERMES_PROFILE'], os.environ['SIM_CONNECTOR_HERMES_HOME'])
        except (KeyError, ValueError, ImportError, OSError) as exc:
            p.error('dedicated Hermes runtime not ready: ' + str(exc))
    if args.cmd == 'doctor':
        print('Dedicated Hermes home, enabled plugin, and provider route configured (provider reachability not tested).')
        return
    store = Store(db)
    if args.cmd == 'pair':
        base = safe_url(args.base_url)
        from urllib.parse import urlencode
        print('sim://pair?' + urlencode({'v': 1, 'url': base, 'code': store.pair(args.owner, args.profile, args.device),
                                         'profile': args.profile, 'device': args.device}))
    elif args.cmd == 'revoke':
        print('revoked' if store.revoke(args.owner, args.profile, args.device) else 'not found')
    else:
        import uvicorn
        url = os.environ.get('SIM_HERMES_URL')
        if url:
            safe_url(url)
        if not args.host.startswith('127.') and args.host != '::1':
            p.error('only loopback bind allowed; use reviewed HTTPS reverse proxy')
        stt = None
        if os.environ.get('SIM_STT_MODEL_PATH'):
            stt = WhisperLocal(os.environ['SIM_STT_MODEL_PATH'])
        elif os.environ.get('SIM_STT_COMMAND'):
            stt = CommandAdapter(json.loads(os.environ['SIM_STT_COMMAND']))
        tts = None
        if os.environ.get('SIM_TTS_URL'):
            tts = LocalSpeechHTTP(os.environ['SIM_TTS_URL'])
        elif os.environ.get('SIM_TTS_COMMAND'):
            command = CommandAdapter(json.loads(os.environ['SIM_TTS_COMMAND']))
            tts = lambda text: pcm_from_wav(command(text.encode('utf-8')))
        uvicorn.run(create_app(store, transcribe=stt, hermes_profile=runtime.profile,
                               synthesize=tts, turn_runner=runtime), host=args.host, port=args.port)


if __name__ == '__main__':
    sys.exit(main())
