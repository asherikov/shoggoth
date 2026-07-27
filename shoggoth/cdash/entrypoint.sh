#!/usr/bin/env bash
set -euo pipefail

. /shoggoth/bringup/rendered/cdash/cdash.env

export APP_KEY DB_PASSWORD
export CDASH_AUTHENTICATION_PROVIDER LOGIN_FIELD
export LDAP_HOSTS LDAP_PORT LDAP_PROVIDER LDAP_LOCATE_USERS_BY LDAP_USERNAME LDAP_PASSWORD LDAP_BASE_DN
export USER_CREATE_PROJECTS TOKEN_DURATION OPENLDAP_S_SLAVE_PASSWORD SHOGGOTH_VAULT_TOKEN OPENBAO_ADDR

printf 'Listen 80\n' > /etc/apache2/ports.conf
sed -i 's/:8080/:80/g' /etc/apache2/sites-enabled/cdash-site.conf

unset LDAP_BIND_PASSWORD

exec /cdash/docker/docker-entrypoint.sh "$@"