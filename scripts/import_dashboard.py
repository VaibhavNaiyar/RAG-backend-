"""Push the ClaimCheck dashboard into a running Grafana (docker compose up -d first)."""
import json
import os
import sys
from pathlib import Path

import httpx

url = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
dash = json.loads((Path(__file__).resolve().parents[1] / "infra/grafana/dashboards/claimcheck.json").read_text())
r = httpx.post(f"{url}/api/dashboards/db", json={"dashboard": dash, "overwrite": True},
               auth=(os.environ.get("GRAFANA_USER", "admin"), os.environ.get("GRAFANA_PASSWORD", "admin")))
print(r.status_code, r.text[:200])
sys.exit(0 if r.is_success else 1)
