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
assert not any(p["port"] == 8081 for r in resources if r and r.get("kind") == "Service" for p in r["spec"]["ports"])
print("Chart isolates the script archiver from credentials.")
