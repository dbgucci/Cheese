import os
import tempfile

# Point the app's data folder at a temp dir for the whole test session, so
# tests never create or touch a real Desktop/CheeseSignals folder.
os.environ.setdefault(
    "CHEESE_SIGNALS_HOME", tempfile.mkdtemp(prefix="cheese-signals-tests-")
)
