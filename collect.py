#!/usr/bin/env python3
"""Standalone catalog refresh — runs the collector without scheduling any posts."""
from config import load_config
from db import init_db, get_conn
from collector import run_collector

config = load_config()
init_db(config["db_path"])

with get_conn(config["db_path"]) as conn:
    run_collector(conn, config)
