#!/bin/bash
cd "$(dirname "$(readlink -f "$0")")"
exec ./venv/bin/python display_service.py
