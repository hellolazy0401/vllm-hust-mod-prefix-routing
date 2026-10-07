import importlib.util
from pathlib import Path
import json
import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('campaign',ROOT/'scripts/campaign.py')
campaign=importlib.util.module_from_spec(spec); spec.loader.exec_module(campaign)

def test_preregistered_matrix_and_order():
    plan=campaign.plan(campaign.read(ROOT/'campaign.json'))
    formal=[r for r in plan if r['suite']=='swe']
    assert len(formal)==30
    assert len({r['name'] for r in plan})==70
    assert [r['arm'] for r in plan if r['suite']=='smoke']==['off','on','kill','off']
    assert [r['arm'] for r in formal if r['c']==8 and r['repeat']==2]==['on','off']

def test_recompute_checks_raw_tokens_not_only_summary(tmp_path):
    campaign.write(tmp_path/'config.json',{'duration':900,'chips':4})
    row={'success':True,'token_ids':[1,2],'expected_output_tokens':2,'start':0,'first_token':1,'last_token':2,'end':2.1,'chunks':[[1,1],[2,1]],'ttft_seconds':1,'e2e_seconds':2,'decode_tokens_per_second':1}
    (tmp_path/'requests.jsonl').write_text(json.dumps(row)+'\n')
    summary={'valid':True,'aborted':False,'failed_requests':0,'measurement_seconds':900,'requests_started':1,'requests_completed_in_window':1,'observed_output_tokens_in_window':2,'output_tokens_per_second':2/900,'decode_tokens_per_second_p90':1,'ttft_seconds_p95':1}
    campaign.write(tmp_path/'summary.json',summary)
    metrics,_,_=campaign.recompute(tmp_path)
    assert metrics['output_tps_per_chip']==2/900/4
    summary['observed_output_tokens_in_window']=3
    campaign.write(tmp_path/'summary.json',summary)
    with pytest.raises(ValueError,match='Raw window'): campaign.recompute(tmp_path)

def test_missing_native_counter_stays_unknown(tmp_path):
    campaign.write(tmp_path/'runtime-evidence.json',{'process_counters':[],'utility_events':[]})
    assert all(v is None for v in campaign.mechanism(tmp_path)['native_deltas'].values())

def test_empty_campaign_exports_no_fabricated_points(tmp_path):
    from types import SimpleNamespace
    cfg=campaign.read(ROOT/'campaign.json')
    campaign.write(tmp_path/'evidences/preregistration.json',cfg)
    campaign.analyze(SimpleNamespace(output=tmp_path))
    assert campaign.read(tmp_path/'analysis/points.add.json')==[]
    assert len(campaign.read(tmp_path/'analysis/excluded.json'))==70
    assert campaign.read(tmp_path/'analysis/gates.json')['formal_complete'] is False

def test_full_formal_matrix_retains_all_repeats(tmp_path):
    from types import SimpleNamespace
    cfg=campaign.read(ROOT/'campaign.json'); ev=tmp_path/'evidences'
    campaign.write(ev/'preregistration.json',cfg)
    campaign.write(ev/'identity.json',{'workload_sha256':'test-only','versions':{'vllm':'fixture'}})
    campaign.write(ev/'profile.json',{'test_fixture':True})
    for item in campaign.plan(cfg):
        if item['suite']!='swe': continue
        run=tmp_path/'results'/item['name']; folder=run/'swe-formal/measurement'; folder.mkdir(parents=True)
        campaign.write(run/'complete.json',{'owner':{'preregistration_sha256':campaign.sha(ev/'preregistration.json'),'identity_sha256':campaign.sha(ev/'identity.json')}})
        campaign.write(run/'server-metadata.json',{'arm':item['arm'],'chips':4,'profile':{'test_fixture':True},'servers':[{'environment':{'VLLM_HUST_UTILITY_VICTIM_ENABLE':'0','VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH':'1','VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH':'0' if item['arm']=='on' else '1'}}]})
        campaign.write(run/'runtime-evidence.json',{'process_counters':[],'utility_events':[]})
        campaign.write(folder/'config.json',{'duration':900,'chips':4,'concurrency':item['c'],'run_id':item['name'],'tokenizer':{'fingerprint':'fixture'},'workload_sha256':'test-only'})
        row={'success':True,'token_ids':[1,2],'expected_output_tokens':2,'start':0,'first_token':1,'last_token':2,'end':2.1,'chunks':[[1,1],[2,1]],'ttft_seconds':1,'e2e_seconds':2,'decode_tokens_per_second':1}
        (folder/'requests.jsonl').write_text(json.dumps(row)+'\n')
        campaign.write(folder/'summary.json',{'valid':True,'aborted':False,'failed_requests':0,'measurement_seconds':900,'requests_started':1,'requests_completed_in_window':1,'observed_output_tokens_in_window':2,'output_tokens_per_second':2/900,'decode_tokens_per_second_p90':1,'ttft_seconds_p95':1})
    campaign.analyze(SimpleNamespace(output=tmp_path))
    assert len(campaign.read(tmp_path/'analysis/points.add.json'))==30
    assert len(campaign.read(tmp_path/'analysis/display_series_ids.add.json'))==6
    assert len(campaign.read(tmp_path/'analysis/paired-comparison.json'))==5
    assert (tmp_path/'analysis/throughput.svg').exists()

def test_port_check_rejects_real_listener():
    import socket
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        s.bind(('127.0.0.1',0)); s.listen(1)
        with pytest.raises(RuntimeError,match='not available'):
            campaign.free_ports([s.getsockname()[1]])

def test_linux_time_wait_does_not_block_reusable_listener():
    import socket
    import sys
    if sys.platform!='linux': pytest.skip('Linux TIME_WAIT semantics')
    server=socket.socket(); server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    server.bind(('127.0.0.1',0)); port=server.getsockname()[1]; server.listen(1)
    client=socket.create_connection(('127.0.0.1',port)); peer,_=server.accept()
    peer.close(); assert client.recv(1)==b''; client.close(); server.close()
    with socket.socket() as plain:
        with pytest.raises(OSError): plain.bind(('127.0.0.1',port))
    campaign.free_ports([port])

def test_recovery_refuses_failed_or_forced_run(tmp_path):
    with pytest.raises(RuntimeError,match='Run failed'):
        campaign.finish_run({'suite':'smoke'},tmp_path,{'client_exit_code':1,'server_exit_code':0})
    campaign.write(tmp_path/'server-metadata.json',{'stopped_at_unix':1,'forced_kill_observed':True})
    with pytest.raises(RuntimeError,match='Clean server shutdown'):
        campaign.finish_run({'suite':'smoke'},tmp_path,{'client_exit_code':0,'server_exit_code':0})
