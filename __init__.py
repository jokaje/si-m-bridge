"""Hermes plugin entry; explicit notification tool, no global hooks."""
import json
import os
from pathlib import Path
from .connector import Store, canonical_store

SCHEMA = {'name':'sim_notify','description':'Explicitly notify a paired SI-M Orb only when an Orb-owned task is finished or an explicit question requires its owner. Never mirror arbitrary final replies. Provide stable event_id for retries.', 'parameters':{'type':'object','additionalProperties':False,'required':['owner','profile','event_id','kind','title','body'],'properties':{k:{'type':'string'} for k in ('owner','profile','event_id','kind','title','body')}}}

def _scope():
    """Resolve either embedded mode or an explicitly bound external gateway store."""
    from .connector import valid
    actual = Path(os.environ['HERMES_HOME']).resolve(strict=True)
    binding = os.environ.get('SIM_BRIDGE_BINDING_FILE')
    if binding:
        path = Path(binding)
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
            raise ValueError('binding file must be private and owned by gateway user')
        config = json.loads(path.read_text())
        if set(config) != {'mode','gateway_home','bridge_home','db','owner','profile'} or config['mode'] != 'gateway':
            raise ValueError('invalid binding contract')
        if Path(config['gateway_home']).resolve(strict=True) != actual:
            raise ValueError('gateway profile mismatch')
        home = Path(config['bridge_home']).resolve(strict=True)
        if home == actual or config['db'] != str(home / 'sim-inbox.sqlite'):
            raise ValueError('bridge store mismatch')
        db = canonical_store(config['db'], home=home)
        owner, profile = config['owner'], config['profile']
    else:
        home = Path(os.environ['SIM_CONNECTOR_HERMES_HOME']).resolve(strict=True)
        if home != actual: raise ValueError('profile home mismatch')
        db = canonical_store(os.environ['SIM_CONNECTOR_DB'])
        owner, profile = os.environ['SIM_CONNECTOR_OWNER'], os.environ['SIM_CONNECTOR_PROFILE']
    valid(owner); valid(profile)
    return owner, profile, db

def configured_scope():
    try: _scope(); return True
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError): return False

def notify(args, **kwargs):
    try:
        owner, profile, db = _scope()
        if set(args)!=set(SCHEMA['parameters']['required']) or args['owner']!=owner or args['profile']!=profile: raise ValueError('scope mismatch')
        store=Store(db)
        try: ident=store.put(owner,profile,args['event_id'],args['kind'],args['title'],args['body'])
        finally: store.db.close()
        return json.dumps({'success':True,'id':ident})
    except (ValueError,KeyError,TypeError, OSError, LookupError, json.JSONDecodeError):
        return json.dumps({'success':False,'error':'notification unavailable'})

def register(ctx):
    ctx.register_tool(name='sim_notify',toolset='sim_bridge',schema=SCHEMA,handler=notify,check_fn=configured_scope)
