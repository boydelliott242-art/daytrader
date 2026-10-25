"""One name for the two modules' suites (trader-largecap and trader-scorecard), so

    python3 -m unittest discover -s tests -p 'test_largecap_scorecard*.py' -v

runs tests/test_largecap.py and tests/test_scorecard.py together. Under the plain discovery
(pattern test*.py) this file adds nothing, so the full suite never counts a test twice."""

import unittest

import test_largecap
import test_scorecard


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    if pattern and "largecap_scorecard" in pattern:
        suite.addTests(loader.loadTestsFromModule(test_largecap))
        suite.addTests(loader.loadTestsFromModule(test_scorecard))
    return suite


if __name__ == "__main__":
    unittest.main(defaultTest=["test_largecap", "test_scorecard"])
