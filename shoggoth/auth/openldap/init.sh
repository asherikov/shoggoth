#!/usr/bin/env bash
set -euo pipefail

LDAP_BASE_DN="$(printf '%s' "${LDAP_DOMAIN}" | sed 's/\./,dc=/g; s/^/dc=/')"
ADMIN_DN="cn=admin,${LDAP_BASE_DN}"
CONFIG_ADMIN_DN="cn=admin,cn=config"
LDAP_URL="ldap://localhost:389"

clean_pw_file() {
    local src="${1}"
    local dst
    dst="$(mktemp)"
    tr -d '\n\r' < "${src}" > "${dst}"
    printf '%s' "${dst}"
}

ADMIN_PW="$(clean_pw_file /run/secrets/openldap_admin_password)"
CONFIG_ADMIN_PW="$(clean_pw_file /run/secrets/openldap_config_admin_password)"
trap 'rm -f "${ADMIN_PW:-}" "${CONFIG_ADMIN_PW:-}"' EXIT

SENTINEL="/var/lib/ldap/shoggoth-init-done"

if [ -f "${SENTINEL}" ]; then
    echo "shoggoth: LDAP already initialized, re-applying passwords for rotation"
    for i in $(seq 1 30); do
        if ldapsearch -x -H "${LDAP_URL}" \
            -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
            -b "${LDAP_BASE_DN}" -s base '(objectClass=*)' dn > /dev/null 2>&1; then
            break
        fi
        if [ "${i}" -eq 30 ]; then
            echo "shoggoth: FATAL: slapd never became ready" >&2
            exit 1
        fi
        sleep 2
    done
    ldapmodify -x -H "${LDAP_URL}" \
        -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
        -f /shoggoth/openldap/passwords.ldif || {
        echo "shoggoth: ERROR: password rotation failed" >&2
        exit 1
    }
    touch /run/shoggoth-ldap-init-done
    exit 0
fi

echo "shoggoth: === openldap: waiting for slapd ==="
for i in $(seq 1 60); do
    if ldapsearch -x -H "${LDAP_URL}" \
        -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
        -b "${LDAP_BASE_DN}" -s base '(objectClass=*)' dn > /dev/null 2>&1; then
        echo "shoggoth: slapd is ready."
        break
    fi
    if [ "${i}" -eq 60 ]; then
        echo "shoggoth: FATAL: slapd never became ready" >&2
        exit 1
    fi
    sleep 2
done

MDB_DN="olcDatabase={1}mdb,cn=config"

echo "shoggoth: === configuring memberof overlay ==="
MEMBEROF_EXISTS="$(ldapsearch -x -H "${LDAP_URL}" \
    -D "${CONFIG_ADMIN_DN}" -y "${CONFIG_ADMIN_PW}" \
    -b "olcOverlay=memberof,${MDB_DN}" -s base '(objectClass=*)' \
    olcMemberOfGroupOC 2>/dev/null || true)"

if printf '%s\n' "${MEMBEROF_EXISTS}" | grep -q "^olcMemberOfGroupOC:"; then
    CURRENT_GROUP_OC="$(printf '%s\n' "${MEMBEROF_EXISTS}" | grep '^olcMemberOfGroupOC:' | awk '{print $2}')"
    if [ "${CURRENT_GROUP_OC}" = "groupOfNames" ]; then
        echo "shoggoth: memberof overlay already configured correctly"
    else
        echo "shoggoth: replacing memberof overlay (current: ${CURRENT_GROUP_OC}, expected: groupOfNames)"
        ldapdelete -x -H "${LDAP_URL}" \
            -D "${CONFIG_ADMIN_DN}" -y "${CONFIG_ADMIN_PW}" \
            "olcOverlay=memberof,${MDB_DN}" || {
            echo "shoggoth: ERROR: failed to delete memberof overlay" >&2
            exit 1
        }
        echo "shoggoth: adding memberof overlay"
        ldapadd -x -H "${LDAP_URL}" \
            -D "${CONFIG_ADMIN_DN}" -y "${CONFIG_ADMIN_PW}" \
            -f /shoggoth/openldap/memberof_overlay.ldif || exit 1
        echo "shoggoth: memberof overlay replaced"
    fi
else
    MEMBEROF_MODULE_EXISTS="$(ldapsearch -x -H "${LDAP_URL}" \
        -D "${CONFIG_ADMIN_DN}" -y "${CONFIG_ADMIN_PW}" \
        -b "cn=module{0},cn=config" -s base '(objectClass=*)' olcModuleLoad 2>/dev/null || true)"
    if ! printf '%s\n' "${MEMBEROF_MODULE_EXISTS}" | grep -q "memberof"; then
        echo "shoggoth: loading memberof module"
        ldapmodify -x -H "${LDAP_URL}" \
            -D "${CONFIG_ADMIN_DN}" -y "${CONFIG_ADMIN_PW}" \
            -f /shoggoth/openldap/memberof_module.ldif || exit 1
    fi
    echo "shoggoth: adding memberof overlay"
    ldapadd -x -H "${LDAP_URL}" \
        -D "${CONFIG_ADMIN_DN}" -y "${CONFIG_ADMIN_PW}" \
        -f /shoggoth/openldap/memberof_overlay.ldif || exit 1
    echo "shoggoth: memberof overlay enabled"
fi

echo "shoggoth: === configuring ACLs ==="
ldapmodify -x -H "${LDAP_URL}" \
    -D "${CONFIG_ADMIN_DN}" -y "${CONFIG_ADMIN_PW}" \
    -f /shoggoth/openldap/config.ldif || {
    echo "shoggoth: ERROR: failed to apply ACLs" >&2
    exit 1
}

echo "shoggoth: === provisioning LDAP data ==="
ldapadd -c -x -H "${LDAP_URL}" \
    -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
    -f /shoggoth/openldap/data.ldif 2>&1 | grep -E 'ldap_[a-z]+:' | grep -v 'Already exists' || true

echo "shoggoth: setting passwords"
ldapmodify -x -H "${LDAP_URL}" \
    -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
    -f /shoggoth/openldap/passwords.ldif || {
    echo "shoggoth: ERROR: failed to set passwords" >&2
    exit 1
}

echo "shoggoth: === verifying setup ==="
ou_count="$(ldapsearch -x -H "${LDAP_URL}" \
    -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
    -b "${LDAP_BASE_DN}" -s sub '(objectClass=organizationalUnit)' dn 2>/dev/null \
    | grep -c "^dn:" || true)"
user_count="$(ldapsearch -x -H "${LDAP_URL}" \
    -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
    -b "${LDAP_BASE_DN}" -s sub '(objectClass=inetOrgPerson)' dn 2>/dev/null \
    | grep -c "^dn:" || true)"
group_count="$(ldapsearch -x -H "${LDAP_URL}" \
    -D "${ADMIN_DN}" -y "${ADMIN_PW}" \
    -b "${LDAP_BASE_DN}" -s sub '(objectClass=groupOfNames)' dn 2>/dev/null \
    | grep -c "^dn:" || true)"
echo "shoggoth: found ${ou_count} OUs, ${user_count} users, ${group_count} groups"
touch "${SENTINEL}"
touch /run/shoggoth-ldap-init-done