# ansible

Implementation of the shoggoth deployment.

Requirements: docker. ansible-core, the two collections in `requirements.yml`
and the `kubernetes` Python client their modules import are baked into the
`client` stage of `shoggoth/dockerfiles/tools`, and every make target runs
`ansible-playbook` inside that image — see "By hand" below for the
`docker run` the make targets spell out, and `$(ANSIBLE)` in the Makefile
for the single definition they all share.

## Run it

```sh
make up                # the deploy, three passes
make down              # destructive, and unguarded
make purge             # destructive — asserts the guard first
make start SERVICE=gitea
make stop SERVICE=gitea
make control_setup     # fetch the kubeconfig
make host_install_nixos  # render the NixOS host config and deploy it (host-level)
make drop_image_tag IMAGE=library/nginx TAG=latest
make k3s_clear_endpoints K3S_STALE_IPS=192.0.2.111   # host-level: stops k3s
make ansible_lint      # offline: syntax, inventory, group_vars
make ansible_check     # --check --diff over every play, needs the host
```

`make up`, `make purge`, `make host_install_nixos` and `make k3s_clear_endpoints`
prompt for the become password, because they pass `--ask-become-pass`; the
container is started with `-it` so the prompt has a terminal to read from.

`down.yml` is the one destructive play with **no guard**; `purge.yml` asserts the
primary instance first. `shoggoth` in `--limit` is the inventory group name, and
`-e ansible_host=<ip>` outranks the `HOST_IP` default in `group_vars`.

Four plays — `host.yml`, `deploy.yml`, `purge.yml` and `clear_endpoints.yml` —
run tasks as root on the host over an ssh session with no tty, where `sudo -n`
cannot prompt, so they pass `--ask-become-pass` and collect the password once
for the whole play.
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

The host is configured by the NixOS module, and `host.yml` is the play that
renders and deploys it (`host_install_nixos`) — see "The host-OS contract"
below.

### The dry run is not a rehearsal

`ansible-playbook --check` walks every playbook the make targets invoke —
`deploy.yml`, `down.yml`, `purge.yml` (skipped for the primary instance),
`service.yml` for every service key in both actions, `drop_image_tag.yml`
against a tag that does not exist, and `clear_endpoints.yml` against a
documentation address. `host.yml` is skipped for the same shape of reason: its
import gate refuses a host whose `configuration.nix` imports nothing yet — the
state the play's own first pass leaves — so the run would stop on that guard
rather than on anything the target is looking for. It needs a reachable host,
the become password and a loaded agent key:
`tunnel_up` opens the apiserver forward before the first play starts, and
fails there if the host does not answer.

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

### By hand

Every playbook target above is one `docker run` of the client image plus an
`ansible-playbook` command line; `control_setup` is the exception — a
host-side `ssh` with no container at all, spelled out in `make/k3s.mk`.
The prefix is long because four of the
plays' requirements have to be reproduced from outside: `ansible.cfg` is looked
up in `$PWD`, `~/.ansible.cfg` and `/etc/ansible/ansible.cfg` — **not** next to
the playbook — and ignored silently otherwise (`config file = None`, then "no
inventory was parsed", then "could not match supplied host pattern");
`kubectl`/`helm`/`k8s_info` read `KUBECONFIG` from the environment, and both
binaries live in the image, so the path has to be the container's; the
container runs as the caller's uid with no passwd entry, so `USER` must be
passed explicitly or `group_vars` reads `lookup('env','USER')` as the number;
and the cluster is reached through an SSH tunnel the OPERATOR HOST opens —
`make tunnel_up` runs `ssh -L 6443:127.0.0.1:6443 -N`, waits for the
apiserver's `/readyz`, and leaves the forward bound on the host's own
loopback. The container joins that namespace with `--network host`, so the
kubeconfig the plays read — the host's k3s.yaml verbatim, server
`https://127.0.0.1:6443` — answers inside it unchanged, and the ssh the
undelegated tasks open to the host leaves through the same stack the
operator's own `make ssh` uses. A by-hand run needs the tunnel up first
(`make tunnel_up`), or the first delegated kubectl call fails against a
dead port.

```sh
docker run --rm -it --network host --user "$(id -u):$(id -g)" \
    -v "$PWD:/work" \
    -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro \
    -v "$SSH_AUTH_SOCK:/ssh-agent.sock" \
    -e USER="$(id -un)" \
    -e HOST_IP=203.0.113.10 \
    -e ANSIBLE_CONFIG=/work/ansible/ansible.cfg \
    asherikov/shoggoth_tools:client \
    ansible-playbook ansible/deploy.yml --limit shoggoth --ask-become-pass
```

No `/etc/hosts` is mounted: the delegated `localhost` tasks resolve service
names through the cluster CoreDNS — see the `TOOLS_CLIENT_ANSIBLE` comment in
the Makefile.

No home directory is mounted: the only identity the container has is the
agent socket. `make ssh_agent_setup` — the prerequisite every live target
carries — provisions one: the agent in your shell when it holds identities,
an agent of make's own at `.tmp/ssh-agent.sock` with the default identity
files (`~/.ssh/id_ed25519`, `id_rsa`, `id_ecdsa`, …) loaded into it when
nothing answers at all, and a failure naming what to run when neither is
available — before the first task, instead of at its first "Permission
denied". An agent that answers but holds no identity is the one case that
fails outright rather than being filled: it is the operator's own, its
contents are their business, and the failure names the key to add. The same
socket serves the host-side forward: `tunnel_up`'s ssh authenticates through
it, which is why every live target carries the prerequisite — the plays' ssh
and the forward alike. The `kubectl` operator
family needs no identity of its own: it reads the cluster through the
forward, in the network namespace it shares with the host. A hardware
token is the one case make cannot cover: its key cannot be loaded from a
file, so it needs your own agent (`eval "$(ssh-agent)"; ssh-add`) with
`SSH_AUTH_SOCK` exported. The image sets `HOME=/tmp`, so tools that
write "home" files (kubectl's discovery cache) have a writable target inside the
container. Staging is all under the container's /tmp: `local_tmp` on the
CONTROLLER side and `remote_tmp` on the TARGET are both pinned to
`/tmp/.ansible/tmp` by `ansible.cfg`, which is right for a `docker run --rm`
container — fresh for every play, and no home path inside the image for the
default `~/.ansible/tmp` to expand to. The host's `/etc/passwd` and
`/etc/group` are mounted read-only for the same reason OpenSSH needs them:
`ssh` resolves its uid with `getpwuid(getuid())` before anything else — an
explicit `-F` does not excuse it — and ignores `$HOME`, so without an entry
for the invoking uid every ssh — the undelegated `host_state` tasks and the
`synchronize` task's rsync alike — dies with `No user exists for uid <uid>`
before it opens a socket. No ssh in the
image reads a user config at all: the plays' connections and the rsync
`--rsh` are pinned to `-F /dev/null` by `ssh_common_args` in `ansible.cfg`,
so the passwd entry's home is never searched for a `.ssh` directory. `-it` is only
what makes the become prompt readable — the make targets drop it when stdin is
not a tty.

## The plays

### `deploy.yml`

The whole deploy, three passes, one invocation

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

The host OS is configured by the NixOS module — `shoggoth/shoggoth.nix` and
`shoggoth/ports.nix` — which stays the source of truth. `host.yml` renders and
deploys it: it substitutes the instance config's values into the two modules
on the control node, copies the results into `/etc/nixos`, refuses to rebuild
until `/etc/nixos/configuration.nix` imports them, rebuilds, and warms the
registry image the module's own containerd mirror cannot pull through itself.
The play configures nothing on its own.

The `imports` line in `/etc/nixos/configuration.nix` is the one manual step,
and it is what makes the play safe to re-run: the first pass deploys the
rendered files and the gate stops it before the rebuild, the second pass —
after the line — completes.

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

3. **`/etc/nixos` is the one path under `/etc` the plays write.** The host-OS
   contract stays in the module, and `host.yml` renders and deploys it (see
   "The host-OS contract" above). A `roles/` entry that writes `/etc/rancher`
   or `/etc/systemd` is the signal the cutover has moved past the module, and
   it must land together with retiring the module, not before.

