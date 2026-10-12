# grafana

Dashboards for the metrics this repo exports (via configurable-exporter), plus
a script that keeps every customer's copy in step with the shared templates on
grafana.hvbatt.com.

```
grafana/
  plants.json                  which plants get which dashboards (instance_id → uid/title)
  deploy_dashboards.py         export templates / deploy them to every plant (stdlib only)
  dashboards/sunpower.json     template: admin-Sunpower-universal
  dashboards/management.json   template: the Management page (Solis and Deye share it)
```

## The dashboards

**Sunpower-&lt;plant&gt;**: one per plant, a copy of *admin-Sunpower-universal*.
In the universal dashboard `instance_id` is a dropdown (`label_values(instance_id)`);
in a plant's copy it's a hidden constant (`GS48`, `Krumovi`, …), so the customer
only ever sees their own plant.

**Management-&lt;plant&gt;**: only for plants that are externally managed (today
GS48 and Zona-28). It's a single HTML text panel that talks to the plant's
solar-management API through a Grafana JSON API datasource
(`const BASE = "/api/datasources/proxy/uid/<uid>"`). The template stores that
uid as `__PLANT_API_DATASOURCE_UID__`; the script fills in the datasource that
`plants.json` names for the plant (`Solis-GS48-API` → `http://10.100.2.1:5000/`, …).

## Workflow

1. Change the dashboard in Grafana, on *admin-Sunpower-universal* (or on
   *Management-GS48* for the management page).
2. Pull it into the repo and commit:
   ```bash
   ./deploy_dashboards.py --token-file ~/grafana-test/grafana_claude_token export
   git diff dashboards/
   ```
3. See what a deploy would change, then deploy:
   ```bash
   ./deploy_dashboards.py --token-file … deploy                 # dry run (default)
   ./deploy_dashboards.py --token-file … deploy --apply
   ./deploy_dashboards.py --token-file … deploy --plant GS48 --apply
   ```

The token can also come from `GRAFANA_TOKEN`; the URL defaults to `plants.json`
(override with `--url` or `GRAFANA_URL`). The token needs Editor/Admin rights on
the dashboards, and read access to datasources for the management lookup.

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

## Adding a plant

Add an entry to `plants.json`:

```json
{ "instance_id": "Plant-X", "name": "Plant-X", "sunpower_uid": "sunpower-plant-x" }
```

`instance_id` must match the exporter's `instance_id:` (and solar-deploy's
inventory). For an externally managed plant, also add `"management_uid"` and
`"api_datasource"`, and create that JSON API datasource in Grafana first. Then
run `deploy --plant Plant-X --create --apply` and set the permissions.

## What a deploy replaces

Everything in a plant's copy except uid/id/version/folder and the
`instance_id` constant comes from the template, including panels, time range
and refresh. Panel tweaks made directly on a plant's copy are overwritten;
make them on the universal dashboard instead. The dry run lists, per
dashboard, how many panels change, appear or disappear.
