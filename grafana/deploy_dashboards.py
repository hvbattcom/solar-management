#!/usr/bin/env python3
"""Keep the per-plant Grafana dashboards in step with the shared templates.

    ./deploy_dashboards.py export                 # Grafana -> dashboards/*.json
    ./deploy_dashboards.py deploy                 # dry run: what would change
    ./deploy_dashboards.py deploy --apply         # write it
    ./deploy_dashboards.py deploy --plant PLANT-A --apply

Templates
  dashboards/sunpower.json    admin-Sunpower-universal (instance_id is a query
                              variable there; per plant it becomes a hidden
                              constant)
  dashboards/management.json  the Management panel; its API base points at the
                              plant's JSON-API datasource, stored here as
                              __PLANT_API_DATASOURCE_UID__
  The Prometheus datasource uid is stored as __PROMETHEUS_DATASOURCE_UID__ and
  resolved by name (config "prometheus_datasource") in the target Grafana, so
  the same templates deploy to other Grafanas too (--config <file>).

An existing dashboard is only ever *updated in place*: same uid, same id,
same folder, saved against its current version. Grafana keeps dashboard
permissions (the customers' View access) by uid, outside the JSON, so they
survive. A dashboard that does not exist yet is created only with --create;
the script then removes the Viewer-role permission Grafana adds by default
(every customer is a Viewer), and the plant's user still needs View by hand.

Plant lists (plants.json, or any --config file) name customers and plants, so
they are git-ignored; plants.example.json shows the format.

Auth: GRAFANA_TOKEN env var, or --token-file (service-account token with
Editor/Admin on the dashboards). Only the Python standard library is used.
"""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLACEHOLDER = "__PLANT_API_DATASOURCE_UID__"
PROM_PLACEHOLDER = "__PROMETHEUS_DATASOURCE_UID__"
BASE_RE = re.compile(r'(const BASE = "/api/datasources/proxy/uid/)([^"]+)(")')


# ── Grafana API ───────────────────────────────────────────────────────────────

class Grafana:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, path: str, method: str = "GET", body=None):
        req = urllib.request.Request(
            self.url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None

    def dashboard(self, uid: str):
        """(dashboard, meta) or (None, None) when it does not exist."""
        status, data = self.call(f"/api/dashboards/uid/{urllib.parse.quote(uid)}")
        if status == 404:
            return None, None
        if status != 200:
            sys.exit(f"GET dashboard {uid}: HTTP {status} {data}")
        return data["dashboard"], data["meta"]

    def datasource_uid(self, name: str):
        status, data = self.call(f"/api/datasources/name/{urllib.parse.quote(name)}")
        return data["uid"] if status == 200 else None

    def save(self, dashboard: dict, folder_uid: str, message: str):
        status, data = self.call("/api/dashboards/db", "POST", {
            "dashboard": dashboard, "folderUid": folder_uid,
            "overwrite": False, "message": message})
        if status != 200:
            sys.exit(f"save {dashboard.get('uid')}: HTTP {status} {data}")
        return data

    def revoke_viewer_role(self, uid: str) -> None:
        """Drop the Viewer-role permission Grafana adds to a new dashboard. Every
        customer account is a Viewer, so with it each customer would see every
        plant's new dashboard."""
        status, data = self.call(
            f"/api/access-control/dashboards/{urllib.parse.quote(uid)}/builtInRoles/Viewer",
            "POST", {"permission": ""})
        if status != 200:
            sys.exit(f"{uid}: created, but removing the Viewer-role permission failed "
                     f"(HTTP {status} {data}) — remove it by hand NOW: every customer can see it")


# ── Templates ─────────────────────────────────────────────────────────────────

def strip_identity(db: dict, title: str) -> dict:
    """Template form: no id/uid/version and nothing that names a plant -- the
    title and the last-selected instance_id are dropped (deploy sets both)."""
    db = copy.deepcopy(db)
    for k in ("id", "uid", "version"):
        db.pop(k, None)
    db["title"] = title
    for v in db.get("templating", {}).get("list", []):
        if v.get("name") == "instance_id":
            v["current"] = {}
            v.pop("options", None)
    return db


def swap_uid(db: dict, old: str, new: str) -> dict:
    """Replace a datasource uid everywhere in a dashboard (JSON round trip)."""
    return json.loads(json.dumps(db).replace(f'"{old}"', f'"{new}"'))


def build_sunpower(tpl: dict, plant: dict, title: str) -> dict:
    db = copy.deepcopy(tpl)
    db["title"] = title
    found = False
    for v in db.get("templating", {}).get("list", []):
        if v.get("name") == "instance_id":
            iid = plant["instance_id"]
            v.clear()
            # same shape as the hand-made per-plant copies, so a deploy only
            # touches what really changed
            v.update({"current": {"text": iid, "value": iid}, "hide": 2,
                      "label": "InstanceID", "name": "instance_id", "query": iid,
                      "skipUrlSync": True, "type": "constant"})
            found = True
    if not found:
        sys.exit("sunpower template has no instance_id variable")
    return db


def build_management(tpl: dict, title: str, ds_uid: str) -> dict:
    db = copy.deepcopy(tpl)
    db["title"] = title
    n = 0
    for p in db.get("panels", []):
        content = (p.get("options") or {}).get("content")
        if content and PLACEHOLDER in content:
            p["options"]["content"] = content.replace(PLACEHOLDER, ds_uid)
            n += 1
    if n == 0:
        sys.exit(f"management template has no {PLACEHOLDER}")
    return db


# ── Diff (dry run) ────────────────────────────────────────────────────────────

def describe_changes(old: dict, new: dict) -> list[str]:
    ignore = {"id", "uid", "version", "panels", "templating"}
    out = []
    oldp = {p.get("id"): p for p in old.get("panels", [])}
    newp = {p.get("id"): p for p in new.get("panels", [])}
    changed = [i for i in newp if i in oldp and oldp[i] != newp[i]]
    added = [i for i in newp if i not in oldp]
    removed = [i for i in oldp if i not in newp]
    if changed or added or removed:
        out.append(f"panels: {len(changed)} changed, {len(added)} added, {len(removed)} removed")
    if old.get("templating") != new.get("templating"):
        names = lambda d: [(v.get("name"), v.get("type")) for v in d.get("templating", {}).get("list", [])]
        out.append(f"variables: {names(old)} -> {names(new)}")
    for k in sorted(set(old) | set(new)):
        if k not in ignore and old.get(k) != new.get(k):
            out.append(f"{k}: changed")
    return out


# ── Commands ──────────────────────────────────────────────────────────────────

def load_cfg(path: str = "plants.json") -> dict:
    p = Path(path)
    p = p if p.is_absolute() else HERE / p
    if not p.exists():
        sys.exit(f"{p.name} not found — copy plants.example.json to {p.name} and fill in your "
                 "plants (it is git-ignored on purpose)")
    return json.loads(p.read_text())


def prometheus_uid(g: Grafana, cfg: dict) -> str:
    name = cfg.get("prometheus_datasource", "prometheus")
    uid = g.datasource_uid(name)
    if not uid:
        sys.exit(f"Prometheus datasource '{name}' not found in {g.url}")
    return uid


def git_rev() -> str:
    try:
        return subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def cmd_export(g: Grafana, cfg: dict, _args) -> None:
    t = cfg["templates"]
    db, _ = g.dashboard(t["sunpower"]["source_uid"])
    if db is None:
        sys.exit("sunpower source dashboard not found")
    prom = prometheus_uid(g, cfg)
    path = HERE / t["sunpower"]["file"]
    path.write_text(json.dumps(swap_uid(strip_identity(db, "Sunpower template"), prom, PROM_PLACEHOLDER),
                               indent=2, ensure_ascii=False) + "\n")
    print(f"exported {db['title']} -> {path.relative_to(HERE)} (prometheus uid -> {PROM_PLACEHOLDER})")

    db, _ = g.dashboard(t["management"]["source_uid"])
    if db is None:
        sys.exit("management source dashboard not found")
    db = strip_identity(db, "Management template")
    n = 0
    for p in db.get("panels", []):
        content = (p.get("options") or {}).get("content") or ""
        new, k = BASE_RE.subn(rf"\g<1>{PLACEHOLDER}\g<3>", content)
        if k:
            p["options"]["content"], n = new, n + k
    if n != 1:
        sys.exit(f"expected exactly one 'const BASE = ...' line in the management panel, found {n}")
    path = HERE / t["management"]["file"]
    path.write_text(json.dumps(swap_uid(db, prom, PROM_PLACEHOLDER), indent=2, ensure_ascii=False) + "\n")
    print(f"exported management template -> {path.relative_to(HERE)} (datasource uid -> {PLACEHOLDER})")


def cmd_deploy(g: Grafana, cfg: dict, args) -> None:
    t = cfg["templates"]
    prom = prometheus_uid(g, cfg)
    tpl_sp = swap_uid(json.loads((HERE / t["sunpower"]["file"]).read_text()), PROM_PLACEHOLDER, prom)
    tpl_mg = swap_uid(json.loads((HERE / t["management"]["file"]).read_text()), PROM_PLACEHOLDER, prom)
    plants = cfg["plants"]
    if args.plant:
        wanted = set(args.plant)
        plants = [p for p in plants if p["instance_id"] in wanted or p["name"] in wanted]
        missing = wanted - {p["instance_id"] for p in plants} - {p["name"] for p in plants}
        if missing:
            sys.exit(f"not in plants.json: {', '.join(sorted(missing))}")

    msg = f"deployed by solar-management grafana/deploy_dashboards.py @ {git_rev()}"
    jobs = []
    for p in plants:
        title = p.get("sunpower_title") or t["sunpower"]["title"].format(**p)
        jobs.append((p, p.get("sunpower_uid") or f"sunpower-{p['name'].lower()}",
                     build_sunpower(tpl_sp, p, title)))
        if p.get("api_datasource"):
            ds_uid = g.datasource_uid(p["api_datasource"])
            if not ds_uid:
                sys.exit(f"{p['name']}: datasource '{p['api_datasource']}' not found in Grafana — "
                         "create it first (JSON API datasource, URL http://<plant wg ip>:5000/)")
            title = t["management"]["title"].format(**p)
            jobs.append((p, p.get("management_uid") or f"management-{p['name'].lower()}",
                         build_management(tpl_mg, title, ds_uid)))

    for p, uid, new in jobs:
        old, meta = g.dashboard(uid)
        label = f"{new['title']:<24} uid={uid}"
        if old is None:
            if not args.create:
                print(f"SKIP   {label}: does not exist (use --create; then set its permissions by hand)")
                continue
            new.update({"uid": uid, "id": None})
            if args.apply:
                g.save(new, cfg.get("new_folder_uid", ""), msg)
                g.revoke_viewer_role(uid)
                print(f"CREATE {label}: created, Viewer role removed — grant the plant's user View in Grafana")
            else:
                print(f"CREATE {label}: would be created (private: no Viewer-role access)")
            continue

        new.update({"uid": uid, "id": old["id"], "version": old["version"]})
        changes = describe_changes(old, new)
        if old.get("title") != new["title"]:
            changes.insert(0, f"title: {old.get('title')!r} -> {new['title']!r}")
        if not changes:
            print(f"OK     {label}: already up to date")
            continue
        if args.apply:
            r = g.save(new, meta.get("folderUid", ""), msg)
            print(f"UPDATE {label}: v{old['version']} -> v{r.get('version')}  ({'; '.join(changes)})")
        else:
            print(f"UPDATE {label}: would change v{old['version']}  ({'; '.join(changes)})")

    if not args.apply:
        print("\ndry run — nothing written; add --apply to deploy")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="plants.json",
                    help="plant list, git-ignored (default plants.json; see plants.example.json)")
    ap.add_argument("--url", help="Grafana base URL (default: GRAFANA_URL or the config's grafana_url)")
    ap.add_argument("--token-file", help="file holding the API token (default: GRAFANA_TOKEN env var)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export", help="pull the template dashboards from Grafana into dashboards/")
    d = sub.add_parser("deploy", help="update the per-plant dashboards from the templates")
    d.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    d.add_argument("--create", action="store_true", help="also create dashboards that do not exist yet")
    d.add_argument("--plant", action="append", help="only this instance_id or name (repeatable)")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    token = (Path(args.token_file).expanduser().read_text().strip() if args.token_file
             else os.environ.get("GRAFANA_TOKEN", ""))
    if not token:
        sys.exit("no token: set GRAFANA_TOKEN or pass --token-file")
    g = Grafana(args.url or os.environ.get("GRAFANA_URL") or cfg["grafana_url"], token)
    {"export": cmd_export, "deploy": cmd_deploy}[args.cmd](g, cfg, args)


if __name__ == "__main__":
    main()
