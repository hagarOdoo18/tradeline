#!/usr/bin/env bash
set -euo pipefail
for db in live_11nov_2024 live_20MAR_2022; do
  c=$(sudo -n -u odoo12 psql -d "$db" -Atf /tmp/odoo12_cutoff_count.sql)
  echo "$db,$c"
done
