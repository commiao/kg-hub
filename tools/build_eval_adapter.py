#!/usr/bin/env python3
"""Build a namespaced, source-free business dependency closure. No production config/secrets."""
import ast
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'adapters/evaluation/src/kg_hub_eval/business'
ASSETS=TARGET.parent/'assets'
ASSETS.mkdir(exist_ok=True)
files=['schema.py','utils/extracted_ingest.py','utils/embedding_profile.py','utils/single_pass_extract.py']
for name in files:
    source=(ROOT/name).read_text()
    if name=='utils/single_pass_extract.py':
        tree=ast.parse(source)
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='observation_title')
        prefix=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_TITLE_PREFIX' for t in n.targets))
        source='import re\nfrom utils.extracted_ingest import MAX_FACT_LEN\n'+ast.get_source_segment(source,prefix)+'\n'+ast.get_source_segment(source,fn)+'\n'
    source=re.sub(r'from utils(?=\s|\.)','from kg_hub_eval.business.utils',source)
    target=TARGET/name
    target.parent.mkdir(exist_ok=True,parents=True)
    target.write_text(source)
(TARGET/'config').mkdir(exist_ok=True)
shutil.copyfile(ROOT/'config/agent_extract_prompt.md',TARGET/'config/agent_extract_prompt.md')
release=ROOT/'RELEASE.json'
revision=json.loads(release.read_text())['commit'] if release.is_file() else subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
manifest={'source_revision':revision,
          'files':{str(p.relative_to(TARGET)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(TARGET.rglob('*')) if p.is_file() and p.suffix in {'.py','.md'}},
          'history':'original evidence paths and bytes remain read-only',
          'campaign':'delegates to a separately pinned released KG business backend'}
(ASSETS/'provenance.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps({'business_assets':len(manifest['files']),'private_data_included':False}))
