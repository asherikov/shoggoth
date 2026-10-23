{{/*
Expand the name of the chart.
*/}}
{{- define "shoggoth.name" -}}
{{- default .Chart.Name .Values.shoggoth.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
Truncated at 53 chars because some Kubernetes name fields are limited to
this (by the DNS naming spec, matches the namespace constraint
make/k3s.mk:215 enforces via `printf '%s' "${INSTANCE}" | wc -c -gt 54`).
*/}}
{{- define "shoggoth.fullname" -}}
{{- if .Values.shoggoth.fullnameOverride -}}
{{- .Values.shoggoth.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.shoggoth.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 53 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Canonical shoggoth labels. Preserves the existing `app:` label convention
from k3s/*.yaml so `make start|stop|restart_service SERVICE=foo` (which
selects by `kubectl get pod -l app=foo`) keeps working unchanged.
*/}}
{{- define "shoggoth.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "shoggoth.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: shoggoth
{{- end -}}

{{/*
Same labels as shoggoth.labels, but with a dict of per-key overrides merged on
top. Use this from templates that need a non-default value (e.g.
app.kubernetes.io/part-of: external-secrets for resources that were
external-secrets-bundle-owned in legacy shoggoth/k3s/) instead of emitting a
literal key after the helper — that pattern silently duplicates the
helper's key in the rendered YAML and breaks `kubeconform -strict` plus
every YAML 1.2 spec-compliant parser.

Accepted override keys today:
  partOf — replaces `app.kubernetes.io/part-of` (default `shoggoth`)

Example:
  labels:
    {{- include "shoggoth.labelsWithMap" (list . (dict "partOf" "external-secrets")) | nindent 4 }}
*/}}
{{- define "shoggoth.labelsWithMap" -}}
{{- $root := index . 0 -}}
{{- $overrides := index . 1 -}}
helm.sh/chart: {{ printf "%s-%s" $root.Chart.Name $root.Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "shoggoth.selectorLabels" $root }}
{{- if $root.Chart.AppVersion }}
app.kubernetes.io/version: {{ $root.Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ $root.Release.Service }}
app.kubernetes.io/part-of: {{ dig "partOf" "shoggoth" $overrides }}
{{- end -}}

{{/*
Selector labels — used by Deployments, StatefulSets, etc. for their
spec.selector.matchLabels. Stable per release.
*/}}
{{- define "shoggoth.selectorLabels" -}}
app.kubernetes.io/name: {{ include "shoggoth.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
LDAP base DN, derived from identity.domain.
  s.local -> dc=s,dc=local

`ldap.baseDn` may only restate that value, never replace it. The osixia
server bootstraps its suffix from LDAP_DOMAIN, which openldap.yaml sets to
identity.domain — so the directory that ends up created is always the derived
DN. Any other baseDn left every DN the chart emits (ADMIN_DN, all LDIF entries,
the bind DNs in gitea/redmine/openbao/phpldapadmin) pointing at a suffix that
was never created. That failure was silent: init.sh runs as a postStart hook
whose command ends in `echo` with only `set -o pipefail`, so its `exit 1` never
fails the hook, and the readinessProbe gates on a sentinel that the aborted
script never touches — the pod sat NotReady with an unprovisioned directory.

Rather than reconcile at runtime, refuse to render the disagreement.
*/}}
{{- define "shoggoth.ldapBaseDn" -}}
{{- $derived := "" -}}
{{- if .Values.identity.domain -}}
{{- $derived = printf "dc=%s" (.Values.identity.domain | replace "." ",dc=") -}}
{{- end -}}
{{- if .Values.ldap.baseDn -}}
{{- if ne .Values.ldap.baseDn $derived -}}
{{- fail (printf "ldap.baseDn (%q) disagrees with identity.domain (%q -> %q). The openldap container bootstraps its suffix from LDAP_DOMAIN=identity.domain, so it would serve %q while every DN this chart emits targets %q and nothing would bind. Set ldap.baseDn to %q or leave it empty." .Values.ldap.baseDn .Values.identity.domain $derived $derived .Values.ldap.baseDn $derived) -}}
{{- end -}}
{{- end -}}
{{- $derived -}}
{{- end -}}

{{/*
Whether a given service is enabled. Renders "1" when enabled, "" otherwise,
so callers can use the helper directly as a conditional:
  {{- if include "shoggoth.isServiceEnabled" (list . "gitea-runner") -}}

Range-based lookup because the dotted-path form `.Values.services.gitea-runner.enabled`
fails to parse: Go-template treats the `-` as a subtraction operator. Range walks
the services map and matches by key, so every key shape (hyphenated, camelCase,
plain) is handled by the same call site — values.yaml uses one consistent
hyphenated naming.
*/}}
{{- define "shoggoth.isServiceEnabled" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
{{- $enabled := false -}}
{{- range $service, $cfg := $root.Values.services -}}
{{- if eq $service $name -}}
{{- if $cfg.enabled -}}
{{- $enabled = true -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $enabled -}}1{{- end -}}
{{- end -}}
