#!/usr/bin/env bash
set -euo pipefail

. /shoggoth/bringup/rendered/gitea/gitea.env

for i in $(seq 1 30); do
    if AUTH_CHECK="$(su-exec git gitea admin auth list 2>&1)"; then
        break
    fi
    if [ "${i}" -eq 30 ]; then
        echo "shoggoth: ERROR: Gitea never became ready: ${AUTH_CHECK}" >&2
        exit 1
    fi
    echo "shoggoth: Waiting for Gitea to be ready (attempt ${i}/30): ${AUTH_CHECK}"
    sleep 2
done

EXISTING="$(su-exec git gitea admin auth list 2>/dev/null \
    | awk -v name="openldap" '$2 == name {print $1; exit}')"

LDAP_ARGS=(-name openldap
    -security-protocol unencrypted
    -host "${LDAP_HOST}"
    -port 389
    -bind-dn "${LDAP_BIND_DN}"
    -bind-password "${LDAP_BIND_PASSWORD}"
    -user-search-base "ou=people,${LDAP_BASE_DN}"
    -user-filter "(&(objectClass=inetOrgPerson)(|(uid=%[1]s)(mail=%[1]s)))"
    -admin-filter "(memberOf=cn=admins,ou=groups,${LDAP_BASE_DN})"
    -username-attribute uid
    -firstname-attribute cn
    -surname-attribute sn
    -email-attribute mail
    -synchronize-users)

if [ -z "${EXISTING}" ]; then
    su-exec git gitea admin auth add-ldap "${LDAP_ARGS[@]}"
else
    su-exec git gitea admin auth update-ldap --id "${EXISTING}" "${LDAP_ARGS[@]}"
fi

echo "shoggoth: === gitea ldap init complete ==="

echo "shoggoth: Setting local admin password"
su-exec git gitea admin user change-password --username admin --password "${SHOGGOTH_ADMIN_PASSWORD}" --must-change-password=false 2>/dev/null \
    || echo "shoggoth: admin user not found yet (will be created on first web login)"