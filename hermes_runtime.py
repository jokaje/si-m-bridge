"""Fixed-profile, bounded Hermes worker processes."""
import asyncio
import contextlib
import sqlite3
import json
import inspect
import os
from pathlib import Path
import signal
import sys



def profile_home(profile, home):
    from connector import valid
    from hermes_constants import get_hermes_home
    valid(profile)
    expected = Path(home).resolve(strict=True)
    if expected != get_hermes_home().resolve(strict=True) or not (expected / 'config.yaml').is_file():
        raise ValueError('dedicated Hermes home/config required')
    if not (expected / 'plugins' / 'si-m-bridge' / 'plugin.yaml').is_file():
        raise ValueError('connector plugin missing')
    from hermes_cli.plugins_discovery import _get_enabled_plugins, _get_disabled_plugins
    if 'si-m-bridge' not in (_get_enabled_plugins() or set()) or 'si-m-bridge' in _get_disabled_plugins():
        raise ValueError('connector plugin not enabled')
    return expected


def check_compatibility():
    """Fail closed when installed Hermes private integration contracts drift."""
    try:
        from hermes_cli.oneshot import _resolve_model_and_provider
        from hermes_cli.runtime_provider import resolve_runtime_with_fallback
        from hermes_cli.tools_config import _get_platform_tools
        from hermes_cli.plugins_discovery import _get_enabled_plugins, _get_disabled_plugins
        from run_agent import AIAgent
    except ImportError as exc:
        # By far the most common first-run failure: the bridge was installed into
        # its own virtualenv, where Hermes simply does not exist.
        raise RuntimeError(
            'Hermes ist in dieser Python-Umgebung nicht installiert. Die Bridge muss '
            'mit dem Python laufen, in dem Hermes installiert ist (z.B. '
            '"sudo -u <dienst> <hermes-venv>/bin/pip install -e ." oder Aufruf mit '
            'diesem Interpreter). Siehe README, Abschnitt "Schnellstart".'
        ) from exc
    try:
        contracts = (
            (_resolve_model_and_provider, ('cfg', 'model', 'provider')),
            (resolve_runtime_with_fallback, ('config', 'requested', 'target_model', 'explicit_base_url', 'explicit_api_key')),
            (_get_platform_tools, ('config', 'platform')),
            (_get_enabled_plugins, ()), (_get_disabled_plugins, ()),
            (AIAgent, ('session_id', 'enabled_toolsets', 'credential_pool', 'requested_provider', 'skip_background_review')),
        )
        for fn, names in contracts:
            params = inspect.signature(fn).parameters
            if any(name not in params for name in names):
                raise TypeError(f'{fn.__module__}.{fn.__name__} contract changed')
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError('unsupported Hermes connector integration API; review installed Hermes before use') from exc


def run_worker(text, session):
    from hermes_cli.config import load_env, load_config
    from hermes_cli.oneshot import _resolve_model_and_provider
    from hermes_cli.runtime_provider import resolve_runtime_with_fallback
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_constants import resolve_reasoning_config
    from hermes_cli.fallback_config import get_fallback_chain
    from run_agent import AIAgent
    load_env()
    cfg = load_config()
    choice = _resolve_model_and_provider(cfg, None, None)
    if not choice.model:
        raise ValueError('profile model missing')
    runtime, fallback = resolve_runtime_with_fallback(cfg, requested=choice.provider, target_model=choice.model,
                                                        explicit_base_url=choice.base_url, explicit_api_key=choice.api_key)
    model = fallback['model'] if fallback else choice.model
    agent = AIAgent(api_key=runtime.get('api_key'), base_url=runtime.get('base_url'),
                    provider=runtime.get('provider'), requested_provider=runtime.get('requested_provider'),
                    api_mode=runtime.get('api_mode'), model=model,
                    enabled_toolsets=sorted(_get_platform_tools(cfg, 'cli')),
                    session_id=session, platform='cli', quiet_mode=True,
                    credential_pool=runtime.get('credential_pool'), fallback_model=get_fallback_chain(cfg) or None,
                    request_overrides=runtime.get('request_overrides'),
                    reasoning_config=resolve_reasoning_config(cfg, model),
                    max_iterations=int((cfg.get('agent') or {}).get('max_turns') or 90),
                    skip_background_review=True)
    # The worker is recreated for every turn: session_id alone does not load chat
    # context into run_conversation. Keep a bounded, private transcript explicitly.
    from connector import canonical_store
    db = sqlite3.connect(canonical_store(os.environ['SIM_CONNECTOR_DB']), timeout=5)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS turn_history (session_id TEXT PRIMARY KEY, history TEXT NOT NULL)')
        row = db.execute('SELECT history FROM turn_history WHERE session_id=?', (session,)).fetchone()
        history = json.loads(row[0]) if row else []
        # Hermes may print progress to stdout; reserve stdout for the worker JSON protocol.
        with contextlib.redirect_stdout(sys.stderr):
            result = agent.run_conversation(text, conversation_history=history)
        if result.get('error') or result.get('partial'):
            raise ValueError('Hermes turn failed')
        reply = result.get('final_response')
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError('empty Hermes reply')
        # Hermes returns the actual transcript (including tool calls/results). Its
        # input is copied internally today, but never persist our guessed two-row
        # suffix: that loses tool context and may duplicate rows on API changes.
        messages = result.get('messages')
        if not isinstance(messages, list) or not any(
                isinstance(m, dict) and m.get('role') == 'user' and m.get('content') == text
                for m in messages):
            raise ValueError('Hermes turn transcript missing')
        # Keep complete user-led turns; a raw [-16:] can strand tool replies
        # without their initiating assistant tool_calls.
        turns = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError('invalid Hermes transcript')
            if message.get('role') == 'user':
                turns.append([])
            if turns:
                turns[-1].append(message)
        history = [m for turn in turns[-8:] for m in turn]
        db.execute('INSERT INTO turn_history(session_id,history) VALUES(?,?) '
                   'ON CONFLICT(session_id) DO UPDATE SET history=excluded.history',
                   (session, json.dumps(history)))
        db.commit()
        return reply[:4096]
    finally:
        db.close()
        agent.close()


class HermesRuntime:
    def __init__(self, profile, home, *, deadline=90):
        check_compatibility()
        self.profile = profile
        self.home = profile_home(profile, home)
        if not 0 < deadline <= 90:
            raise ValueError('invalid deadline')
        self.deadline = deadline
        self.slots = asyncio.Semaphore(2)
        self.session_locks = {}

    async def __call__(self, text, session):
        from connector import valid
        valid(session)
        lock = self.session_locks.setdefault(session, asyncio.Lock())
        async with lock:
            return await self._run(text, session)

    async def _run(self, text, session):
        from hermes_constants import get_hermes_home
        if get_hermes_home().resolve(strict=True) != self.home:
            raise ValueError('Hermes home changed')
        if self.slots._value <= 0:
            raise ValueError('Hermes busy')
        await self.slots.acquire()
        child = None
        try:
            child = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), '--worker',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=dict(os.environ, HERMES_HOME=str(self.home), SIM_HERMES_PROFILE=self.profile),
                start_new_session=True)
            try:
                out, _ = await asyncio.wait_for(child.communicate(json.dumps([text, session]).encode()), self.deadline)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Cancellation must not release the slot before the child is reaped.
                reap = asyncio.create_task(child.wait())
                try:
                    await asyncio.shield(reap)
                except asyncio.CancelledError:
                    await asyncio.shield(reap)
                raise
            if child.returncode or len(out) > 8192:
                raise ValueError('Hermes turn failed')
            reply = json.loads(out)
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError('empty Hermes reply')
            return reply
        finally:
            self.slots.release()

    def close(self):
        pass


class GatewayRuntime:
    """Existing default agent via its authenticated gateway; bridge state stays separate.

    No Hermes home override, profile clone, plugin install or gateway restart.
    The gateway owns its own conversation persistence and profile routing.
    """
    def __init__(self, profile, gateway_url, key_file, *, deadline=90):
        from connector import safe_url, valid
        valid(profile)
        if profile != 'default':
            raise ValueError('gateway mode currently requires explicit default profile')
        self.profile = profile
        self.url = safe_url(gateway_url)
        if self.url not in ('http://127.0.0.1:8765', 'http://localhost:8765'):
            raise ValueError('gateway must be the local default-agent API')
        path = Path(key_file).resolve(strict=True)
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError('gateway key file must be private (0600)')
        self.key_file = path
        if not 0 < deadline <= 90:
            raise ValueError('invalid deadline')
        self.deadline = deadline
        self.slots = asyncio.Semaphore(2)
        self.session_locks = {}

    async def __call__(self, text, session):
        from connector import valid
        import httpx
        valid(session)
        lock = self.session_locks.setdefault(session, asyncio.Lock())
        async with lock:
            if self.slots._value <= 0:
                raise ValueError('Hermes busy')
            await self.slots.acquire()
            try:
                key = self.key_file.read_text().strip()
                if not key or '\n' in key or '\r' in key:
                    raise ValueError('invalid gateway key file')
                # API server's declared conversation key provides continuity without
                # borrowing a Telegram session. Never send a client-chosen profile.
                headers = {'Authorization': 'Bearer ' + key,
                           'X-Hermes-Session-Key': 'si-m-orb-' + session}
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.deadline, connect=3),
                                             follow_redirects=False, trust_env=False) as client:
                    response = await client.post(self.url + '/v1/chat/completions',
                        headers=headers, json={'model': 'hermes',
                            'messages': [{'role': 'system', 'content': 'This is a spoken response on a device with a 40-second audio limit. Answer the user fully but concisely in at most two short sentences, preferably under 180 characters. Do not omit a required warning or ask for approval implicitly; if a complete safe answer cannot fit, say what remains unanswered.'},
                                         {'role': 'user', 'content': text}], 'stream': False})
                    response.raise_for_status()
                    if len(response.content) > 65536:
                        raise ValueError('oversized gateway response')
                    reply = response.json()['choices'][0]['message']['content']
                    if not isinstance(reply, str) or not reply.strip():
                        raise ValueError('empty gateway reply')
                    return reply[:4096]
            finally:
                self.slots.release()


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    try:
        profile_home(os.environ['SIM_HERMES_PROFILE'], os.environ['HERMES_HOME'])
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384: raise ValueError('oversized worker input')
        text, session = json.loads(raw)
        from connector import valid
        valid(session)
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ValueError('invalid text')
        with contextlib.redirect_stdout(sys.stderr):
            reply = run_worker(text, session)
        print(json.dumps(reply), flush=True)
    except Exception:
        raise SystemExit(1)
