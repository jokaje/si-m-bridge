"""Hermes plugin entry; explicit notification tool, no global hooks."""
import json
import os
from pathlib import Path
from .connector import Store, canonical_store

SCHEMA = {'name':'sim_notify','description':'Explicitly notify a paired SI-M Orb only when an Orb-owned task is finished or an explicit question requires its owner. Never mirror arbitrary final replies. Provide stable event_id for retries.', 'parameters':{'type':'object','additionalProperties':False,'required':['owner','profile','event_id','kind','title','body'],'properties':{k:{'type':'string'} for k in ('owner','profile','event_id','kind','title','body')}}}

def configured_scope():
    owner=os.environ.get('SIM_CONNECTOR_OWNER'); profile=os.environ.get('SIM_CONNECTOR_PROFILE')
    home=os.environ.get('SIM_CONNECTOR_HERMES_HOME'); actual=os.environ.get('HERMES_HOME')
    if not all((owner,profile,home,actual,os.environ.get('SIM_CONNECTOR_DB'))): return False
    try:
        from .connector import valid
        valid(owner); valid(profile)
        return (Path(home).resolve(strict=True)==Path(actual).resolve(strict=True)
                and canonical_store(os.environ['SIM_CONNECTOR_DB']) == canonical_store(Path(home)/'sim-inbox.sqlite'))
    except (ValueError,OSError): return False

def notify(args, **kwargs):
    try:
        if not configured_scope(): raise ValueError('unconfigured scope')
        owner=os.environ['SIM_CONNECTOR_OWNER']; profile=os.environ['SIM_CONNECTOR_PROFILE']
        if set(args)!=set(SCHEMA['parameters']['required']) or args['owner']!=owner or args['profile']!=profile: raise ValueError('scope mismatch')
        store=Store(canonical_store(os.environ['SIM_CONNECTOR_DB']))
        try: ident=store.put(owner,profile,args['event_id'],args['kind'],args['title'],args['body'])
        finally: store.db.close()
        return json.dumps({'success':True,'id':ident})
    except (ValueError,KeyError,TypeError, OSError, LookupError):
        return json.dumps({'success':False,'error':'notification unavailable'})

def register(ctx):
    ctx.register_tool(name='sim_notify',toolset='sim_bridge',schema=SCHEMA,handler=notify,check_fn=configured_scope)
