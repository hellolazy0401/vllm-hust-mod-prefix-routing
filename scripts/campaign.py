"""Reproducible PR173 campaign driver. Does not edit vLLM or publish results."""
import argparse
import csv
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
from pathlib import Path
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def write(p, obj):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
def sha(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()
def command(cmd, out):
    r = subprocess.run(cmd, capture_output=True, text=True, errors='replace')
    Path(out).write_text(r.stdout+r.stderr, encoding='utf-8', newline='\n')
    return r.returncode
def quantile(xs, q):
    xs = sorted(xs)
    if not xs: return None
    x = (len(xs)-1)*q; lo=int(x); hi=math.ceil(x)
    return xs[lo]+(xs[hi]-xs[lo])*(x-lo)
def plan(cfg):
    result=[]
    for suite, cs in [('swe', cfg['concurrencies']), ('positive', cfg['control_concurrencies']), ('negative', cfg['control_concurrencies'])]:
        for r in (1,2,3):
            for c in (cs if r != 2 else list(reversed(cs))):
                for arm in (['off','on'] if r != 2 else ['on','off']):
                    result.append(dict(suite=suite, arm=arm, c=c, repeat=r, name=f'{suite}-{arm}-c{c}-r{r}'))
    for arm, r in [('off',0),('on',0),('kill',0),('off',9)]:
        result.insert(0 if arm=='off' and r==0 else len(result), dict(suite='smoke',arm=arm,c=1,repeat=r,name=f'smoke-{arm}-c1-r{r}'))
    return result
def free_ports(ports=(18180,18181,18182)):
    for port in ports:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if os.name=='nt' else socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
                s.listen(1)
            except OSError as exc:
                raise RuntimeError(f'Port {port} is not available for a new listener: {exc}; inspect owned processes, do not rerun over existing results') from exc

def capture(a):
    out=a.output.resolve(); out.mkdir(parents=True,exist_ok=False)
    cfg=read(a.config); write(out/'preregistration.json',cfg)
    write(out/'profile.json',read(cfg['profile']))
    receipt={'created_unix':time.time(),'python':sys.version,'versions':{},'paths':{},'model_files':{}}
    for package in ('vllm','vllm-ascend','torch','torch-npu','vllm-hust-ext','vllm-hust-prefix-routing'):
        receipt['versions'][package]=importlib.metadata.version(package)
    if receipt['versions']['vllm-hust-prefix-routing']!='0.1.0.dev4': raise RuntimeError('Expected dev4')
    for label in ('core','ascend','bench'):
        path=Path(cfg[label]); receipt['paths'][label]=str(path)
        command(['git','-C',str(path),'rev-parse','HEAD'],out/f'{label}-commit.txt')
        command(['git','-C',str(path),'status','--porcelain'],out/f'{label}-status.txt')
        command(['git','-C',str(path),'diff','HEAD','--'],out/f'{label}-tracked.diff')
        files={p.relative_to(path).as_posix():sha(p) for p in path.rglob('*.py') if '.git' not in p.parts and not any(x.startswith('.venv') for x in p.parts)}
        write(out/f'{label}-python-sha256.json',files)
    model=Path(cfg['model'])
    for p in sorted(model.rglob('*')):
        if p.is_file() and p.suffix in ('.json','.safetensors','.bin','.model','.tiktoken','.jinja'):
            receipt['model_files'][p.relative_to(model).as_posix()]={'bytes':p.stat().st_size,'sha256':sha(p)}
    if not any(n.endswith(('.safetensors','.bin')) for n in receipt['model_files']): raise RuntimeError('No model weights hashed')
    receipt['workload_sha256']=sha(cfg['workload'])
    receipt['mod_wheel_sha256']=sha(ROOT/'wheels/vllm_hust_prefix_routing-0.1.0.dev4-py3-none-any.whl')
    receipt['harness_sha256']={name:sha(ROOT/name) for name in (
        'scripts/campaign.py','scripts/serve_prefix_ab.py','scripts/run_prefix_client.py',
        'benchmarks/prefix_routing_random_proxy.py')}
    dist=importlib.metadata.distribution('vllm-hust-prefix-routing')
    receipt['installed_mod_files']={str(f):sha(dist.locate_file(f)) for f in dist.files or [] if str(f).startswith('vllm_hust_prefix_routing/') and not str(f).endswith('.pyc') and dist.locate_file(f).is_file()}
    command(['npu-smi','info'],out/'hardware.txt')
    command([sys.executable,'-m','pip','freeze'],out/'pip-freeze.txt')
    command(['vllm-hust-ext','extension','list'],out/'extensions.txt')
    write(out/'environment.json',{k:v for k,v in os.environ.items() if k.startswith(('VLLM_HUST_PREFIX_ROUTING','VLLM_HUST_UTILITY_VICTIM','ASCEND_RT_VISIBLE_DEVICES')) and 'CONFIG' not in k and 'TOKEN' not in k})
    write(out/'identity.json',receipt)
    write(out/'plan.json',plan(cfg))
    print('Evidence captured; model hashes identify actual files, not an inferred upstream revision:',out)

def run_one(cfg, item, evidence, results):
    identity=read(evidence/'identity.json')
    for name, expected in identity.get('harness_sha256',{}).items():
        if sha(ROOT/name)!=expected: raise RuntimeError('Frozen harness changed: '+name)
    if sha(cfg['workload'])!=identity['workload_sha256']: raise RuntimeError('Prepared workload changed')
    if read(cfg['profile'])!=read(evidence/'profile.json'): raise RuntimeError('Profile changed')
    if importlib.metadata.version('vllm-hust-prefix-routing')!='0.1.0.dev4': raise RuntimeError('MOD version changed')
    if results.exists(): raise RuntimeError(f'Refusing to overwrite {results}; preserve failed run and use a new campaign')
    free_ports()
    results.parent.mkdir(parents=True,exist_ok=True)
    logpath=results.parent/(item['name']+'.supervisor.log')
    policy=cfg.get('proxy_connection_policy','keepalive')
    if policy not in ('keepalive','close'): raise ValueError('Invalid proxy_connection_policy')
    env={**os.environ,'VLLM_HUST_UTILITY_VICTIM_ENABLE':'0','VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH':'1',
         'PREFIX_ROUTING_PROXY_CONNECTION_POLICY':policy}
    driver=[sys.executable,str(ROOT/'scripts/serve_prefix_ab.py'),item['arm'],cfg['model'],'--profile',cfg['profile'],'--output',str(results)]
    owner={'item':item,'preregistration_sha256':sha(evidence/'preregistration.json'),'identity_sha256':sha(evidence/'identity.json'),'started_unix':time.time(),
           'proxy_connection_policy':policy,'ingress_proxy_sha256':sha(ROOT/'benchmarks/prefix_routing_random_proxy.py')}
    with logpath.open('w',encoding='utf-8') as log:
        server=subprocess.Popen(driver,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        owner['supervisor_pid']=server.pid
        client=None
        try:
            deadline=time.monotonic()+cfg['startup_timeout_seconds']
            while not (results/'ready.json').exists():
                if server.poll() is not None: raise RuntimeError(f'Server failed; inspect {logpath}')
                if time.monotonic()>deadline: raise RuntimeError('Startup timeout')
                time.sleep(2)
            write(results/'ownership.json',owner)
            cmd=[sys.executable,str(ROOT/'scripts/run_prefix_client.py'),'--run-dir',str(results),'--concurrency',str(item['c']),'--label','formal']
            if item['suite']=='swe':
                cmd+=['--kind','swe','--duration',str(cfg['duration_seconds']),'--swe-client',cfg['swe_client'],'--workload',cfg['workload']]
            else:
                n=32 if item['suite']=='smoke' else cfg['control_requests']
                prefixes=n if item['suite']=='negative' else (1 if item['suite']=='smoke' else cfg['positive_prefixes'])
                cmd+=['--kind','prefix','--num-prompts',str(n),'--num-prefixes',str(prefixes),'--prefix-len','8192']
            write(results/'client-command.json',cmd)
            with (results/'client-driver.log').open('w',encoding='utf-8') as client_log:
                client=subprocess.Popen(cmd,env=env,stdout=client_log,stderr=subprocess.STDOUT)
                while client.poll() is None:
                    if server.poll() is not None: raise RuntimeError('Server exited while client running')
                    stamp=time.time(); sample={'unix':stamp,'errors':[]}
                    folder=results/'telemetry'; folder.mkdir(exist_ok=True)
                    for port in (18180,18181,18182):
                        endpoint='/_prefix_routing_benchmark/stats' if port==18180 else '/metrics'
                        try:
                            with urllib.request.urlopen(f'http://127.0.0.1:{port}{endpoint}',timeout=3) as r:
                                (folder/f'{stamp:.3f}-{port}.txt').write_bytes(r.read())
                        except Exception as exc: sample['errors'].append(str(exc))
                    command(['npu-smi','info'],folder/f'{stamp:.3f}-hardware.txt')
                    with (folder/'samples.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps(sample)+'\n')
                    time.sleep(10)
                owner['client_exit_code']=client.returncode
        finally:
            if client is not None and client.poll() is None:
                client.terminate()
                try: client.wait(timeout=30)
                except subprocess.TimeoutExpired: client.kill(); client.wait()
            if server.poll() is None: server.send_signal(signal.SIGTERM)
            try: server.wait(timeout=420)
            except subprocess.TimeoutExpired:
                owner['cleanup_timeout']=True
                # No broad kill: retain ownership and require manual inspection.
                write(results.parent/(item['name']+'.incomplete.json'),owner)
                raise RuntimeError(f'Owned supervisor {server.pid} did not exit; stop campaign and inspect')
            owner['server_exit_code']=server.returncode
            owner['finished_unix']=time.time()
            if results.exists():
                write(results/'ownership.json',owner)
                command(['npu-smi','info'],results/'hardware-after.txt')
    free_ports()
    finish_run(item, results, owner)

def finish_run(item, results, owner):
    if owner.get('client_exit_code')!=0 or owner.get('server_exit_code')!=0 or owner.get('cleanup_timeout'):
        raise RuntimeError('Run failed; retained raw artifacts')
    meta=read(results/'server-metadata.json')
    if (results/'ready.json').exists() or not meta.get('stopped_at_unix') or meta.get('forced_kill_observed'):
        raise RuntimeError('Clean server shutdown not established; refusing completion')
    measurement=results/('swe-formal' if item['suite']=='swe' else 'prefix-formal')
    if (measurement/'exit-code.txt').read_text().strip()!='0': raise RuntimeError('Client receipt is not success')
    if item['suite']=='swe': recompute(measurement/'measurement')
    else:
        result=read(measurement/'result.json')
        if result['failed']!=0 or result['completed']!=result['num_prompts']: raise RuntimeError('Incomplete prefix requests')
    check=subprocess.run([sys.executable,str(ROOT/'scripts/check_runtime_evidence.py'),str(results)],capture_output=True,text=True)
    (results/'runtime-check.txt').write_text(check.stdout+check.stderr,encoding='utf-8')
    # A no-trigger ON is an observed outcome, never silently discarded/re-run.
    if not (results/'runtime-evidence.json').exists(): raise RuntimeError('Missing runtime evidence')
    report=read(results/'runtime-evidence.json')
    if report['utility_events'] or (item['arm']!='on' and (report['prefix_events'] or report['process_counters'])):
        raise RuntimeError('Activation contamination')
    if (results/'hardware-after.txt').read_text().count('No running processes found') < 4:
        raise RuntimeError('NPU release not confirmed; inspect hardware-after.txt before continuing')
    write(results/'complete.json',{'complete':True,'runtime_check_exit':check.returncode,'owner':owner})

def recompute(folder):
    s=read(folder/'summary.json'); c=read(folder/'config.json')
    if not s['valid'] or s['aborted'] or s['failed_requests']!=0: raise ValueError('Invalid SWE window')
    duration=c['duration']
    if duration!=900 or s['measurement_seconds']!=duration or c['chips']!=4: raise ValueError('Wrong duration/chip normalization')
    rows=[json.loads(x) for x in (folder/'requests.jsonl').read_text().splitlines()]
    if len(rows)!=s['requests_started'] or not all(r['success'] and len(r['token_ids'])==r['expected_output_tokens'] for r in rows): raise ValueError('Request/token validation failed')
    done=[r for r in rows if r['end']<=duration]
    tokens=sum(n for r in rows for t,n in r['chunks'] if 0<=t<duration)
    if tokens!=s['observed_output_tokens_in_window'] or len(done)!=s['requests_completed_in_window']: raise ValueError('Raw window mismatch')
    speeds=[r['decode_tokens_per_second'] for r in done if r['decode_tokens_per_second'] is not None]
    if not speeds: raise ValueError('No completed decode samples')
    m={'output_tps':tokens/duration,'output_tps_per_chip':tokens/duration/4,'decode_p90_tps':quantile(speeds,.9),'ttft_p95_ms':quantile([r['ttft_seconds']*1000 for r in done],.95),'tpot_p95_ms':quantile([1000/x for x in speeds],.95),'e2e_p95_ms':quantile([r['e2e_seconds']*1000 for r in done],.95),'completed_requests':len(done)}
    for r in done:
        if not math.isclose(r['ttft_seconds'],r['first_token']-r['start'],rel_tol=1e-8,abs_tol=1e-8): raise ValueError('TTFT mismatch')
        if not math.isclose(r['e2e_seconds'],r['last_token']-r['start'],rel_tol=1e-8,abs_tol=1e-8): raise ValueError('E2E mismatch')
        if r['decode_tokens_per_second'] is not None and not math.isclose(r['decode_tokens_per_second'],(len(r['token_ids'])-1)/(r['last_token']-r['first_token']),rel_tol=1e-8): raise ValueError('Decode mismatch')
    for value,expected in [(m['output_tps'],s['output_tokens_per_second']),(m['decode_p90_tps'],s['decode_tokens_per_second_p90']),(m['ttft_p95_ms']/1000,s['ttft_seconds_p95'])]:
        if not math.isclose(value,expected,rel_tol=1e-8): raise ValueError('Summary mismatch')
    return m,c,s

def mechanism(run):
    report=read(run/'runtime-evidence.json')
    counts={}
    for p in report['process_counters']:
        for comp, values in p['data']['counters'].items():
            for k,v in values.items(): counts[comp+'.'+k]=counts.get(comp+'.'+k,0)+v
    deltas={}
    for node in (0,1):
        for metric in ('prefix_cache_queries_total','prefix_cache_hits_total','num_preemptions_total'):
            vals=[]
            for when in ('before','after'):
                p=next(iter(run.glob(f'*-formal/node{node}.{when}.prom')),None)
                lines=[] if p is None else [l for l in p.read_text().splitlines() if l.startswith('vllm:'+metric+'{')]
                vals.append(sum(float(l.split()[-1]) for l in lines) if lines else None)
            deltas[f'node{node}.{metric}']=None if None in vals else vals[1]-vals[0]
    return {'counters':counts,'native_deltas':deltas,'utility_events':len(report['utility_events']),'scope':'client invocation including drain; MOD counters whole fresh server lifetime','per_request_causal_attribution':False}

def analyze(a):
    ev=a.output/'evidences'; cfg=read(ev/'preregistration.json'); out=a.output/'analysis'; out.mkdir(exist_ok=True)
    rows=[]; excluded=[]; controls=[]
    for item in plan(cfg):
        run=a.output/'results'/item['name']
        try:
            owner=read(run/'complete.json')['owner']
            if owner['preregistration_sha256']!=sha(ev/'preregistration.json') or owner['identity_sha256']!=sha(ev/'identity.json'): raise ValueError('Evidence identity mismatch')
            meta=read(run/'server-metadata.json')
            if meta['arm']!=item['arm'] or meta['chips']!=4 or meta['profile']!=read(ev/'profile.json'):
                raise ValueError('Server configuration mismatch')
            for node in meta['servers']:
                env=node['environment']
                if env['VLLM_HUST_UTILITY_VICTIM_ENABLE']!='0' or env['VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH']!='1': raise ValueError('Utility configuration contaminated')
                if env['VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH']!=('0' if item['arm']=='on' else '1'): raise ValueError('Wrong MOD switch')
            if meta.get('forced_kill_observed'): raise ValueError('Forced kill observed')
            mech=mechanism(run)
            if mech['utility_events']: raise ValueError('Utility active')
            if item['suite']!='swe':
                result=read(run/'prefix-formal/result.json')
                if result['failed'] or result['completed']!=result['num_prompts']: raise ValueError('Control failed')
                controls.append({**item,'metrics':result,'mechanism':mech}); continue
            m,c,s=recompute(run/'swe-formal/measurement')
            if c['workload_sha256']!=read(ev/'identity.json')['workload_sha256'] or c['concurrency']!=item['c']: raise ValueError('Client protocol changed')
            rows.append({**item,'metrics':m,'mechanism':mech,'client_run_id':c['run_id'],'tokenizer':c['tokenizer'],'workload_sha256':c['workload_sha256'],'summary':s,'requests_sha256':sha(run/'swe-formal/measurement/requests.jsonl')})
        except (OSError,ValueError,KeyError,StopIteration) as exc: excluded.append({**item,'reason':str(exc)})
    write(out/'runs.json',rows); write(out/'controls.json',controls); write(out/'excluded.json',excluded)
    cells=[]
    for c in cfg['concurrencies']:
        pair={arm:sorted([r for r in rows if r['c']==c and r['arm']==arm],key=lambda r:r['repeat']) for arm in ('off','on')}
        if any(len(x)!=3 for x in pair.values()): continue
        gains=[pair['on'][i]['metrics']['output_tps']/pair['off'][i]['metrics']['output_tps']-1 for i in range(3)]
        boots=[statistics.mean(x) for x in itertools.product(gains,repeat=3)]
        cells.append({'c':c,'paired_gain_mean':statistics.mean(gains),'paired_gain_bootstrap95':[quantile(boots,.025),quantile(boots,.975)],'off_output_tps_mean':statistics.mean(r['metrics']['output_tps'] for r in pair['off']),'on_output_tps_mean':statistics.mean(r['metrics']['output_tps'] for r in pair['on']),'off_ttft_p95_ms_mean':statistics.mean(r['metrics']['ttft_p95_ms'] for r in pair['off']),'on_ttft_p95_ms_mean':statistics.mean(r['metrics']['ttft_p95_ms'] for r in pair['on']),'n_pairs':3,'ci_caution':'Only 3 pairs; discrete bootstrap exploratory, not strong statistical assurance'})
    write(out/'paired-comparison.json',cells)
    control_cells=[]
    for suite in ('positive','negative'):
        for c in cfg['control_concurrencies']:
            pair={arm:sorted([r for r in controls if r['suite']==suite and r['c']==c and r['arm']==arm],key=lambda r:r['repeat']) for arm in ('off','on')}
            if any(len(x)!=3 for x in pair.values()): continue
            gains=[pair['on'][i]['metrics']['output_throughput']/pair['off'][i]['metrics']['output_throughput']-1 for i in range(3)]
            boots=[statistics.mean(x) for x in itertools.product(gains,repeat=3)]
            ci=[quantile(boots,.025),quantile(boots,.975)]
            matches=[r['mechanism']['counters'].get('routing_policy.prefix_hit_decisions',0) for r in pair['on']]
            control_cells.append({'suite':suite,'c':c,'paired_gain_mean':statistics.mean(gains),'bootstrap95':ci,'on_match_counts':matches,'statistical_candidate':(ci[0]>0 and min(matches)>0) if suite=='positive' else (ci[0]>=-cfg['negative_effect_equivalence_margin'] and ci[1]<=cfg['negative_effect_equivalence_margin'] and max(matches)<=2),'scope':'Includes vLLM benchmark connection probe; inspect logs before accepting <=2 negative matches'})
    write(out/'control-comparison.json',control_cells)
    baseline=[r for r in rows if r['arm']=='off']
    pareto=[]
    for r in rows:
        if r['arm']!='on': continue
        m=r['metrics']
        dominates=lambda b: b['output_tps_per_chip']>=m['output_tps_per_chip'] and b['decode_p90_tps']>=m['decode_p90_tps'] and b['ttft_p95_ms']<=m['ttft_p95_ms']
        if baseline and not any(dominates(b['metrics']) for b in baseline): pareto.append(r['name'])
    write(out/'pareto-candidates.json',{'on_not_weakly_dominated_by_measured_off':pareto,'reference_count':len(baseline),'axes':['output_tps_per_chip up','decode_p90_tps up','ttft_p95_ms down'],'global_pareto_claim':False,'quality_and_uncertainty_review_required':True})
    with (out/'runs.csv').open('w',newline='',encoding='utf-8') as f:
        fields=['name','arm','c','repeat','output_tps','output_tps_per_chip','decode_p90_tps','ttft_p95_ms','tpot_p95_ms','e2e_p95_ms','prefix_hit_decisions','matched_tokens','native_cache_queries','native_cache_hits','native_cache_hit_ratio']
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in rows:
            sums={}
            for metric in ('queries','hits'):
                values=[r['mechanism']['native_deltas'][f'node{i}.prefix_cache_{metric}_total'] for i in (0,1)]
                sums[metric]=None if None in values or any(v<0 for v in values) else sum(values)
            w.writerow({**{k:r[k] for k in ('name','arm','c','repeat')},**{k:v for k,v in r['metrics'].items() if k in fields},'prefix_hit_decisions':r['mechanism']['counters'].get('routing_policy.prefix_hit_decisions',0),'matched_tokens':r['mechanism']['counters'].get('routing_policy.matched_tokens',0),'native_cache_queries':sums['queries'],'native_cache_hits':sums['hits'],'native_cache_hit_ratio':None if not sums['queries'] or sums['hits'] is None else sums['hits']/sums['queries']})
    eligible=[x for x in cells if x['c'] in cfg['primary_cells'] and x['paired_gain_mean']>=.05 and x['paired_gain_bootstrap95'][0]>0 and x['on_ttft_p95_ms_mean']<=cfg['ttft_p95_ratio_limit']*x['off_ttft_p95_ms_mean']]
    write(out/'gates.json',{'performance':{'P-G1':{'status':'pending','reason':'Historical raw OFF/ON not supplied; current release cannot substitute'},'P-G2':{'status':'review_required','reason':'Inspect controls.json: positive benefit and negative effect disappearance, native deltas and MOD counters; KILL is not negative control'},'P-G3':{'status':'pending','reason':'Need historical implementation versus extracted historical MOD throughput within 3%; dev4 Mamba adaptation must be disclosed separately'},'P-G4':{'status':'candidate' if len(eligible)>=2 else 'not_established','candidate_cells':[x['c'] for x in eligible],'requires':'Correctness/quality and all preregistered SLOs reviewed; 3 repeats exploratory'},'P-G5':{'status':'review_required','reason':'Compare whole-run throughput/chip and latency against matched OFF points, not historic utility baseline'}},'engineering':{'E-G1':'pending historical inventory review','E-G2':'pending extraction disposition review; dev4 Mamba adaptation disclosed','E-G3':'check wheel SHA, manifest, offline installation and local tests','E-G4':'check preflight plus Core/Ascend source fingerprints in evidences','E-G5':'check four smoke records OFF-ON-KILL-OFF, post-stop hardware and port release'},'missing_or_invalid':len(excluded),'formal_complete':len(rows)==30})
    # Candidate points preserve each entire observation. Cohort admission is explicit.
    cohort='prefix-routing-qwen35-swe-dev4-review'
    points=[]
    for r in rows:
        points.append({'id':cfg['campaign']+'-'+r['name'],'cohort_id':cohort,'label':f"Prefix Routing {r['arm'].upper()} C{r['c']} r{r['repeat']}",'configuration':{'engine':'vLLM + vLLM-Ascend','engine_version':read(ev/'identity.json')['versions']['vllm'],'mods':['prefix-routing'] if r['arm']=='on' else [],'hardware':{'label':'Ascend 910B2','accelerator_count':4},'context_capacity_tokens':262144,'parameters':{'tensor_parallel_size':2,'data_parallel_size':1,'independent_replicas':2,'expert_parallel':True,'prefix_caching':True,'utility_victim_enabled':False,'mod_version':'0.1.0.dev4','campaign_identity_sha256':sha(ev/'identity.json')}},'load':{'concurrency':r['c'],'repeat':r['repeat'],'session_rotation_depth':1,'concurrency_series':cfg['campaign']+'-'+r['arm']+'-r'+str(r['repeat']),'presentation_group':{'id':'prefix-routing-'+r['arm'],'label_en':'Prefix Routing '+r['arm'].upper(),'label_zh':'Prefix Routing '+r['arm'].upper()}},'metrics':r['metrics'],'evidence':{'status':'measured','execution_kind':'real-online','profile':'smoke','measurement_seconds':900,'run_ids':[r['client_run_id']],'aggregation':'One unpooled 900s observation; all 3 repeats retained','benchmark_protocol':{'protocol_id':'swe-prefix-reuse/v1','prepared_workload_sha256':r['workload_sha256']},'publication_status':'pending checkpoint/workload equivalence, artifact URL and cohort admission'}})
    write(out/'points.add.json',points); write(out/'display_series_ids.add.json',sorted({p['load']['concurrency_series'] for p in points}))
    if cells:
        ymax=max(x[k] for x in cells for k in ('off_output_tps_mean','on_output_tps_mean'))*1.1
        svg=['<svg xmlns="http://www.w3.org/2000/svg" width="800" height="420"><rect width="100%" height="100%" fill="white"/><text x="60" y="25">PR173 SWE: mean output tokens/s (3 repeats), 4 chips</text>']
        for arm,color in [('off','#777'),('on','#176dcc')]:
            coords=[(60+cfg['concurrencies'].index(x['c'])*170,360-x[arm+'_output_tps_mean']/ymax*300) for x in cells]
            svg.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="'+ ' '.join(f'{x},{y}' for x,y in coords)+'"/>')
            for cell,(x,y) in zip(cells,coords): svg.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{color}"/><text x="{x}" y="{y-8}" font-size="12">{arm} {cell[arm+"_output_tps_mean"]:.2f}</text>')
        for i,c in enumerate(cfg['concurrencies']): svg.append(f'<text x="{60+i*170}" y="390">C{c}</text>')
        svg.append('</svg>'); (out/'throughput.svg').write_text(''.join(svg),encoding='utf-8')
    print(f'Analyzed {len(rows)}/30 formal observations. Exclusions: {len(excluded)}. No data published.')

def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['capture','plan','run','recover','analyze','seal']); p.add_argument('--config',type=Path,default=ROOT/'campaign.json'); p.add_argument('--output',type=Path,required=True); p.add_argument('--suite',choices=['smoke','swe','positive','negative']); p.add_argument('--only',help='Exact run name; never silently resume/overwrite')
    p.add_argument('--resume',action='store_true',help='Explicitly skip completed runs with matching campaign receipts; incomplete runs still stop')
    a=p.parse_args()
    if a.action=='capture': capture(a); return
    if a.action=='analyze': analyze(a); return
    if a.action=='seal':
        lines=[sha(x)+'  '+x.relative_to(a.output).as_posix() for x in sorted(a.output.rglob('*')) if x.is_file() and x.name!='SHA256SUMS.txt']
        (a.output/'SHA256SUMS.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8',newline='\n'); return
    cfg=read(a.output/'evidences/preregistration.json')
    items=[x for x in plan(cfg) if (not a.suite or x['suite']==a.suite) and (not a.only or x['name']==a.only)]
    if a.action=='plan': print(json.dumps(items,indent=2)); return
    if not items: p.error('No matching runs')
    if a.action=='recover':
        if not a.only or len(items)!=1: p.error('recover requires --only one exact run name')
        item=items[0]; run=a.output/'results'/item['name']; owner=read(run/'ownership.json')
        if (run/'complete.json').exists(): p.error('Already complete; no changes made')
        if owner['item']!=item or owner['preregistration_sha256']!=sha(a.output/'evidences/preregistration.json') or owner['identity_sha256']!=sha(a.output/'evidences/identity.json'):
            p.error('Campaign identity mismatch')
        free_ports()
        recovery_hardware=run/'hardware-recovery.txt'
        if command(['npu-smi','info'],recovery_hardware)!=0 or recovery_hardware.read_text().count('No running processes found')<4:
            p.error('Current four-device release not established')
        finish_run(item,run,owner)
        write(run/'recovery.json',{'unix':time.time(),'reason':'Finish interrupted post-run validation only; no requests replayed','original_owner_sha256':sha(run/'ownership.json')})
        print('RECOVERED',item['name']); return
    for item in items:
        complete=a.output/'results'/item['name']/'complete.json'
        if a.resume and complete.exists():
            receipt=read(complete); owner=receipt['owner']
            if receipt.get('complete') is not True or owner['item']!=item or owner['preregistration_sha256']!=sha(a.output/'evidences/preregistration.json') or owner['identity_sha256']!=sha(a.output/'evidences/identity.json'):
                p.error('Completed run campaign identity mismatch')
            print('KEEP completed',item['name'],flush=True); continue
        print('START',item['name'],flush=True); run_one(cfg,item,a.output/'evidences',a.output/'results'/item['name'])

if __name__=='__main__': main()
