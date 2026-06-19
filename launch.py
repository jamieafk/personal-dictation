"""Entry point for py2app. Sets up the package path and runs the app."""
import sys
import os

# Add the project root to sys.path so 'from src import ...' works
project_dir = os.path.dirname(os.path.abspath(__file__))
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

from src.app import DictationApp
DictationApp().run()
