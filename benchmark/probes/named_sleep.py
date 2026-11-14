#!/usr/bin/env python3
import os
import signal
import sys

signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execvp("sleep", [sys.argv[1], sys.argv[2]])
