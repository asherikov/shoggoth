# ansible

Implementation of the shoggoth deployment.

Requirements:

```sh
pipx install "ansible-core>=2.22.0"    # ansible-core itself, in a venv of its own
pipx inject ansible-core kubernetes    # the Python client the collection's modules import
ansible-galaxy collection install -r ansible/requirements.yml
```

## Run it

```sh
export ANSIBLE_CONFIG=$PWD/ansible/ansible.cfg
export KUBECONFIG=$PWD/private/kubeconfig

ansible-playbook ansible/deploy.yml --limit shoggoth          # the deploy, three passes
ansible-playbook ansible/kubeconfig.yml --limit shoggoth      # fetch the kubeconfig
ansible-playbook ansible/service.yml --limit shoggoth \       # enable|disable
  -e shoggoth_service=gitea -e shoggoth_service_action=enable
ansible-playbook ansible/down.yml --limit shoggoth            # destructive, and unguarded
ansible-playbook ansible/purge.yml --limit shoggoth-test      # destructive — asserts the guard first
ansible-playbook ansible/drop_image_tag.yml --limit shoggoth \  # drop one tag
  -e shoggoth_image=library/nginx -e shoggoth_tag=latest
ansible-playbook ansible/clear_endpoints.yml --limit shoggoth \  # host-level: stops k3s
  -e 'shoggoth_k3s_stale_ips=192.0.2.111' --ask-become-pass
```

The control node needs pipx on PATH, ansible-core 2.22.0 or newer (an older one
cannot run a module on a host whose `python3` is 3.15 — see "Known issues"), and
the `kubernetes` client inside the ansible-core venv: `ansible-galaxy collection
install` installs collections, never their Python requirements, and a
`pip install --user kubernetes` lands in site-packages the venv never reads.
Verify with `pipx runpip ansible-core list | grep kubernetes`.

`down.yml` is the one destructive play with **no guard**; `purge.yml` asserts the
primary instance first. `shoggoth` in `--limit` is the inventory group name, and
`-e ansible_host=<ip>` outranks the `HOST_IP` default in `group_vars`.

Three plays — `deploy.yml`, `kubeconfig.yml` and `purge.yml` — run tasks as root
on the host over an ssh session with no tty, where `sudo -n` cannot prompt, so
they pass `--ask-become-pass` and collect the password once for the whole play.
Each spells the flag out because ansible-core asks as soon as it is set, before
it parses a play. A NOPASSWD host answers with an empty line and sudo stays at
`-H -S -n`.

Everything else is delegated to the control node, the `host_state` role's
content sync included: the role hands the instance's storage directory and its
three content directories to the login user and makes the shared parent
searchable, so the transfer needs no privilege (the mechanism is in the role).

`deploy.yml` needs a kubeconfig it cannot fetch: reading one from the host needs
the sudo password it is collecting for something else, and `private/` is not in
git. Without one, client-go falls back to `http://localhost:8080`, so the play
checks for a current context first.

There is no host-OS play; the host is configured by the NixOS module
(`host_install_nixos`) — see "The host-OS contract" below.

### The dry run is not a rehearsal

`ansible-playbook --check` walks every playbook — `deploy.yml`, `down.yml`,
`purge.yml` (skipped for the primary instance), `kubeconfig.yml`, `service.yml`
for both actions, and `drop_image_tag.yml` against a tag that does not exist.
It needs a reachable host, the become password and the tunnel.

**Check mode skips `command` tasks**, which is most of the deploy. The kubeconfig
probe and the CRD wait are forced back in with `check_mode: false` because they
only read and are the two that most often fail; the namespace-label strip is left
skipped, because a check that relabels a namespace is not a check. What runs is
the `k8s_info`, the `include_vars`, the `debug` tasks and the `host_state` role,
where `synchronize` adds `--dry-run --delete-after` and really does contact the
host. So it exercises parsing, inventory and variable resolution — and a chart
change whose `helm upgrade` would abort passes it clean.

`ansible_lint` is the offline guard: no cluster, no host, no collections beyond
ansible-core, and it evaluates the inventory and group_vars expressions a
`--syntax-check` never runs.

Hand-invoking a playbook needs two things the snippet below spells out:
`ansible.cfg` is looked up in `$PWD`, `~/.ansible.cfg` and
`/etc/ansible/ansible.cfg` — **not** next to the playbook — and ignored silently
otherwise (`config file = None`, then "no inventory was parsed", then "could not
match supplied host pattern"); and `kubectl`/`helm`/`k8s_info` read `KUBECONFIG`
from the environment:

```sh
export ANSIBLE_CONFIG=$PWD/ansible/ansible.cfg
export KUBECONFIG=$PWD/private/kubeconfig
ansible-playbook ansible/deploy.yml --limit shoggoth
```

## The plays

### `deploy.yml`

The whole deploy, three passes, one invocation

### `kubeconfig.yml`

Fetch the cluster kubeconfig, `slurp` + delegated `copy`, no redirect.

### `service.yml`

Flip one service toggle, one play, two actions.

The two silent module traps — `reuse_values` is ignored unless
`reset_values: false` is set with it, and `set_values[].value` is concatenated as
`"--set " + value` so the booleans are quoted strings — are documented in the
play's header. No idempotency gain: the module reports `changed: true` whether or
not anything differs.

### `down.yml`

One play, three rungs — uninstall (tolerating failure), strip the finalizers off
whatever blocks it, uninstall again without hooks and fail hard, then purge the
orphaned ESO objects. Why three rungs, and the two Jinja traps in them, are in
the play's header.

One invariant is in neither: the orphan purge is gated on
`shoggoth_down_rendered | length > 0` because a render with no ESO objects makes
every live one an orphan by the diff, and the unguarded purge would delete the
release's own secrets on the way out.

### `purge.yml`

The one play with a guard: it refuses the primary instance before anything is
deleted. Namespace (`wait`, `wait_timeout: 120`), cluster-scoped RBAC
(`delete_all` + `label_selectors`) and the host storage tree
(`file: state=absent`) — the last is the step the play is for. No `helm
uninstall`, deliberately; the header says why. The original's `-`-prefix
tolerance is `ignore_errors: true`, which still prints the failure.

### `drop_image_tag.yml`

The registry answers on the host's own port (hostNetwork), so every task is
delegated to localhost. `uri`'s `status_code` lists 404 beside 200/202/204,
making "tag not present" an accepted outcome rather than a branch — both in the
play's header.

Two targets went instead of being ported: `crds_apply`, whose apply is now Pass 1
of `deploy.yml` (and whose second spelling of the CRD set had already drifted from
`group_vars/instances.yml`, now the only one), and `sync`, whose rsync had no
reader on the host — the role that did real work is Pass 0 of `deploy.yml`.

### `clear_endpoints.yml`

The one play that writes to the host rather than the cluster, and the one that
takes the cluster down while it works: it stops k3s, appends kine's own delete row
for each named address's master lease, verifies, and starts k3s again. The
Endpoints object is never touched, so the reconciler rewrites it in place and
there is no window in which the service has no endpoints. The kine mechanism, the
`block`/`rescue`/`always` shape, the argv-only staging and the reader fallback are
all specified in the play's header.

Every task runs on the host: the unit, the datastore and the sidecar files
exist only there, and both `[instances]` entries point at one machine, so
`--limit` only selects connection vars.

The value is quoted on the command line — `-e 'shoggoth_k3s_stale_ips=<ip> …'` —
because it is a list, and an unquoted `-e` hands Ansible a bare second argument
instead of one value containing a space.

## The host-OS contract

The host is not configured by ansible yet.

## The three decisions that make the later migration cheap

1. **Instance = inventory group, not an extra-var.** `inventory_hostname` is the
   instance and `--limit` selects on it; parameterising with `-e instance=…`
   would mean restructuring the inventory when host-OS configuration arrives.

2. **One source of truth for config.** `deploy.yml` loads
   `shoggoth/<instance>.yaml` with `include_vars` — the same file the deployment
   passes to helm with `--values` and reads with yq, so the inventory and the rest
   of the deployment read one file. There is deliberately no `host_vars/`: a
   per-instance value belongs in the config file, and copying a port into a
   host_vars file is how the two drift apart. Per-host vars start earning their
   keep when the cutover plan's `[shoggoth_hosts]` group arrives.

3. **Nothing in this tree writes `/etc`.** The host-OS contract stays in
   `shoggoth.nix` + `ports.nix` while the module owns those paths (see "The
   host-OS contract" above). A `roles/` entry that writes `/etc/rancher`,
   `/etc/nixos` or `/etc/systemd` is the signal the cutover has started, and it
   must land together with retiring the module, not before.

