import os
import shutil
import tempfile
import unittest


class IsolatedHome(unittest.TestCase):
    """Each test gets its own empty FINANCES_HOME so vault files never leak between tests."""

    def setUp(self):
        self._old = os.environ.get("FINANCES_HOME")
        self.home = tempfile.mkdtemp()
        os.environ["FINANCES_HOME"] = self.home
        super().setUp()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FINANCES_HOME", None)
        else:
            os.environ["FINANCES_HOME"] = self._old
        shutil.rmtree(self.home, ignore_errors=True)
