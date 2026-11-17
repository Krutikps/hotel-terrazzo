#!/usr/bin/env bash
# Render build step: install, collect static files, migrate, bootstrap. Exit on first error.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py bootstrap_site
