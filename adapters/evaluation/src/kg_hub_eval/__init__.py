"""KG graph-extraction semantics; execution/recovery live in llm-eval."""
from __future__ import annotations
import importlib
import json
import os
import subprocess
from pathlib import Path

from llm_eval.evidence import digest, need, read, sha, verify, write_sealed
from .business.tools import extract_bench as bench


class EntityExtraction:
    def describe(self):
        provenance=read(Path(__file__).parent/'assets/provenance.json')
        return {"sdk":1,"id":"kg_hub.entity_extract","version":"1","policy_version":"kg-admission-v2",
            "business_provenance":{"source_revision":provenance['source_revision'],"files_sha256":digest(provenance['files'])},
            "unit":"original observation","modes":["smoke","admission","blind","campaign"],
            "metrics":{"discipline":{"denominator":"observed source observations","semantic_truth":False},
                       "retrieval":{"requires":"frozen corpus, questions, background and embedding model"}},
            "policy":{"calibrated":False,"minimum_cases":30,"minimum_random_reviews":10,"minimum_source_quote":8,
                      "minimum_identity":"local-observed"},
            "admission_backend":"business CLI bench/admit: frozen reference, all KG thresholds, blind review and semantic admission-v2",
            "generic_path":"diagnostic only; authoritative KG admission stays in the migrated business backend"}

    def validate_case(self, case):
        source=case.get("input",{})
        need(type(source.get("id")) is int and isinstance(source.get("episode_body"),str), "frozen KG observation ID/body required")
        need(isinstance(case.get("context",{}).get("lookahead",[]),list), "invalid lookahead")

    def build_requests(self, cases, profile):
        unknown=set(profile)-{"chunk"}
        need(not unknown,"unsupported KG profile knobs")
        chunk=profile.get("chunk",5)
        need(type(chunk) is int and chunk>0,"positive chunk required")
        system,version=bench.bench_system_prompt()
        groups={}
        for case in cases:
            context=case.get("context",{})
            groups.setdefault(digest(context),[]).append(case)
        requests=[]
        for group in groups.values():
            for k in range(0,len(group),chunk):
                selected=group[k:k+chunk]
                requests.append({"id":"batch-"+str(len(requests)+1),"case_ids":[c["id"] for c in selected],
                    "system":system,"query":bench.build_query([c["input"] for c in selected],selected[0].get("context",{}).get("lookahead",[])),
                    "observation_ids":[c["input"]["id"] for c in selected],"prompt_version":version})
        return requests

    def parse_response(self, request, text):
        items=bench.parse_items(text,request["observation_ids"])
        return {"valid":set(items)==set(request["observation_ids"]),"value":list(items.values())}

    def aggregate_case(self, case, outputs):
        rows=[item for output in outputs for item in (output.get("value") or []) if item["id"]==case["input"]["id"]]
        need(len(rows)<=1,"duplicate business observation output")
        return {"valid":bool(rows),"value":rows[0] if rows else None}

    def evaluate_case(self, case, aggregate):
        from .business.schema import ENTITY_TYPES,EDGE_TYPES
        from .business.utils.single_pass_extract import observation_title
        title=observation_title(case["input"]["episode_body"])
        metrics=bench.item_metrics(aggregate.get("value"),title,ENTITY_TYPES,EDGE_TYPES)
        problems=bench.lint_items([(case["input"]["id"],title,"frozen")],{case["input"]["id"]:aggregate["value"]} if aggregate.get("value") else {})
        return {**metrics,"lint_problems":problems,"retrieval":None,"semantic_quality":None}

    def review_spec(self, case, aggregate):
        return case.get("criteria") or [{"id":"source-support","description":"Check responsibility, direction and factual support against original source"}]

    def business_cli(self, argv):
        need(argv and argv[0] in {"bench","runtime","regression","eval"},"unsupported evaluation business entry")
        modules={"bench":"extract_bench","regression":"extract_regression","eval":"extract_evaluation"}
        if argv[0]=="runtime":
            from llm_eval.executors.launcher import main
            return main(argv[1:])
        return importlib.import_module("kg_hub_eval.business.tools."+modules[argv[0]]).main(argv[1:])

    def campaign(self, argv, *, backend_manifest=None):
        """Only the released KG backend may claim/submit/read back real graph data."""
        configured=os.environ.get("LLM_EVAL_KG_BACKEND_MANIFEST")
        need(backend_manifest or configured,"campaign requires pinned released KG backend manifest; see adapter guide")
        manifest=verify(backend_manifest or read(configured))
        root=Path(manifest["release_root"]).resolve()
        release=read(root/"RELEASE.json")
        need(release.get("commit")==manifest["commit"],"backend release identity mismatch")
        required={"tools/extract","tools/extract_cli.py","tools/extract_campaign.py","tools/agent_extract.py",
                  "utils/extraction_quality.py","utils/semantic_regression.py","utils/bench_reference.py",
                  "utils/extracted_ingest.py","config/agent_extract_prompt.md","RELEASE.json"}
        need(required <= set(manifest["files"]),"incomplete campaign backend inventory")
        actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and
                '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'}}
        need(actual==set(manifest["files"]),"campaign backend file inventory changed")
        need(all(sha(root/p)==h for p,h in manifest["files"].items()),"backend bytes changed")
        entry=root/'tools/extract'
        need(entry.is_file() and "tools/extract" in manifest["files"],"released entry not bound")
        return subprocess.run([str(entry),"campaign",*argv],check=False,
            env=dict(os.environ,LLM_EVAL_DELEGATED_BACKEND=manifest["commit"])).returncode


def pin_campaign_backend(release_root, out):
    root=Path(release_root).resolve()
    release=read(root/'RELEASE.json')
    need(release.get('name')=='kg-hub' and len(release.get('commit',''))==40,"released KG backend required")
    files={str(p.relative_to(root)):sha(p) for p in sorted(root.rglob('*')) if p.is_file() and
           '__pycache__' not in p.parts and p.suffix not in {'.pyc','.pyo'}}
    return write_sealed(out,{'type':'kg-campaign-backend','release_root':str(root),'commit':release['commit'],'files':files})
