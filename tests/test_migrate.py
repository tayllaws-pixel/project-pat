import importlib.util, os, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("migrate", os.path.join(ROOT, "db/migrate.py"))
migrate = importlib.util.module_from_spec(spec); spec.loader.exec_module(migrate)


class MigrateTests(unittest.TestCase):
    def test_sections_route_to_correct_database(self):
        sql = open(os.path.join(ROOT, "db/migrations/0001_init.sql")).read()
        names = [n for n, _ in migrate.split_sections(sql)]
        self.assertEqual(names, ["common", "pat", "wade", "sentinel", "all"])
        pat = migrate.sql_for_db(sql, "pat"); wade = migrate.sql_for_db(sql, "wade"); sen = migrate.sql_for_db(sql, "sentinel")
        self.assertIn("CREATE TABLE IF NOT EXISTS messages", pat); self.assertNotIn("CREATE TABLE IF NOT EXISTS approvals", pat)
        self.assertIn("CREATE TABLE IF NOT EXISTS jobs", wade); self.assertNotIn("memory_versions", wade)
        self.assertIn("CREATE TABLE IF NOT EXISTS audit", sen); self.assertNotIn("CREATE TABLE IF NOT EXISTS jobs", sen)
        for s in (pat, wade, sen):
            self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", s)       # common preamble
            self.assertIn("schema_migrations", s)                            # @db all

    def test_plan_skips_applied_and_rejects_changed(self):
        sql = open(os.path.join(ROOT, "db/migrations/0001_init.sql")).read()
        cs = migrate.checksum(migrate.sql_for_db(sql, "pat"))
        self.assertEqual(migrate.plan("pat", {"0001": cs}), [])
        self.assertEqual([v for v, _, _ in migrate.plan("pat", {})], ["0001"])
        with self.assertRaises(RuntimeError):
            migrate.plan("pat", {"0001": "deadbeef"})

    def test_schema_statements_are_idempotent_guarded(self):
        sql = open(os.path.join(ROOT, "db/migrations/0001_init.sql")).read()
        import re
        creates = re.findall(r"^CREATE (TABLE|INDEX|EXTENSION)(?! IF NOT EXISTS)", sql, re.M)
        self.assertEqual(creates, [], "every CREATE must be IF NOT EXISTS")


if __name__ == "__main__":
    unittest.main()
