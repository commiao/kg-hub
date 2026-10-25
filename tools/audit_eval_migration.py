#!/usr/bin/env python3
"""Compare published and migrated business semantics against read-only historical bytes.

Runs separate interpreters; neither fresh probes nor embedding downloads are allowed.
The full retrieval/admission flow is additionally covered by the legacy fixture suite.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

CHILD = r'''
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
if sys.argv[2]=='published':
 from tools import extract_bench as bench
 from utils import bench_reference as ref
 from utils.single_pass_extract import observation_title
 from schema import ENTITY_TYPES,EDGE_TYPES
else:
 from kg_hub_eval.business.tools import extract_bench as bench
 from kg_hub_eval.business.utils import bench_reference as ref
 from kg_hub_eval.business.utils.single_pass_extract import observation_title
 from kg_hub_eval.business.schema import ENTITY_TYPES,EDGE_TYPES
records=[]
for path in sorted(root.rglob('run.json')):
 d=path.parent;m=json.loads(path.read_text())
 try:
  ref.verify_run(d,m)
  status='verified'
 except (ValueError,SystemExit,OSError,KeyError,TypeError) as e:
  status=str(e)
 items=ref.result_items(d)
 sources={}
 if (d/'input-data.json').exists():
  for part in ref.read(d/'input-data.json').get('parts',[]):
   sources.update({int(i['id']):i for i in part['items']})
 metrics={i:bench.item_metrics(item,observation_title(sources.get(i,{}).get('episode_body','')),ENTITY_TYPES,EDGE_TYPES)
          for i,item in items.items()}
 records.append({'run':str(path.relative_to(root)),'verification':status,
  'items_sha256':ref.digest(items),'metrics_sha256':ref.digest(metrics),
  'cost':{k:m.get(k) for k in ('calls','tokens','tokens_known','credits','credits_known','ms','preflight')},
  'evidence_version':m.get('evidence',{}).get('version'),
  'run_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
print(json.dumps(records,sort_keys=True,ensure_ascii=False))
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('published-root','history-root','out'):p.add_argument('--'+name,required=True)
    args=p.parse_args();root=Path(__file__).resolve().parents[1]
    records=[]
    for version in ('published','migrated'):
        env=dict(os.environ)
        env['PYTHONPATH']=args.published_root if version=='published' else str(root/'adapters/evaluation/src')+os.pathsep+env.get('PYTHONPATH','')
        result=subprocess.run([sys.executable,'-c',CHILD,args.history_root,version],cwd='/private/tmp',env=env,
                              capture_output=True,text=True,check=True)
        records.append(json.loads(result.stdout))
    if records[0]!=records[1]:
        by_old={r['run']:r for r in records[0]};by_new={r['run']:r for r in records[1]}
        changes=[r for r in sorted(set(by_old)|set(by_new)) if by_old.get(r)!=by_new.get(r)]
        raise SystemExit('migration parity mismatch: '+str(changes))
    result={'historical_runs':len(records[0]),'byte_equal_semantics':True,'real_model_calls':0,
            'source_writes':0,'checks':['original evidence verdict/rejection','business result parsing',
             'discipline metrics','cost fields','evidence version','original run hash'],
            'limits':['historical retrieval embeddings not regenerated','new formal model qualification not issued'],
            'records':records[0]}
    path=Path(args.out);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as file:json.dump(result,file,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='records'},ensure_ascii=False))

if __name__=='__main__':main()
