import re
import sys
from pathlib import Path

import yaml

resources = list(yaml.safe_load_all(Path(sys.argv[1]).read_text()))
pod = next(r for r in resources if r and r.get("kind") == "Deployment")["spec"]["template"]["spec"]
assert pod["automountServiceAccountToken"] is False
containers = {c["name"]: c for c in pod["containers"]}
assert {m["name"] for m in containers["archiver"]["volumeMounts"]} == {"scripts", "archive-tmp"}
assert {v["name"] for v in containers["archiver"]["env"]} == {"K6_UI_SCRIPTS_DIR"}
assert {"kube-token", "session", "azure-token"} <= {m["name"] for m in containers["web"]["volumeMounts"]}
assert containers["archiver"]["command"] == ["python", "-m", "k6_ui.archiver"]
scripts = next(v for v in pod["volumes"] if v["name"] == "scripts")
assert len(scripts) == 2 and ({"emptyDir", "configMap"} & scripts.keys())
assert not any(p["port"] == 8081 for r in resources if r and r.get("kind") == "Service" for p in r["spec"]["ports"])
label = re.compile(r"(([A-Za-z0-9][-A-Za-z0-9_.]{0,61})?[A-Za-z0-9])?")
for r in resources:
    if r:
        value = r["metadata"].get("labels", {}).get("helm.sh/chart", "")
        assert label.fullmatch(value), f"{r['kind']} has an invalid helm.sh/chart label: {value}"
print("Chart isolates the script archiver from credentials.")
