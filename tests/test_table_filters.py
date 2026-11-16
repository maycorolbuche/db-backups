import unittest
from unittest.mock import patch

from db_backups import BackupError, dump_error, dump_sections, dump_tls_flags, load_config, selected_routines, validate_table_filters


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


class DumpStructureErrorTests(unittest.TestCase):
    def test_uses_server_error_from_command_failure(self):
        stderr = b"mysqldump: Couldn't execute 'SHOW CREATE TABLE hidden': SELECT denied for private-user (1142)\n"
        self.assertEqual(
            dump_error(stderr, 2),
            "Erro MySQL/MariaDB 1142: usuário sem permissão para a operação na tabela.",
        )

    def test_identifies_missing_routine_privileges_without_copying_names(self):
        stderr = b"mysqldump: private-user has insufficient privileges to SHOW CREATE FUNCTION hidden"
        result = dump_error(stderr, 2)
        self.assertIn("sem permissão para exportar a definição de uma rotina", result)
        self.assertNotIn("private-user", result)


class ExportObjectTests(unittest.TestCase):
    def test_routine_switches_disable_combined_dump(self):
        for procedures, functions in [(False, False), (True, False), (False, True)]:
            with self.subTest(procedures=procedures, functions=functions):
                config = {"type": "mysql", "export_procedures": procedures, "export_functions": functions}
                sections = dump_sections(config, "app", ["mysqldump"], ["report_view"])
                self.assertNotIn("--routines", sections[0][1])
                self.assertIn("--ignore-table=app.report_view", sections[0][1])

    def test_rejects_non_boolean_export_switch(self):
        with self.assertRaisesRegex(BackupError, "export_views"):
            validate_table_filters("app", {"export_views": "false"})

    def test_selects_one_type_without_inspecting_the_other(self):
        for kind in ("PROCEDURE", "FUNCTION"):
            with self.subTest(kind=kind), patch("db_backups.query_schema") as query:
                query.side_effect = [
                    [{"ROUTINE_NAME": "sample"}],
                    [{"Create Database": "CREATE DATABASE `app`"}],
                    [{
                        f"Create {kind.title()}": f"CREATE {kind} `sample`()",
                        "sql_mode": "NO_BACKSLASH_ESCAPES",
                        "collation_connection": "utf8mb4_general_ci",
                    }],
                ]
                sql = selected_routines(["mysql"], "app", kind, 0).decode()
                self.assertIn(f"ROUTINE_TYPE = '{kind}'", query.call_args_list[0].args[1])
                self.assertEqual(query.call_args_list[2].args[1], f"SHOW CREATE {kind} `sample`")
                self.assertIn(f"CREATE {kind} `sample`()", sql)
                self.assertIn("CREATE DATABASE IF NOT EXISTS `app`", sql)
