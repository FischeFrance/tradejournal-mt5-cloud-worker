import os
import sys

WORKER_DIR = os.path.join(os.path.dirname(__file__), "..", "worker")
sys.path.insert(0, os.path.abspath(WORKER_DIR))
