import contextlib
import io
import os
import unittest
from unittest.mock import patch

from scripts import research_staging_admin


class StagingAdminGuardTests(unittest.TestCase):
    def test_nonlocal_or_ambiguous_databases_are_rejected_before_connecting(self):
        targets = [
            'postgresql://postgres@remote.example:55439/lol_staging',
            'postgresql://postgres@127.0.0.1:5432/lol_staging',
            'postgresql://postgres@127.0.0.1:55439/postgres',
            'postgresql://postgres@127.0.0.1:55439/lol_staging?host=remote.example',
            'not-a-dsn',
        ]
        for target in targets:
            with self.subTest(target=target), patch.dict(os.environ, {'STAGING_ADMIN_DSN':target,
                    'DATABASE_URL':'','RESEARCH_DATABASE_URL':''}, clear=True), \
                    patch.object(research_staging_admin.psycopg,'connect') as connect, \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit):
                    research_staging_admin.main(['revoke-key','--key-id','fixture'])
                connect.assert_not_called()
                self.assertNotIn(target, errors.getvalue())

    def test_existing_application_database_configuration_fails_closed(self):
        for other in ('DATABASE_URL','RESEARCH_DATABASE_URL'):
            env={'STAGING_ADMIN_DSN':'postgresql://postgres@127.0.0.1:55439/lol_staging',
                 'DATABASE_URL':'','RESEARCH_DATABASE_URL':'',other:'do-not-connect'}
            with self.subTest(other=other), patch.dict(os.environ,env,clear=True), \
                    patch.object(research_staging_admin.psycopg,'connect') as connect, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    research_staging_admin.main(['revoke-key','--key-id','fixture'])
                connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
