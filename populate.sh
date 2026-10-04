#!/bin/sh
# Populate an OpenCloud instance with the Weyland demo data (see README.md).
cd "$(dirname "$0")" && exec python3 populate.py "$@"
