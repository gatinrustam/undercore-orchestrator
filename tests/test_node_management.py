import json
import pytest
from fastapi.testclient import TestClient
from test_node_switch import pair
from orchestrator.registry import NodeRegistry
from orchestrator.settings import NodeSettings
from orchestrator.agent_http import create_agent_app
from orchestrator.models import PilotError


def managed(gateway,tmp_path):
    nodes=[]
    for node in gateway.nodes.values():
        key=tmp_path/(node.id+'.token');key.write_text(node.api_key);key.chmod(0o600)
        nodes.append(NodeSettings(id=node.id,api_url=node.api_url,server_id=node.server_id,region=node.region,
                    capacity=node.capacity,mode=node.mode,api_key_file=str(key)))
    gateway.registry=NodeRegistry(gateway.store,nodes)
    return gateway.registry


def payload(registry,gateway,node='a'):
    value=next(r for r in registry.snapshot(gateway)['nodes'] if r['id']==node)
    return {k:v for k,v in value.items() if k in ('id','api_url','server_id','region','capacity','mode','protocol','lease_enabled')}|{'expected_revision':value['revision']}


def test_admin_bearer_is_distinct_and_secret_free(pair,tmp_path):
    gateway,*_=pair;registry=managed(gateway,tmp_path)
    client=TestClient(create_agent_app(gateway,'b'*40,admin_token='m'*40))
    path='/internal/admin/nodes'
    assert client.get(path).status_code==401
    assert client.get(path,headers={'Authorization':'Bearer '+'b'*40}).status_code==401
    response=client.get(path,headers={'Authorization':'Bearer '+'m'*40})
    assert response.status_code==200 and 'no-store' in response.headers['cache-control']
    assert 'api_key' not in response.text and '.token' not in response.text and 'n'*40 not in response.text
    assert client.get('/v1/clients',headers={'Authorization':'Bearer '+'m'*40}).status_code==401


def test_drain_is_durable_and_conflicting_edits_are_rejected(pair,tmp_path):
    gateway,*_=pair;registry=managed(gateway,tmp_path)
    data=payload(registry,gateway);data['mode']='draining'
    registry.save(gateway,data)
    assert gateway.nodes['a'].mode=='draining'
    assert NodeRegistry(gateway.store,[]).nodes()['a'].mode=='draining'
    with pytest.raises(PilotError,match='revision_conflict'):registry.save(gateway,data)
    bad=payload(registry,gateway);bad['server_id']='wrong'
    with pytest.raises(PilotError,match='assigned_node_identity_changed'):registry.save(gateway,bad)


def test_disabled_node_can_be_edited_while_offline_but_new_address_requires_identity(pair,tmp_path):
    gateway,_,states,*_=pair;registry=managed(gateway,tmp_path)
    states['a']['down']=True
    data=payload(registry,gateway);data['mode']='disabled'
    registry.save(gateway,data)
    data=payload(registry,gateway);data['api_key']='bad-key-'*8
    with pytest.raises(PilotError):registry.save(gateway,data)
    assert payload(registry,gateway)['expected_revision']==2
    assert not (gateway.store.path.parent/'node-secrets').exists()


def test_lease_is_irreversible_and_invalid_address_cannot_replace_registry(pair,tmp_path):
    gateway,*_=pair;registry=managed(gateway,tmp_path)
    data=payload(registry,gateway);data['lease_enabled']=True
    registry.save(gateway,data)
    data=payload(registry,gateway);data['lease_enabled']=False
    with pytest.raises(PilotError,match='lease_cannot_be_disabled'):registry.save(gateway,data)
    data=payload(registry,gateway);data['api_url']='http://node.invalid'
    with pytest.raises(PilotError,match='invalid_request'):registry.save(gateway,data)
    assert payload(registry,gateway)['expected_revision']==2


def test_rotated_key_is_private_durable_and_never_returned(pair,tmp_path):
    gateway,*_=pair;registry=managed(gateway,tmp_path)
    data=payload(registry,gateway);data['api_key']=gateway.nodes['a'].api_key
    registry.save(gateway,data)
    files=list((gateway.store.path.parent/'node-secrets').iterdir())
    assert len(files)==1 and files[0].stat().st_mode & 0o777 == 0o600
    assert NodeRegistry(gateway.store,[]).nodes()['a'].api_key==data['api_key']
    response=json.dumps(registry.snapshot(gateway))
    assert data['api_key'] not in response and str(files[0]) not in response
