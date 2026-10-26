#!/usr/bin/env python3
"""Run every panel of the telemetry dashboards through Grafana's own query API, once per app.

It goes through the plugin, macros and datasource exactly as a browser would, so a passing run means
every panel loads without an error. Panels allowed to be empty for an app (e.g. errors where there
are none) are listed in MAY_BE_EMPTY.

    GRAFANA_URL=http://10.20.0.20:30300 GRAFANA_PASSWORD=... python3 check-telemetry-dashboards.py [env]
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

URL = os.environ.get("GRAFANA_URL", "http://10.20.0.20:30300").rstrip("/")
AUTH = "Basic " + base64.b64encode(f"admin:{os.environ['GRAFANA_PASSWORD']}".encode()).decode()
ENV = sys.argv[1] if len(sys.argv) > 1 else "demo"
CH_UID = "clickhouse-telemetry"
MAY_BE_EMPTY = {"오류 fingerprint · 릴리스별"}


def api(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:500]}


def ch_rows(sql):
    res = api("/api/ds/query", {"from": "now-30d", "to": "now", "queries": [
        {"refId": "A", "datasource": {"uid": CH_UID}, "editorType": "sql", "format": 1, "rawSql": sql}]})
    frame = res["results"]["A"]["frames"][0]
    return list(zip(*frame["data"]["values"])) if frame["data"]["values"] else []


def substitute(text, variables):
    for name, value in variables.items():
        text = text.replace("${" + name + "}", str(value)).replace("$" + name, str(value))
    return text


def default_uid():
    return next(d["uid"] for d in api("/api/datasources") if d.get("isDefault"))


def run_panel(panel, variables, default):
    queries = []
    for t in panel.get("targets", []):
        q = json.loads(substitute(json.dumps(t), variables))
        q.setdefault("datasource", panel["datasource"])
        # Panels follow the other dashboards here and name Prometheus as uid "" (the default
        # datasource). The browser resolves that; the query API needs the real uid.
        if not q["datasource"].get("uid"):
            q["datasource"] = {**q["datasource"], "uid": default}
        # A browser always sends these; without them Prometheus range queries come back empty.
        q.setdefault("intervalMs", 60000)
        q.setdefault("maxDataPoints", 500)
        queries.append(q)
    start = "now-" + panel.get("timeFrom", "30d")
    res = api("/api/ds/query", {"from": start, "to": "now", "queries": queries})
    if "_http_error" in res:
        return False, f"HTTP {res['_http_error']}: {res['_body']}"
    rows = 0
    for ref, r in res.get("results", {}).items():
        if r.get("error"):
            return False, r["error"][:300]
        for frame in r.get("frames", []):
            values = frame.get("data", {}).get("values", [])
            rows += len(values[0]) if values else 0
    return True, rows


def main():
    apps = [r[0] for r in ch_rows("SELECT app_id FROM core.apps ORDER BY app_id")]
    funnels = {r[0]: (r[1], r[2]) for r in ch_rows("SELECT app_id, name, window_ms FROM core.funnels")}
    default = default_uid()
    failures = 0
    for uid in ("telemetry-factory", "telemetry-app"):
        dash = api(f"/api/dashboards/uid/{uid}")["dashboard"]
        per_app = uid == "telemetry-app"
        for app in apps if per_app else [None]:
            variables = {"env": ENV}
            if app:
                name, window = funnels.get(app, ("", 0))
                variables.update({"app": app, "funnel": name, "funnel_window": window})
            for panel in dash["panels"]:
                if not panel.get("targets"):
                    continue
                ok, detail = run_panel(panel, variables, default)
                empty_ok = panel["title"] in MAY_BE_EMPTY
                if ok and detail == 0 and not empty_ok:
                    ok, detail = False, "no rows"
                failures += not ok
                print(f"{'ok  ' if ok else 'FAIL'} {uid:17} {app or '-':12} {panel['title'][:32]:32} {detail}")
    print(f"\n{failures} failing panel run(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
