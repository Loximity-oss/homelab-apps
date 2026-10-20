# <App> - runbook

Template; copy to catalog/<app>/docs/runbook.md.

## Check
- `systemctl is-active docker`; `docker compose -f /opt/example/compose.yaml ps`
- `curl -fsS http://127.0.0.1:8080/`

## Restart
- `docker compose -f /opt/example/compose.yaml up -d --force-recreate`

## Upgrade / rollback
- Change `version.pin` in app.yaml (must be in `version.known`) by PR; roll back by reverting the PR.

## Logs
- `docker compose -f /opt/example/compose.yaml logs --tail 100`
