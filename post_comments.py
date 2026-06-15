#!/usr/bin/env python3
"""Standalone runner for pending LinkedIn first comments. Designed to run hourly."""
from config import load_config
from db import init_db, get_conn
from publora import process_pending_comments

config = load_config()
init_db(config["db_path"])

with get_conn(config["db_path"]) as conn:
    process_pending_comments(conn, config)
