#!/usr/bin/env bash
set -euo pipefail

if [ -n "${REDMINE_SECRET_KEY_BASE_FILE:-}" ] && [ -z "${SECRET_KEY_BASE:-}" ]; then
    export SECRET_KEY_BASE="$(cat "${REDMINE_SECRET_KEY_BASE_FILE}")"
fi

. /shoggoth/bringup/rendered/redmine/redmine.env

for i in $(seq 1 60); do
    if READY_CHECK="$(bundle exec rails runner 'puts "ready"' 2>&1)"; then
        echo "shoggoth: Redmine is ready for LDAP configuration"
        break
    fi
    echo "shoggoth: Waiting for Redmine to be ready (attempt ${i}/60): ${READY_CHECK}"
    sleep 2
done

if ! bundle exec rails runner 'puts "ready"' > /dev/null 2>&1; then
    echo "shoggoth: FATAL: Redmine never became ready" >&2
    exit 1
fi

echo "shoggoth: Configuring LDAP auth source"
LDAP_HOST="${LDAP_HOST}" LDAP_BIND_DN="${LDAP_BIND_DN}" LDAP_BIND_PASSWORD="${LDAP_BIND_PASSWORD}" LDAP_BASE_DN="${LDAP_BASE_DN}" \
    bundle exec rails runner '
    auth = AuthSourceLdap.find_or_initialize_by(name: "openldap")
    auth.host = ENV["LDAP_HOST"]
    auth.port = 389
    auth.account = ENV["LDAP_BIND_DN"]
    auth.account_password = ENV["LDAP_BIND_PASSWORD"]
    auth.base_dn = ENV["LDAP_BASE_DN"]
    auth.attr_login = "uid"
    auth.attr_firstname = "cn"
    auth.attr_lastname = "sn"
    auth.attr_mail = "mail"
    auth.onthefly_register = true
    auth.tls = false
    auth.save!
' && echo "shoggoth: === redmine ldap init complete ===" \
    || { echo "shoggoth: FATAL: LDAP auth source configuration failed" >&2; exit 1; }

echo "shoggoth: Setting local admin password"
RESULT="$(SHOGGOTH_ADMIN_PASSWORD="${SHOGGOTH_ADMIN_PASSWORD}" bundle exec rails runner '
    user = User.find_by_login("admin")
    if user
      user.password = ENV["SHOGGOTH_ADMIN_PASSWORD"]
      user.password_confirmation = ENV["SHOGGOTH_ADMIN_PASSWORD"]
      user.must_change_passwd = false
      user.save!
      puts "updated"
    else
      puts "not_found"
    end
')" || { echo "shoggoth: ERROR: admin password update failed" >&2; exit 1; }
case "${RESULT}" in
    updated)    echo "shoggoth: admin password updated" ;;
    not_found)  echo "shoggoth: admin user not found, skipping password update" ;;
esac