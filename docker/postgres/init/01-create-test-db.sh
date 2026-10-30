#!/bin/sh
# Runs once, on first initialisation of the Postgres data volume.
# Creates a separate database so the test suite can TRUNCATE freely without touching app data.
set -eu

: "${POSTGRES_TEST_DB:?POSTGRES_TEST_DB must be set (see .env.example)}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "CREATE DATABASE \"$POSTGRES_TEST_DB\";"
