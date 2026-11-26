# Package marker + load_tests shim.
# Without load_tests, unittest.TestLoader().loadTestsFromName('tools.tests')
# returns 0 tests and does not error -- a silent green for anyone who wires
# that form into CI. The loader reads only this __init__, never the sibling
# test_*.py modules, so this makes the package name load what the package holds.
import os

def load_tests(loader, standard_tests, pattern):
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return loader.discover(here, pattern="test_*.py", top_level_dir=root)
