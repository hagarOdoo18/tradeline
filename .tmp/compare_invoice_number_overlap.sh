#!/usr/bin/env bash
set -euo pipefail
sudo -n -u odoo12 psql -d live_11nov_2024 -Atc "SELECT number FROM account_invoice WHERE number IS NOT NULL AND number <> '' ORDER BY number" > /tmp/inv_numbers_live11.txt
sudo -n -u odoo12 psql -d live_20MAR_2022 -Atc "SELECT number FROM account_invoice WHERE number IS NOT NULL AND number <> '' ORDER BY number" > /tmp/inv_numbers_live20.txt
echo -n 'common_numbers,'
comm -12 /tmp/inv_numbers_live11.txt /tmp/inv_numbers_live20.txt | wc -l
echo -n 'only_live11_numbers,'
comm -23 /tmp/inv_numbers_live11.txt /tmp/inv_numbers_live20.txt | wc -l
echo -n 'only_live20_numbers,'
comm -13 /tmp/inv_numbers_live11.txt /tmp/inv_numbers_live20.txt | wc -l
