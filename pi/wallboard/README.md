# Recovered from the Pi

`pi/inspect-wallboard.sh` copies the live `wallboard-kiosk.sh` here when it
finds one. That script drives the jobContext Grafana kiosk playlist and
selects a playlist per booted OS from `up{job=gpu-windows}` — load-bearing
infrastructure that currently exists only on the Pi and in no repository.

Commit whatever lands here. Flight Deck adopts this behaviour rather than
reimplementing it, so this file is the reference for what "preserve the
existing playlist logic" actually means.
