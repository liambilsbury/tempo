#!/usr/bin/env python3
"""Exec sleep under another process name with SIGINT at its default"""
import os
import signal
import sys

signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execvp("sleep", [sys.argv[1], sys.argv[2]])
