# bao_create_secret

Pipeline-owned role (Class C; not in oc-cfg's write scope). The only controller-side action a catalogue role may
use (lint gate). Proposal: infra `docs/proposals/homelab-apps/5-pipeline-classes.md` 5.1, `5b-security.md`.

```yaml
- name: Admin password, once
  when: not _hash_file.stat.exists           # the caller keeps its own marker on the guest
  ansible.builtin.include_role:
    name: bao_create_secret
  vars:
    path: "<zone>/secret/apps/adguard/<host>" # exactly as listed in catalog.secrets of this app
    keys: [username, password]               # exactly the catalogue keys
    data: {username: admin}
    generate: {key: password, length: 28, chars: [ascii_letters, digits]}
    hash: bcrypt
# then: bao_create_secret_result.hash (status created) -> write it to the guest marker file
```

| Input | Rule |
|---|---|
| `path` | must equal one `catalog.secrets[].path` of the app being deployed; `<zone>`/`<host>` are filled in by the script from the infra spec of `inventory_hostname`; result must be under `<zone>/secret/apps/<app>/` |
| `keys` | the catalogue keys of that entry, all of them |
| `data` | fixed string values (e.g. a username, or k3s tokens read from the guest for escrow) |
| `generate` | `{key, length 16..128, chars}`; chars from `ascii_letters`, `ascii_lowercase`, `ascii_uppercase`, `digits`, `hexdigits`, `punctuation` (`-_.,:;+=@%`) |
| `hash` | `bcrypt` or omitted; hashes the generated value; needs `bcrypt` in `/opt/ansible-apps/venv` |

Result `bao_create_secret_result`: `{status, path, hash}`. `status`: `created`, `exists` (KV answered check-and-set
mismatch), or `check` (check mode: nothing runs). Plaintext never leaves the script.

Trust: the script (`files/bao_create_secret.py`, run on CT 300 as `runner-apps-play`) finds its run dir from its own
location and takes the app, zone, zone mode and child token from it (`inventory.yml`, `hosts`, infra specs, `env`,
`token`). Caller vars and `environment:` cannot change the app, zone, address or token. The per-app child token is
create-only on that app's paths, so OpenBao enforces the same limit server-side.

**Existing secret:** the child token has `create` only, so OpenBao answers **403** for a path that already holds a
secret (the existence check makes the write an update). The role fails then instead of passing silently: the guest
marker is gone but the value is stored, so a human decides (read it from OpenBao or delete it, then re-run).
Tests: `scripts/tests/test_bao_create_secret.py`.
