"""Fixed-profile, bounded Hermes worker processes."""
import asyncio
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
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
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
    try:
        result = agent.run_conversation(text)
        if result.get('error') or result.get('partial'):
            raise ValueError('Hermes turn failed')
        reply = result.get('final_response')
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError('empty Hermes reply')
        return reply[:4096]
    finally:
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

    async def __call__(self, text, session):
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


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    try:
        profile_home(os.environ['SIM_HERMES_PROFILE'], os.environ['HERMES_HOME'])
        text, session = json.loads(sys.stdin.buffer.read(16384))
        from connector import valid
        valid(session)
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ValueError('invalid text')
        print(json.dumps(run_worker(text, session)), flush=True)
    except Exception:
        raise SystemExit(1)
