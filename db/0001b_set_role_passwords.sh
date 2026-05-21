#!/usr/bin/env bash
# db/0001b_set_role_passwords.sh
#
# Runs inside the Postgres init container (docker-entrypoint-initdb.d).
# Sets LOGIN + PASSWORD on the evidence_writer and evidence_reader roles
# created by 0001_evidence_schema.sql.
#
# Passwords come from env vars injected by docker-compose — never hardcoded.
# This file must be executable: chmod +x db/0001b_set_role_passwords.sh
#
# Naming: files in docker-entrypoint-initdb.d are executed in filename order.
# "0001b_" runs immediately after "0001_evidence_schema.sql".

set -euo pipefail

: "${POSTGRES_WRITER_PASSWORD:?POSTGRES_WRITER_PASSWORD must be set}"
: "${POSTGRES_READER_PASSWORD:?POSTGRES_READER_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname   "${POSTGRES_DB}" \
     <<-SQL
    -- Grant LOGIN and set passwords for application roles.
    ALTER ROLE evidence_writer WITH LOGIN PASSWORD '${POSTGRES_WRITER_PASSWORD}';
    ALTER ROLE evidence_reader WITH LOGIN PASSWORD '${POSTGRES_READER_PASSWORD}';

    -- Allow the admin user to SET ROLE to each application role (useful for
    -- manual debugging; does not grant the role's permissions permanently).
    GRANT evidence_writer TO ${POSTGRES_USER};
    GRANT evidence_reader TO ${POSTGRES_USER};
SQL

echo "0001b: role passwords set for evidence_writer and evidence_reader"
