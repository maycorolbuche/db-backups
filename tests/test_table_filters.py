import unittest

from db_backups import BackupError, dump_error, dump_sections, dump_tls_flags, load_config, validate_table_filters


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


class DumpErrorTests(unittest.TestCase):
    def test_reports_cause_without_copying_connection_details(self):
        cases = [
            (b"mysqldump: Got error: 1045: Access denied for 'private-user'@'private-host'", "autenticação recusada"),
            (b"mysqldump: Got error: 2026: SSL connection error", "falha de TLS"),
            (b"mysqldump: Couldn't find table: \"ACESSOS\"", "Tabela não encontrada"),
            (b"mysqldump: unknown variable 'private-value'", "rejeitou uma opção"),
            (b"unexpected private-user private-host private-value", "código 2"),
        ]
        for stderr, reason in cases:
            with self.subTest(reason=reason):
                result = dump_error(stderr, 2)
                self.assertIn(reason, result)
                self.assertNotIn("private-", result)

    def test_reports_only_the_origin_ip_on_connection_rejection(self):
        cases = [
            (b"Got error: 1045: Access denied for user 'private-user'@'203.0.113.10'", "203.0.113.10"),
            (b"Got error: 1130: Host '2001:db8::10' is not allowed to connect", "2001:db8::10"),
        ]
        for stderr, address in cases:
            with self.subTest(address=address):
                result = dump_error(stderr, 2)
                self.assertIn(f"IP de origem informado pelo servidor: {address}.", result)
                self.assertNotIn("private-user", result)


class DumpTlsTests(unittest.TestCase):
    def test_mysql_requires_tls_when_ca_is_omitted(self):
        self.assertEqual(dump_tls_flags("mysql", {"host": "db.example.com"}), ["--ssl-mode=REQUIRED"])

    def test_mysql_verifies_identity_when_ca_is_configured(self):
        self.assertEqual(
            dump_tls_flags("mysql", {"host": "db.example.com", "ssl-ca": "mysql-ca.pem"}),
            ["--ssl-mode=VERIFY_IDENTITY"],
        )

    def test_local_mysql_keeps_native_defaults(self):
        self.assertEqual(dump_tls_flags("mysql", {"host": "localhost"}), [])

    def test_mariadb_keeps_certificate_verification(self):
        with self.assertRaisesRegex(BackupError, "Configure ssl-ca"):
            dump_tls_flags("mariadb", {"host": "db.example.com"})
        self.assertEqual(
            dump_tls_flags("mariadb", {"host": "db.example.com", "ssl-ca": "mysql-ca.pem"}),
            ["--ssl-verify-server-cert"],
        )
