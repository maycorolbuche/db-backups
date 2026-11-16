import unittest

from db_backups import BackupError, dump_sections, load_config, validate_table_filters


class TableFiltersTests(unittest.TestCase):
    def test_example_configuration(self):
        _, databases = load_config("databases.example.yml")
        self.assertEqual(databases["home-care"]["ignore_data"], ["activity_log", "audit_logs"])
        self.assertEqual(databases["home-care"]["ignore_table"], ["cache", "cache_locks"])

    def test_rejects_invalid_or_overlapping_lists(self):
        for config in [
            {"ignore_data": "logs"},
            {"ignore_table": ["logs", "logs"]},
            {"ignore_data": ["app.logs"]},
            {"ignore_table": ["--force"]},
            {"ignore_data": ["logs"], "ignore_table": ["logs"]},
        ]:
            with self.subTest(config=config), self.assertRaises(BackupError):
                validate_table_filters("app", config)

    def test_no_filters_keeps_existing_dump(self):
        sections = dump_sections({"type": "mysql"}, "app", ["mysqldump"])
        self.assertEqual(sections, [
            ("Exportação do banco",
             ["mysqldump", "--routines", "--events", "--triggers", "--databases", "app"]),
        ])

    def test_mysql_separates_schema_and_data(self):
        sections = dump_sections({"type": "mysql", "ignore_data": ["logs"], "ignore_table": ["cache"]},
                                 "app", ["mysqldump"])
        schema, data, triggers = [args for _, args in sections]
        self.assertIn("--no-data", schema)
        self.assertNotIn("--ignore-table=app.logs", schema)
        self.assertIn("--ignore-table=app.logs", data)
        self.assertIn("--skip-triggers", data)
        self.assertIn("--triggers", triggers)
        self.assertTrue(all("--ignore-table=app.cache" in args for _, args in sections))

    def test_mariadb_uses_native_data_filter(self):
        sections = dump_sections({"type": "mariadb", "ignore_data": ["logs"], "ignore_table": ["cache"]},
                                 "app", ["mariadb-dump"])
        self.assertIn("--ignore-table-data=app.logs", sections[0][1])
        self.assertIn("--ignore-table=app.cache", sections[0][1])
        self.assertNotIn("--ignore-table-data=app.logs", sections[1][1])
