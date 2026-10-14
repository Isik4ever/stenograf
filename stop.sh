#!/bin/zsh
# Stoppar Stenograf-servern.
pkill -f "uvicorn app.server:app --host 127.0.0.1 --port ${STENOGRAF_PORT:-8790}" && echo "Stenograf stoppad." || echo "Stenograf körde inte."
