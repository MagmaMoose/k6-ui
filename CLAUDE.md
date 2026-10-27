# k6-ui

FastAPI (`src/k6_ui`) serves React (`frontend`) and creates k6-operator TestRuns. Helm lives in `charts/k6-ui`. Deployment-specific catalogs, script sources, targets, credentials and branding stay outside this public repository.

- Archive the load shape before creating a TestRun. Credentials reach runners only through Secret references.
- Kubernetes user/group claim mapping must match the API server. Every browser operation checks SubjectAccessReview.
- Save every runner's exit code and output before deleting a TestRun. Run-record ConfigMaps own the execution resources.
- No postponed annotations in `app.py`: dependency aliases are callback-scoped.
- No inline script/style: the CSP allows only external assets.
- No employer or client names, hosts, identities, scripts or other private deployment data in code, docs, tests, commits or PR text.
