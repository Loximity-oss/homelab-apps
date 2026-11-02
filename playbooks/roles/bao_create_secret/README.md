# playbooks/roles/bao_create_secret

This role creates one secret in OpenBao for a catalogue application. The pipeline owns the role.

## Contents

| File | Purpose |
|---|---|
| `tasks/main.yml` | Calls the script |
| `files/bao_create_secret.py` | Runs on `hm1-shr-cicd01` as `runner-apps-play` and writes the secret |

Tests: `scripts/tests/test_bao_create_secret.py`.

## Use

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
```

After the run, write `bao_create_secret_result.hash` to the marker file on the guest.

| Input | Rule |
|---|---|
| `path` | Must equal one `catalog.secrets[].path` of the application. The script fills in `<zone>` and `<host>` from the guest spec. The result must be under `<zone>/secret/apps/<app>/` |
| `keys` | All the catalogue keys of that entry |
| `data` | Fixed string values, for example a user name |
| `generate` | `{key, length, chars}`. The length is from 16 to 128. Allowed `chars`: `ascii_letters`, `ascii_lowercase`, `ascii_uppercase`, `digits`, `hexdigits`, `punctuation` |
| `hash` | `bcrypt` or no value. It hashes the generated value. It needs `bcrypt` in `/opt/ansible-apps/venv` |

The result `bao_create_secret_result` is `{status, path, hash}`. The status is one of these:
- `created`: the script wrote the secret.
- `exists`: OpenBao answered a check-and-set mismatch.
- `check`: the run is in check mode and nothing ran.

The plaintext never leaves the script.

## Trust

The script finds its run directory from its own location. It reads the application, the zone, and the child token from that directory. A variable of the caller cannot change them. The child token can only create secrets on the paths of that application. OpenBao enforces the same limit.

Caution: the role is Class C. It is not in the write scope of `oc-cfg`.

Caution: if the secret already exists, OpenBao answers 403 and the role fails. The marker on the guest is gone, but the value is stored. A person reads the value from OpenBao or deletes it, then runs the deploy again.

Back to the root README: [../../../README.md](../../../README.md)
