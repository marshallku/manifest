#!/usr/bin/env python3
"""Generate configmap-dashboard-telemetry.yaml: the product-event dashboards (ClickHouse on analytics01)."""
import json
import sys

CH = {"type": "grafana-clickhouse-datasource", "uid": "clickhouse-telemetry"}
PROM = {"type": "prometheus", "uid": ""}
PLUGIN = "4.22.1"

# Fixed by name, so an app keeps its colour whatever the filter shows (colour follows the entity).
COLOR = {"mode": "palette-classic-by-name"}


def ch(sql, fmt="table", ref="A"):
    return {
        "refId": ref,
        "datasource": CH,
        "editorType": "sql",
        "format": 1 if fmt == "table" else 0,
        "queryType": fmt if fmt == "table" else "timeseries",
        "rawSql": sql.strip(),
        "pluginVersion": PLUGIN,
    }


def prom(expr, legend, ref="A", instant=False):
    q = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend, "range": not instant,
         "instant": instant}
    if instant:
        q["format"] = "table"  # one row per series, labels as columns
    return q


_id = [0]


def panel(kind, title, targets, x, y, w, h, desc="", ds=CH, defaults=None, options=None, overrides=None,
          transformations=None, time_from=None):
    _id[0] += 1
    p = {
        "id": _id[0],
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": ds,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": {"color": COLOR, **(defaults or {})}, "overrides": overrides or []},
        "options": options or {},
    }
    if transformations:
        p["transformations"] = transformations
    if time_from:
        # Pipeline health is about the last hours, and Prometheus history only starts at deploy time.
        p["timeFrom"] = time_from
    return p


def row(title, y):
    _id[0] += 1
    return {"id": _id[0], "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def text(content, x, y, w, h):
    _id[0] += 1
    return {"id": _id[0], "type": "text", "title": "", "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": content}}


def var_query(name, label, sql, current=None, hide=0):
    v = {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": CH,
        "query": {"editorType": "sql", "rawSql": sql, "format": 1, "queryType": "table", "pluginVersion": PLUGIN},
        "definition": sql,
        "refresh": 1,
        "hide": hide,
        "sort": 1,
        "includeAll": False,
        "multi": False,
        "options": [],
    }
    if current:
        v["current"] = {"text": current, "value": current}
    return v


def dashboard(uid, title, panels, variables, desc, time_from="now-30d"):
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["telemetry", "product"],
        "timezone": "Asia/Seoul",
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 39,
        "time": {"from": time_from, "to": "now"},
        "refresh": "1m",
        "templating": {"list": variables},
        "annotations": {"list": []},
        "links": [{"title": "Telemetry", "type": "dashboards", "tags": ["telemetry"], "asDropdown": False,
                   "includeVars": True, "keepTime": True}],
        "panels": panels,
        "id": None,
    }


ENV = var_query("env", "env", "SELECT DISTINCT env FROM raw.events ORDER BY env", current="demo")
TS_DEFAULTS = {"custom": {"lineWidth": 2, "fillOpacity": 0, "pointSize": 8, "showPoints": "never",
                          "spanNulls": False, "axisSoftMin": 0}}
LEGEND = {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
          "tooltip": {"mode": "multi", "sort": "desc"}}

# ------------------------------------------------------------------ Factory Overview
y = 0
fo = []
fo.append(text(
    "**앱 공장 제품 이벤트 한눈에 보기.** 모든 수치는 `core.events`(중복 제거 뷰)에서, 시각은 수집기가 보정한 "
    "`est_occurred_at`(KST) 기준. `env=demo`는 `telemetry fake`가 만든 가짜 데이터다. "
    "앱을 누르면 앱 상세로 간다.", 0, y, 24, 2))
y += 2
fo.append(panel(
    "table", "앱별 상태 · 최근 7일", [ch("""
SELECT
    a.app_id AS app,
    a.identity AS identity,
    uniqExactIf(e.anon, e.dataset = 'measure' AND e.est_occurred_at >= now() - INTERVAL 7 DAY) AS actors_7d,
    uniqExactIf(e.anon, e.dataset = 'measure' AND e.est_occurred_at < now() - INTERVAL 7 DAY) AS actors_prev_7d,
    round(100 * (actors_7d - actors_prev_7d) / nullIf(actors_prev_7d, 0), 1) AS wow_pct,
    countIf(e.event_name = a.north_star AND e.est_occurred_at >= now() - INTERVAL 7 DAY) AS north_star_7d,
    round(100 * countIf(e.event_name = 'error.occurred' AND e.est_occurred_at >= now() - INTERVAL 7 DAY)
        / nullIf(countIf(e.event_name = 'screen.viewed' AND e.est_occurred_at >= now() - INTERVAL 7 DAY), 0), 2)
        AS errors_per_100_views,
    dateDiff('second', max(e.received_at), now64(3)) AS freshness_s,
    argMaxIf(e.release, e.est_occurred_at, e.event_name = 'screen.viewed') AS client_release
FROM core.apps AS a
LEFT JOIN (
    SELECT app_id, event_name, est_occurred_at, received_at, release, dataset, user.anonymous_id AS anon
    FROM core.events
    WHERE env = '${env}' AND est_occurred_at >= now() - INTERVAL 14 DAY
) AS e ON e.app_id = a.app_id
GROUP BY a.app_id, a.identity
ORDER BY a.app_id
""")], 0, y, 24, 6,
    desc="actors는 measure 데이터셋의 익명 id 수다. session_only 앱은 방문 수와 같다. north_star는 레지스트리의 "
         "앱별 핵심 이벤트 수. freshness는 마지막 수신 후 경과 초.",
    overrides=[
        {"matcher": {"id": "byName", "options": "app"}, "properties": [{"id": "links", "value": [
            {"title": "앱 상세", "url": "/d/telemetry-app?var-env=${env}&var-app=${__value.raw}&${__url_time_range}"}]}]},
        {"matcher": {"id": "byName", "options": "wow_pct"}, "properties": [
            {"id": "unit", "value": "percent"}, {"id": "displayName", "value": "전주 대비 %"}]},
        {"matcher": {"id": "byName", "options": "freshness_s"}, "properties": [
            {"id": "unit", "value": "s"}, {"id": "displayName", "value": "freshness"}]},
        {"matcher": {"id": "byName", "options": "errors_per_100_views"}, "properties": [
            {"id": "displayName", "value": "오류 / 100 화면"}]},
    ]))
y += 6
fo.append(panel(
    "timeseries", "일별 활성 actor (KST)", [ch("""
SELECT toStartOfDay(est_occurred_at, 'Asia/Seoul') AS time, app_id, uniqExact(user.anonymous_id) AS actors
FROM core.events
WHERE env = '${env}' AND dataset = 'measure' AND $__timeFilter(est_occurred_at)
GROUP BY time, app_id
ORDER BY time
""", fmt="timeseries")], 0, y, 12, 8, desc="measure 데이터셋 익명 id 기준. 같은 사람이 기기를 바꾸면 둘로 센다.",
    defaults=TS_DEFAULTS, options=LEGEND))
fo.append(panel(
    "timeseries", "시간당 이벤트", [ch("""
SELECT toStartOfHour(est_occurred_at) AS time, app_id, count() AS events
FROM core.events
WHERE env = '${env}' AND $__timeFilter(est_occurred_at)
GROUP BY time, app_id
ORDER BY time
""", fmt="timeseries")], 12, y, 12, 8, defaults=TS_DEFAULTS, options=LEGEND))
y += 8
fo.append(row("수집 파이프라인", y))
y += 1
fo.append(panel(
    "timeseries", "수락된 이벤트 / 분", [prom(
        'sum by (app) (rate(telemetry_events_accepted_total{env="$env"}[5m])) * 60', "{{app}}")],
    0, y, 8, 7, ds=PROM, desc="Prometheus(스크레이프 시각 기준). 백필은 보낸 순간에 몰려 보인다.", time_from="6h",
    defaults=TS_DEFAULTS, options=LEGEND))
fo.append(panel(
    "table", "검증 거부 · 코드별 (수집기 시작 이후)", [prom(
        'sum by (app, code) (telemetry_events_rejected_total{env="$env"})', "", instant=True)],
    8, y, 8, 7, ds=PROM, time_from="6h",
    desc="거부된 이벤트는 저장하지 않고 코드별 숫자만 남긴다. 수집기가 재시작되면 0부터 다시 센다. "
         "increase()를 쓰지 않는 이유: 처음 나타난 라벨 조합의 첫 증가분을 Prometheus가 보지 못해 "
         "드문 거부가 아예 안 보인다.",
    options={"sortBy": [{"displayName": "rejected", "desc": True}]},
    transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                     "renameByName": {"Value": "rejected"}}}],
    overrides=[{"matcher": {"id": "byName", "options": "rejected"}, "properties": [{"id": "decimals", "value": 0}]}]))
fo.append(panel(
    "timeseries", "스풀 대기 · 막힘", [
        prom("telemetry_spool_pending", "pending rows", "A"),
        prom("max(telemetry_flush_blocked) or vector(0)", "flush blocked (1 = 막힘)", "B")],
    16, y, 8, 7, ds=PROM, time_from="6h",
    desc="pending이 줄지 않고 blocked가 1이면 ClickHouse 쪽 문제다. 데이터는 스풀에 남아 있다.",
    defaults=TS_DEFAULTS, options=LEGEND))
y += 7
fo.append(panel(
    "table", "시각 품질 · 자동화 의심", [ch("""
SELECT
    app_id AS app,
    count() AS events,
    round(100 * countIf(timing_quality = 'skewed') / count(), 2) AS skewed_pct,
    round(100 * countIf(timing_quality = 'invalid') / count(), 2) AS invalid_pct,
    round(100 * countIf(automation_suspected) / count(), 2) AS automation_pct,
    round(quantile(0.5)(dateDiff('second', est_occurred_at, received_at)), 1) AS lag_p50_s,
    round(quantile(0.95)(dateDiff('second', est_occurred_at, received_at)), 1) AS lag_p95_s
FROM core.events
WHERE env = '${env}' AND $__timeFilter(est_occurred_at)
GROUP BY app_id
ORDER BY app_id
""")], 0, y, 24, 5,
    desc="skewed: 기기 시계가 5분 넘게 틀린 이벤트(시각은 보정됨). lag: 발생부터 수신까지. demo 백필은 며칠 단위로 보인다."))

factory = dashboard("telemetry-factory", "Telemetry · Factory Overview", fo, [ENV],
                    "앱 공장 전체의 제품 이벤트와 수집 파이프라인 상태")

# ------------------------------------------------------------------ App Product
_id[0] = 0
APP = var_query("app", "app", "SELECT app_id FROM core.apps ORDER BY app_id", current="maji")
FUNNEL = var_query("funnel", "funnel", "SELECT name FROM core.funnels WHERE app_id = '${app}' ORDER BY name")
WINDOW = var_query("funnel_window", "funnel window (ms)",
                   "SELECT window_ms FROM core.funnels WHERE app_id = '${app}' AND name = '${funnel}'", hide=2)
SCOPE = "app_id = '${app}' AND env = '${env}'"
y = 0
ap = []
for i, (title, sql, unit) in enumerate([
    ("actors", f"SELECT uniqExact(user.anonymous_id) FROM core.events WHERE {SCOPE} AND dataset = 'measure' AND $__timeFilter(est_occurred_at)", "short"),
    ("세션", f"SELECT uniqExact(session_id) FROM core.events_sessionized WHERE {SCOPE} AND dataset = 'measure' AND $__timeFilter(est_occurred_at)", "short"),
    ("이벤트", f"SELECT count() FROM core.events WHERE {SCOPE} AND $__timeFilter(est_occurred_at)", "short"),
    ("세션당 이벤트", f"SELECT round(count() / uniqExact(session_id), 1) FROM core.events_sessionized WHERE {SCOPE} AND dataset = 'measure' AND $__timeFilter(est_occurred_at)", "short"),
]):
    ap.append(panel("stat", title, [ch(sql)], i * 6, y, 6, 3, defaults={"unit": unit, "color": {"mode": "fixed", "fixedColor": "text"}},
                    options={"colorMode": "none", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"]}}))
y += 3
ap.append(panel(
    "timeseries", "일별 actor · 세션 (KST)", [ch(f"""
SELECT toStartOfDay(est_occurred_at, 'Asia/Seoul') AS time,
       uniqExact(user.anonymous_id) AS actors,
       uniqExact(session_id) AS sessions
FROM core.events_sessionized
WHERE {SCOPE} AND dataset = 'measure' AND $__timeFilter(est_occurred_at)
GROUP BY time
ORDER BY time
""", fmt="timeseries")], 0, y, 12, 8, desc="세션: 같은 익명 id의 이벤트 사이가 30분 이상 비면 새 세션(core.events_sessionized).",
    defaults=TS_DEFAULTS, options=LEGEND))
ap.append(panel(
    "bargauge", "퍼널 · ${funnel}", [ch(f"""
WITH (SELECT steps FROM core.funnels WHERE app_id = '${{app}}' AND name = '${{funnel}}') AS s
SELECT concat(toString(step), '. ', s[step]) AS step_name,
       countIf(level >= step) AS actors,
       round(100 * actors / max(countIf(level >= 1)) OVER (), 1) AS pct_of_first
FROM (
    SELECT windowFunnel(${{funnel_window}})(toUInt64(toUnixTimestamp64Milli(est_occurred_at)),
        event_name = s[1], event_name = s[2], event_name = s[3],
        event_name = s[4], event_name = s[5], event_name = s[6]) AS level
    FROM core.events
    WHERE {SCOPE} AND dataset = 'measure' AND $__timeFilter(est_occurred_at)
    GROUP BY user.anonymous_id
)
ARRAY JOIN range(1, length(s) + 1) AS step
GROUP BY step
ORDER BY step
""")], 12, y, 12, 8,
    desc="레지스트리(core.funnels)의 단계와 창. 첫 단계 이후 창 안에 순서대로 일어난 단계까지 센다(windowFunnel). "
         "telemetry verify가 같은 SQL로 정답과 대조한다.",
    defaults={"unit": "short", "min": 0, "color": {"mode": "fixed", "fixedColor": "blue"}},
    options={"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "valueMode": "text",
             "namePlacement": "left", "reduceOptions": {"values": True, "calcs": [], "fields": "/^actors$/"}},
    transformations=[{"id": "organize", "options": {"excludeByName": {"pct_of_first": True}}}]))
y += 8
ap.append(panel(
    "table", "리텐션 · 주간 코호트 (KST)", [ch(f"""
WITH days AS (
    SELECT user.anonymous_id AS a, toDate(est_occurred_at, 'Asia/Seoul') AS d
    FROM core.events
    WHERE {SCOPE} AND dataset = 'measure' AND isNotNull(user.anonymous_id)
    GROUP BY a, d
), firsts AS (
    SELECT a, min(d) AS first FROM days GROUP BY a
), today AS (
    SELECT toDate(now(), 'Asia/Seoul') AS t
)
SELECT toString(toMonday(first)) AS cohort_week,
       count() AS users,
       round(100 * countIf(first + 1 < (SELECT t FROM today) AND (a, first + 1) IN (SELECT a, d FROM days))
           / nullIf(countIf(first + 1 < (SELECT t FROM today)), 0), 1) AS d1_pct,
       round(100 * countIf(first + 7 < (SELECT t FROM today) AND (a, first + 7) IN (SELECT a, d FROM days))
           / nullIf(countIf(first + 7 < (SELECT t FROM today)), 0), 1) AS d7_pct,
       round(100 * countIf(first + 14 < (SELECT t FROM today) AND (a, first + 14) IN (SELECT a, d FROM days))
           / nullIf(countIf(first + 14 < (SELECT t FROM today)), 0), 1) AS d14_pct
FROM firsts
WHERE first >= toDate($__fromTime, 'Asia/Seoul') AND first <= toDate($__toTime, 'Asia/Seoul')
GROUP BY cohort_week
ORDER BY cohort_week
""")], 0, y, 12, 7,
    desc="Dn: 첫 방문 n일 뒤 그 날에 다시 온 비율. n일째가 아직 끝나지 않은 사용자는 분모에서 뺀다(빈 칸). "
         "session_only 앱은 익명 id가 방문마다 바뀌므로 항상 0이다.",
    defaults={"custom": {"cellOptions": {"type": "color-background", "mode": "basic"}}},
    overrides=[{"matcher": {"id": "byRegexp", "options": "d.*_pct"}, "properties": [
        {"id": "unit", "value": "percent"},
        {"id": "color", "value": {"mode": "continuous-blues"}},
        {"id": "min", "value": 0}, {"id": "max", "value": 60}]},
        {"matcher": {"id": "byName", "options": "users"}, "properties": [
            {"id": "custom.cellOptions", "value": {"type": "auto"}}]},
        {"matcher": {"id": "byName", "options": "cohort_week"}, "properties": [
            {"id": "custom.cellOptions", "value": {"type": "auto"}}]}]))
ap.append(panel(
    "timeseries", "오류 / 100 화면 · 일별", [ch(f"""
SELECT toStartOfDay(est_occurred_at, 'Asia/Seoul') AS time,
       release,
       round(100 * countIf(event_name = 'error.occurred') / nullIf(countIf(event_name = 'screen.viewed'), 0), 2) AS errors_per_100_views
FROM core.events
WHERE {SCOPE} AND dataset = 'measure' AND event_name IN ('error.occurred', 'screen.viewed')
  AND $__timeFilter(est_occurred_at)
GROUP BY time, release
ORDER BY time
""", fmt="timeseries")], 12, y, 12, 7, desc="릴리스별로 선을 나눴다. 새 릴리스에서 선이 뛰면 그 릴리스가 원인 후보다.",
    defaults=TS_DEFAULTS, options=LEGEND))
y += 7
ap.append(panel(
    "table", "릴리스 비교", [ch(f"""
SELECT release,
       min(est_occurred_at) AS first_seen,
       max(est_occurred_at) AS last_seen,
       uniqExact(user.anonymous_id) AS actors,
       countIf(event_name = 'screen.viewed') AS views,
       countIf(event_name = 'error.occurred') AS errors,
       round(100 * errors / nullIf(views, 0), 2) AS errors_per_100_views
FROM core.events
WHERE {SCOPE} AND dataset = 'measure' AND event_name IN ('screen.viewed', 'error.occurred')
  AND $__timeFilter(est_occurred_at)
GROUP BY release
ORDER BY first_seen
""")], 0, y, 12, 6, desc="화면·오류 이벤트(클라이언트)의 release(빌드 SHA) 기준. 서버 릴리스는 섞지 않는다. 기간이 다르니 비율로 비교한다."))
ap.append(panel(
    "table", "오류 fingerprint · 릴리스별", [ch(f"""
SELECT release,
       properties.fingerprint::String AS fingerprint,
       properties.kind::String AS kind,
       count() AS errors,
       uniqExact(user.anonymous_id) AS actors
FROM core.events
WHERE {SCOPE} AND event_name = 'error.occurred' AND $__timeFilter(est_occurred_at)
GROUP BY release, fingerprint, kind
ORDER BY errors DESC
LIMIT 20
""")], 12, y, 12, 6))
y += 6
ap.append(panel(
    "bargauge", "이벤트 구성", [ch(f"""
SELECT event_name, count() AS events
FROM core.events
WHERE {SCOPE} AND $__timeFilter(est_occurred_at)
GROUP BY event_name
ORDER BY events DESC
""")], 0, y, 8, 8, defaults={"unit": "short", "min": 0, "color": {"mode": "fixed", "fixedColor": "blue"}},
    options={"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "valueMode": "text",
             "namePlacement": "left", "reduceOptions": {"values": True, "calcs": [], "fields": "/^events$/"}}))
ap.append(panel(
    "table", "화면 Top", [ch(f"""
SELECT context.screen AS screen,
       count() AS views,
       uniqExact(user.anonymous_id) AS actors
FROM core.events
WHERE {SCOPE} AND event_name = 'screen.viewed' AND $__timeFilter(est_occurred_at)
GROUP BY screen
ORDER BY views DESC
LIMIT 15
""")], 8, y, 8, 8, desc="라우트 템플릿 단위. 실제 URL과 쿼리스트링은 수집기가 거부한다."))
ap.append(panel(
    "table", "유입 환경", [ch(f"""
SELECT ifNull(context.os, '-') AS os,
       ifNull(context.in_app, '-') AS in_app,
       ifNull(context.referrer_class, '-') AS referrer,
       uniqExact(user.anonymous_id) AS actors
FROM core.events
WHERE {SCOPE} AND dataset = 'measure' AND isNotNull(context.os) AND $__timeFilter(est_occurred_at)
GROUP BY os, in_app, referrer
ORDER BY actors DESC
LIMIT 15
""")], 16, y, 8, 8, desc="in_app: 카카오톡·토스·인스타그램 인앱 브라우저 여부."))
y += 8
ap.append(panel(
    "table", "이벤트 정의 · 타입 뷰", [ch("""
SELECT name AS typed_view, create_table_query LIKE '%account_id%' AS identified
FROM system.tables
WHERE database = 'agent' AND name LIKE concat('ev\\\\_', replaceAll('${app}', '-', '\\\\_'), '\\\\_%')
ORDER BY name
""")], 0, y, 24, 6,
    desc="레지스트리에서 생성된 이벤트·버전별 뷰. 속성이 타입이 있는 컬럼으로 풀려 있다. "
         "예: SELECT template, count() FROM agent.ev_maji_card_published_v2 GROUP BY template"))

app = dashboard("telemetry-app", "Telemetry · App Product", ap, [ENV, APP, FUNNEL, WINDOW],
                "앱 하나의 사용 흐름: 활성, 퍼널, 리텐션, 릴리스별 오류")


def configmap(dashboards):
    out = ["# Generated by telemetry-dashboards.py in this directory: edit the script, then",
           "# `python3 telemetry-dashboards.py > configmap-dashboard-telemetry.yaml`.",
           "# Panel queries read registry views (core.apps, core.funnels) instead of hard-coding apps.",
           "apiVersion: v1", "kind: ConfigMap", "metadata:", "    name: grafana-dashboard-telemetry",
           "    namespace: mgmt01", "    labels:", "        app: grafana", "data:"]
    for name, d in dashboards:
        out.append(f"    {name}: |")
        for line in json.dumps(d, ensure_ascii=False, indent=4).splitlines():
            out.append("        " + line)
    return "\n".join(out) + "\n"


sys.stdout.write(configmap([("telemetry-factory.json", factory), ("telemetry-app.json", app)]))
