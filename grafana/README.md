# grafana

Dashboards for the metrics this repo exports (via configurable-exporter), plus
a script that keeps every plant's copy in step with the shared templates.

```
grafana/
  plants.example.json          format of the plant list (copy to plants.json)
  plants.json                  YOUR plant list — git-ignored, never commit it
  deploy_dashboards.py         export templates / deploy them to every plant (stdlib only)
  dashboards/sunpower.json     template: the universal Sunpower dashboard
  dashboards/management.json   template: the Management page (Solis and Deye share it)
```

## The dashboards

**Sunpower-&lt;plant&gt;**: one per plant, a copy of the universal Sunpower
dashboard. In the universal dashboard `instance_id` is a dropdown
(`label_values(instance_id)`); in a plant's copy it's a hidden constant, so a
customer only ever sees their own plant.

**Management-&lt;plant&gt;**: only for externally managed plants. It's a single
HTML text panel that talks to the plant's solar-management API through a
Grafana JSON API datasource (`const BASE = "/api/datasources/proxy/uid/<uid>"`).
The template stores that uid as `__PLANT_API_DATASOURCE_UID__`; the script fills
in the datasource the plant list names for the plant (`api_datasource`).

The Prometheus datasource is stored as `__PROMETHEUS_DATASOURCE_UID__` and
resolved by name (`prometheus_datasource`) in the target Grafana, so the same
templates work on any Grafana.

## Plant list

```bash
cp plants.example.json plants.json     # then fill in your Grafana and plants
```

`plants*.json` is git-ignored (except the example): a plant list names
customers and plants. Keep one file per Grafana and pick it with `--config`.

Per plant: `instance_id` (must match the exporter's `instance_id:`), `name`
(used in titles), `sunpower_uid`, optionally `sunpower_title`; for an
externally managed plant also `management_uid` and `api_datasource`.

## Workflow

1. Change the dashboard in Grafana, on the universal Sunpower dashboard (or
   on the Management dashboard named as the template's `source_uid`).
2. Pull it into the repo and commit:
   ```bash
   ./deploy_dashboards.py --token-file <token file> export
   git diff dashboards/
   ```
   `export` drops everything that names a plant (dashboard title, the
   last-selected `instance_id`, datasource uids), so the templates are safe to
   commit.
3. See what a deploy would change, then deploy:
   ```bash
   ./deploy_dashboards.py --token-file <token file> deploy                   # dry run (default)
   ./deploy_dashboards.py --token-file <token file> deploy --apply
   ./deploy_dashboards.py --token-file <token file> deploy --plant PLANT-A --apply
   ./deploy_dashboards.py --config plants-other.json --url http://localhost:3000 \
       --token-file <token file> deploy                                       # another Grafana
   ```

The token can also come from `GRAFANA_TOKEN`; the URL defaults to the config's
`grafana_url` (override with `--url` or `GRAFANA_URL`). The token needs
Editor/Admin rights on the dashboards and read access to datasources.

## Customer access is never touched

An existing dashboard is only **updated in place**: same uid, same id, same
folder, saved against its current version (`overwrite: false`, so a concurrent
edit fails instead of being silently overwritten). Grafana stores dashboard
permissions (the customers' *View* access) by uid, separately from the JSON, so
they survive every deploy. Never delete and re-import a plant's dashboard; that
would drop its permissions.

A plant whose dashboard doesn't exist yet is skipped unless you pass
`--create`. The new dashboard then has **no customer permissions**: set them in
Grafana (Dashboard → Settings → Permissions).

## What a deploy replaces

Everything in a plant's copy except uid/id/version/folder and the
`instance_id` constant comes from the template, including panels, time range
and refresh. Panel tweaks made directly on a plant's copy are overwritten;
make them on the universal dashboard instead. The dry run lists, per
dashboard, how many panels change, appear or disappear.
