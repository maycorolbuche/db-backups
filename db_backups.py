"""Create, retain and monitor encrypted MySQL/MariaDB backups."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import ClientError
from croniter import croniter


STAMP = "%Y%m%dT%H%M%S%fZ"
OBJECT_NAME = re.compile(r"(\d{8}T\d{12}Z)_(manual|\d{8}T\d{12}Z)_[0-9a-f]{32}\.sql\.gz\.age")


class BackupError(Exception):
    """An error whose message is safe to send to Telegram."""


def utcnow():
    return datetime.now(timezone.utc)


def stamp(moment):
    return moment.astimezone(timezone.utc).strftime(STAMP)


def load_config(path, *, validate_filters=True):
    config = yaml.safe_load(Path(path).read_text())
    zone = ZoneInfo(config.get("timezone", "America/Sao_Paulo"))
    databases = config["databases"]
    if not isinstance(databases, dict):
        raise BackupError("databases deve ser um mapa por identificador.")
    for db_id, db in databases.items():
        if not isinstance(db_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", db_id):
            raise BackupError("Identificador de banco inválido.")
        if db["type"] not in {"mysql", "mariadb"}:
            raise BackupError(f"{db_id}: tipo de banco inválido.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", db["executor"]):
            raise BackupError(f"{db_id}: executor inválido.")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", db["credentials_env"]):
            raise BackupError(f"{db_id}: variável de credenciais inválida.")
        schedule = db["schedule"]
        if len(schedule.split()) != 5 or not croniter.is_valid(schedule):
            raise BackupError(f"{db_id}: agenda cron inválida.")
        for field in ("keep", "grace_minutes", "max_storage_mb"):
            value = db.get(field)
            if field == "max_storage_mb" and value is None:
                continue
            if type(value) is not int or value < 1:
                raise BackupError(f"{db_id}: {field} deve ser inteiro positivo.")
        if not isinstance(db.get("name", db_id), str):
            raise BackupError(f"{db_id}: nome inválido.")
        if validate_filters:
            validate_table_filters(db_id, db)
    return zone, databases


def validate_table_filters(db_id, db):
    for field in ("export_procedures", "export_functions", "export_views"):
        if type(db.get(field, True)) is not bool:
            raise BackupError(f"{db_id}: {field} deve ser true ou false.")
    for option in ("ignore_data", "ignore_table"):
        tables = db.get(option, [])
        if not isinstance(tables, list) or any(
            not isinstance(table, str) or not 1 <= len(table) <= 64
            or table.startswith("-") or table != table.strip()
            or any(char in "./\\" or ord(char) < 32 for char in table)
            for table in tables
        ):
            raise BackupError(f"{db_id}: {option} deve ser uma lista de nomes de tabelas.")
        if len(tables) != len(set(tables)):
            raise BackupError(f"{db_id}: {option} contém tabelas repetidas.")
    overlap = set(db.get("ignore_data", [])) & set(db.get("ignore_table", []))
    if overlap:
        raise BackupError(f"{db_id}: tabelas nas duas listas: {', '.join(sorted(overlap))}.")


def due(db, zone, moment):
    local = moment.astimezone(zone)
    return stamp(croniter(db["schedule"], local + timedelta(microseconds=1)).get_prev(datetime))


def retention_plan(backups, new, keep, limit):
    if limit is not None and new["Size"] > limit:
        raise BackupError(f"Espaço insuficiente. O arquivo tem {new['Size']:,} bytes e a cota é {limit:,} bytes.")
    retained = [new]
    total = new["Size"]
    for obj in backups:
        if len(retained) == keep or (limit is not None and total + obj["Size"] > limit):
            break
        retained.append(obj)
        total += obj["Size"]
    quota_limited = len(retained) < min(keep, len(backups) + 1)
    return retained, backups[len(retained) - 1:], quota_limited


def send_telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    if not token or not chat:
        raise BackupError("Credenciais do Telegram ausentes.")
    payload = {"chat_id": chat, "text": message}
    thread = os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
    if thread:
        if not thread.isascii() or not thread.isdecimal() or int(thread) < 1:
            raise BackupError("TELEGRAM_MESSAGE_THREAD_ID inválido.")
        payload["message_thread_id"] = int(thread)
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage",
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            if not json.load(response).get("ok"):
                raise BackupError("Telegram rejeitou a mensagem.")
    except HTTPError as error:
        raise BackupError(f"Telegram retornou HTTP {error.code}.") from None


class Storage:
    def __init__(self):
        self.bucket = os.environ["R2_BUCKET"]
        account = os.environ["R2_ACCOUNT_ID"]
        if not re.fullmatch(r"[0-9a-f]{32}", account):
            raise BackupError("R2_ACCOUNT_ID inválido.")
        self.s3 = boto3.client(
            "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            region_name="auto", aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )

    def list(self, prefix):
        pages = self.s3.get_paginator("list_objects_v2")
        return [obj for page in pages.paginate(Bucket=self.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])]

    def backups(self, db_id, clean_incomplete=False):
        prefix = f"backups/{db_id}/"
        objects = [obj for obj in self.list(prefix)
                   if OBJECT_NAME.fullmatch(obj["Key"][len(prefix):])]
        verified = {obj["Key"] for obj in self.list(f"verified/{db_id}/")}
        valid = []
        for obj in objects:
            if "verified/" + obj["Key"].removeprefix("backups/") in verified:
                valid.append(obj)
            elif clean_incomplete:
                self.delete(obj["Key"])
        return sorted(valid, key=lambda obj: obj["Key"], reverse=True)

    def mark_verified(self, key, sha256):
        marker = "verified/" + key.removeprefix("backups/")
        self.write(marker, {"sha256": sha256})

    def remove_backup(self, key):
        self.delete(key)
        try:
            self.delete("verified/" + key.removeprefix("backups/"))
        except Exception as error:
            print(f"Falha ao limpar marcador: {describe(error)}", flush=True)

    def read(self, key):
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return {}
            raise
        with closing(response["Body"]) as body:
            return json.load(body)

    def write(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(data).encode(),
                           ContentType="application/json")

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def metadata(self, key):
        return self.s3.head_object(Bucket=self.bucket, Key=key)["Metadata"]

    def upload(self, path, key, sha256):
        self.s3.upload_file(str(path), self.bucket, key,
                            ExtraArgs={"Metadata": {"sha256": sha256}})
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        with closing(response["Body"]) as body:
            digest = hashlib.sha256()
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != sha256 or response["ContentLength"] != path.stat().st_size:
            raise BackupError("O arquivo recuperado do R2 não passou na verificação de integridade.")

    def notify(self, channel, message):
        key = f"notifications/{channel}/{stamp(utcnow())}-{uuid4().hex}.json"
        try:
            self.write(key, {"message": message})
        except Exception:
            send_telegram(message)
            return
        self.deliver(key, message)

    def deliver(self, key, message):
        try:
            send_telegram(message)
        except Exception as error:
            print(f"Notificação pendente: {describe(error)}", flush=True)
            return False
        try:
            self.delete(key)
        except Exception as error:
            print(f"Falha ao confirmar notificação: {describe(error)}", flush=True)
            return False
        return True

    def retry(self, channel):
        for obj in sorted(self.list(f"notifications/{channel}/"), key=lambda item: item["Key"]):
            item = self.read(obj["Key"])
            if item and not self.deliver(obj["Key"], item["message"]):
                break


def describe(error):
    if isinstance(error, BackupError):
        return str(error)
    if isinstance(error, ClientError):
        return f"R2 retornou {error.response['Error']['Code']}."
    if isinstance(error, KeyError):
        return "Configuração obrigatória ausente."
    if isinstance(error, subprocess.TimeoutExpired):
        return "Tempo limite excedido."
    if isinstance(error, FileNotFoundError):
        return "Executável ou arquivo de configuração indisponível."
    return type(error).__name__


def option_value(value):
    raw = str(value)
    if any(ord(char) < 32 for char in raw):
        raise BackupError("Valor inválido nas credenciais MySQL/MariaDB.")
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def dump_sections(db, database, command, excluded_views=()):
    routines = ["--routines"] if db.get("export_procedures", True) and db.get("export_functions", True) else []
    ignored_objects = [*db.get("ignore_table", []), *excluded_views]
    ignore_table = [f"--ignore-table={database}.{table}" for table in ignored_objects]
    ignore_data = db.get("ignore_data", [])
    base = [*command, *ignore_table]
    target = ["--databases", database]
    if not ignore_data:
        return [("Exportação do banco", [*base, *routines, "--events", "--triggers", *target])]

    if db["type"] == "mariadb":
        data_filters = [f"--ignore-table-data={database}.{table}" for table in ignore_data]
        sections = [("Exportação da estrutura e dos dados",
                     [*base, *data_filters, "--skip-triggers", *routines, *target])]
    else:
        data_filters = [f"--ignore-table={database}.{table}" for table in ignore_data]
        sections = [
            ("Exportação da estrutura", [*base, "--no-data", "--skip-triggers", *routines, *target]),
            ("Exportação dos dados", [*base, *data_filters, "--no-create-info",
                                     "--no-create-db", "--skip-triggers", *target]),
        ]
    sections.append(("Exportação dos gatilhos e eventos",
                     [*base, "--no-data", "--no-create-info", "--no-create-db",
                      "--triggers", "--events", *target]))
    return sections


def dump_error(stderr, returncode):
    message = stderr.decode("utf-8", errors="replace")
    match = re.search(r"(?i)(?:got error|error):\s*(\d{4})\b", message)
    if not match:
        match = re.search(
            r"(?im)^[^\n]*couldn['’]t execute [^\n]*\((\d{4})\)[ \t]*$", message
        )
    if match:
        code = match.group(1)
        reasons = {
            "1044": "usuário sem acesso ao banco",
            "1045": "autenticação recusada pelo servidor",
            "1049": "banco de dados não encontrado",
            "1109": "tabela não encontrada",
            "1130": "origem da conexão não autorizada pelo servidor",
            "1142": "usuário sem permissão para a operação na tabela",
            "1146": "tabela não encontrada",
            "1227": "usuário sem o privilégio exigido para a operação",
            "1356": "view inválida ou sem permissão sobre seus objetos",
            "2002": "não foi possível conectar ao servidor",
            "2003": "não foi possível conectar ao servidor",
            "2005": "host do banco não encontrado",
            "2013": "conexão perdida durante a operação",
            "2026": "falha de TLS ou na validação do certificado",
            "2059": "plugin de autenticação indisponível no cliente",
        }
        reason = reasons.get(code, "operação recusada pelo cliente ou servidor")
        result = f"Erro MySQL/MariaDB {code}: {reason}."
        if code in {"1045", "1130"}:
            origin = re.search(r"@'([^']+)'", message) if code == "1045" else re.search(
                r"(?i)\bhost '([^']+)' is not allowed", message
            )
            if origin:
                try:
                    address = ipaddress.ip_address(origin.group(1))
                except ValueError:
                    pass
                else:
                    result += f" IP de origem informado pelo servidor: {address}."
        return result
    if re.search(r"(?i)insufficient privileges to show create (?:function|procedure)", message):
        return "Usuário sem permissão para exportar a definição de uma rotina (SHOW CREATE FUNCTION ou PROCEDURE)."
    if "couldn't find table:" in message.lower():
        return "Tabela não encontrada. Confira o nome e as maiúsculas e minúsculas."
    if re.search(r"(?i)unknown (?:option|variable)", message):
        return "O cliente de dump rejeitou uma opção. Confira a compatibilidade do executável."
    return f"O cliente de dump encerrou com código {returncode}."


def dump_tls_flags(engine, credentials):
    host = credentials.get("host")
    if host is None or host in ("localhost", "127.0.0.1", "::1"):
        return []
    if engine == "mysql":
        mode = "VERIFY_IDENTITY" if credentials.get("ssl-ca") else "REQUIRED"
        return [f"--ssl-mode={mode}"]
    if not credentials.get("ssl-ca"):
        raise BackupError("Configure ssl-ca para a conexão MariaDB remota.")
    return ["--ssl-verify-server-cert"]


def quote_identifier(name):
    return "`" + name.replace("`", "``") + "`"


def query_schema(command, statement, deadline):
    result = subprocess.run([*command, f"--execute={statement}"], capture_output=True,
                            timeout=max(0, deadline - time.monotonic()))
    if result.returncode:
        raise BackupError(dump_error(result.stderr, result.returncode))
    try:
        root = ElementTree.fromstring(result.stdout)
    except ElementTree.ParseError:
        raise BackupError("Não foi possível ler os metadados do banco.") from None
    return [{field.attrib["name"]: field.text for field in row.findall("field")}
            for row in root.findall("row")]


def selected_routines(command, database, kind, deadline):
    rows = query_schema(command, "SELECT ROUTINE_NAME FROM information_schema.ROUTINES "
                        f"WHERE ROUTINE_SCHEMA = DATABASE() AND ROUTINE_TYPE = '{kind}' "
                        "ORDER BY ROUTINE_NAME", deadline)
    if not rows:
        return b""

    database_name = quote_identifier(database)
    schema = query_schema(command, f"SHOW CREATE DATABASE {database_name}", deadline)
    create_database = schema[0]["Create Database"].replace("CREATE DATABASE ", "CREATE DATABASE IF NOT EXISTS ", 1)
    lines = [
        create_database + ";",
        f"USE {database_name};",
        "SET @backup_sql_mode = @@SESSION.sql_mode, @backup_client_charset = @@SESSION.character_set_client,"
        " @backup_connection_collation = @@SESSION.collation_connection;",
        "SET SESSION character_set_client = utf8mb4;",
    ]
    for row in rows:
        name = quote_identifier(row["ROUTINE_NAME"])
        routine = query_schema(command, f"SHOW CREATE {kind} {name}", deadline)
        definition = routine[0].get(f"Create {kind.title()}") if routine else None
        if not definition:
            raise BackupError(f"Usuário sem permissão para exportar a definição de {kind}.")
        mode = routine[0].get("sql_mode") or ""
        collation = routine[0].get("collation_connection") or ""
        if not re.fullmatch(r"[A-Z0-9_,]*", mode) or not re.fullmatch(r"[a-zA-Z0-9_]+", collation):
            raise BackupError(f"Metadados inválidos na definição de {kind}.")
        delimiter = ";;" + uuid4().hex
        while delimiter in definition:
            delimiter = ";;" + uuid4().hex
        lines.extend([
            f"SET SESSION sql_mode = '{mode}';",
            f"SET SESSION collation_connection = '{collation}';",
            f"DROP {kind} IF EXISTS {name};",
            f"DELIMITER {delimiter}",
            definition + "\n" + delimiter,
            "DELIMITER ;",
        ])
    lines.append("SET SESSION sql_mode = @backup_sql_mode, character_set_client = @backup_client_charset,"
                 " collation_connection = @backup_connection_collation;")
    return ("\n".join(lines) + "\n").encode("utf-8")


def make_archive(db, directory, output):
    try:
        credentials = json.loads(os.environ[db["credentials_env"]])
        database = credentials.pop("database")
        if not isinstance(database, str) or not database or database.startswith("-") or "\0" in database:
            raise ValueError
        allowed = {"host", "port", "user", "password", "socket", "ssl-ca"}
        if set(credentials) - allowed or not {"user", "password"} <= credentials.keys():
            raise ValueError
    except (ValueError, TypeError, AttributeError, KeyError):
        raise BackupError("Credenciais do banco ausentes ou inválidas.") from None
    tls_flags = dump_tls_flags(db["type"], credentials)
    options = directory / "client.cnf"
    settings = "".join(f"{key}={option_value(value)}\n" for key, value in credentials.items())
    options.write_text("[client]\n" + settings)
    options.chmod(0o600)
    client = "mysqldump" if db["type"] == "mysql" else "mariadb-dump"
    flags = ["--single-transaction", "--quick", "--hex-blob"]
    if db["type"] == "mysql":
        flags += ["--no-tablespaces", "--set-gtid-purged=OFF"]
    flags += tls_flags
    recipient = os.environ["AGE_RECIPIENT"].strip()
    if not re.fullmatch(r"age1[0-9a-z]+", recipient):
        raise BackupError("AGE_RECIPIENT deve conter uma chave pública age.")
    command = [client, f"--defaults-file={options}", *flags]
    deadline = time.monotonic() + 45 * 60

    for table in db.get("ignore_data", []) + db.get("ignore_table", []):
        probe = subprocess.run([*command, "--no-data", "--skip-triggers", database, table],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=max(0, deadline - time.monotonic()))
        if probe.returncode:
            raise BackupError(f"Não foi possível validar a tabela {table}. "
                              f"{dump_error(probe.stderr, probe.returncode)}")

    export_procedures = db.get("export_procedures", True)
    export_functions = db.get("export_functions", True)
    excluded_views = []
    routine_sql = b""
    if not db.get("export_views", True) or export_procedures != export_functions:
        sql_client = "mysql" if db["type"] == "mysql" else "mariadb"
        metadata_command = [sql_client, f"--defaults-file={options}", *tls_flags, "--xml",
                            "--default-character-set=utf8mb4", f"--database={database}"]
        if not db.get("export_views", True):
            views = query_schema(metadata_command, "SELECT TABLE_NAME FROM information_schema.TABLES "
                                 "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'VIEW'", deadline)
            excluded_views = [row["TABLE_NAME"] for row in views]
        if export_procedures != export_functions:
            kind = "PROCEDURE" if export_procedures else "FUNCTION"
            routine_sql = selected_routines(metadata_command, database, kind, deadline)

    with subprocess.Popen(["gzip", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as compressor:
        try:
            with subprocess.Popen(["age", "-r", recipient, "-o", str(output)],
                                  stdin=compressor.stdout, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) as encryptor:
                compressor.stdout.close()
                try:
                    if routine_sql:
                        compressor.stdin.write(routine_sql)
                        compressor.stdin.flush()
                    for label, dump in dump_sections(db, database, command, excluded_views):
                        result = subprocess.run(dump, stdout=compressor.stdin, stderr=subprocess.PIPE,
                                                timeout=max(0, deadline - time.monotonic()))
                        if result.returncode:
                            raise BackupError(f"{label}: {dump_error(result.stderr, result.returncode)}")
                    compressor.stdin.close()
                    for label, process in (("Criptografia", encryptor), ("Compactação", compressor)):
                        code = process.wait(timeout=max(0, deadline - time.monotonic()))
                        if code:
                            raise BackupError(f"{label} encerrou com código {code}.")
                finally:
                    if encryptor.poll() is None:
                        encryptor.kill()
        finally:
            if compressor.poll() is None:
                compressor.kill()


def slot_of(obj):
    return obj["Key"].rsplit("/", 1)[1].split("_")[1]


def display_stamp(value, zone):
    if not value:
        return "indisponível"
    return datetime.strptime(value, STAMP).replace(tzinfo=timezone.utc).astimezone(zone).strftime("%d/%m/%Y às %H:%M")


def run_backup(store, db_id, db, zone, slot):
    start = utcnow()
    name = db.get("name", db_id)
    status_key = f"status/{db_id}.json"
    channel = f"backup-{db_id}"
    kind = "manual" if slot == "manual" else "agendada"
    context = f"{name}\nExecução {kind} em {display_stamp(stamp(start), zone)}"
    key = f"backups/{db_id}/{stamp(start)}_{slot}_{uuid4().hex}.sql.gz.age"
    phase = "consulta ao armazenamento"
    last = None
    try:
        backups = store.backups(db_id, clean_incomplete=True)
        last = backups[0]["Key"].rsplit("/", 1)[1].split("_")[0] if backups else None
        completed_key = f"scheduled/{db_id}.json"
        limit_mb = db.get("max_storage_mb")
        limit = None if limit_mb is None else limit_mb * 1_000_000
        if slot != "manual":
            completed = store.read(completed_key).get("slot", "")
            if any(slot_of(obj) == slot for obj in backups):
                phase = "recuperação da rotação no R2"
                if backups and (limit is None or backups[0]["Size"] <= limit):
                    _, expired, _ = retention_plan(backups[1:], backups[0], db["keep"], limit)
                    for obj in reversed(expired):
                        store.remove_backup(obj["Key"])
                store.write(completed_key, {"slot": max(slot, completed)})
                return True
            if backups and completed >= slot:
                return True
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            archive = directory / "backup.sql.gz.age"
            phase = "exportação, compactação e criptografia"
            validate_table_filters(db_id, db)
            make_archive(db, directory, archive)
            size = archive.stat().st_size
            phase = "cota de armazenamento"
            retained, expired, limited = retention_plan(backups, {"Key": key, "Size": size}, db["keep"], limit)
            with archive.open("rb") as source:
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
            phase = "upload e verificação no R2"
            try:
                store.upload(archive, key, checksum)
                store.mark_verified(key, checksum)
                phase = "rotação no R2"
                for obj in reversed(expired):
                    store.remove_backup(obj["Key"])
            except Exception:
                try:
                    store.remove_backup(key)
                except Exception as rollback:
                    raise BackupError(f"Não foi possível remover a nova cópia após a falha. O uso da cota pode estar acima do limite. {describe(rollback)}") from None
                raise
    except Exception as error:
        reason = f"{phase}: {describe(error)}"
        finish(store, status_key, channel, {"at": stamp(start), "error": reason},
               f"❌ Falha no backup — {context}\nEtapa: {phase}\nMotivo: {describe(error)}\n"
               f"Último backup válido anterior: {display_stamp(last, zone)}")
        return False

    duration = round((utcnow() - start).total_seconds())
    message = f"✅ Backup concluído — {context}\nDestino: Cloudflare R2\nTamanho: {size:,} bytes\nDuração: {duration} s"
    if limited:
        usage = sum(obj["Size"] for obj in retained)
        message += (f"\n⚠️ Cota de {limit_mb} MB atingida. O espaço limitou a retenção a "
                    f"{len(retained)} de {db['keep']} cópias ({usage:,} bytes ocupados).")
    if slot != "manual":
        store.write(completed_key, {"slot": slot})
    finish(store, status_key, channel, {"at": stamp(start), "error": None}, message)
    return True


def finish(store, key, channel, status, message):
    try:
        store.write(key, status)
    finally:
        store.notify(channel, message)
    print(message, flush=True)


def monitor(store, databases, zone, now, snapshot):
    result = {}
    for db_id, db in databases.items():
        channel = f"monitor-{db_id}"
        try:
            store.retry(channel)
            backups = store.backups(db_id)
            latest = backups[0] if backups else None
            expected = due(db, zone, now - timedelta(minutes=db["grace_minutes"]))
            latest_scheduled = max((slot_of(obj) for obj in backups if slot_of(obj) != "manual"), default="")
            completed = store.read(f"scheduled/{db_id}.json").get("slot", "") if backups else ""
            overdue = max(latest_scheduled, completed) < expected
            alert_key = f"monitor/{db_id}.json"
            alert = store.read(alert_key)
            if overdue and alert.get("slot") != expected:
                name = db.get("name", db_id)
                store.notify(channel, f"⚠️ Backup atrasado — {name}\n"
                             f"Execução prevista em {display_stamp(expected, zone)}\n"
                             f"Tolerância: {db['grace_minutes']} min")
                store.write(alert_key, {"slot": expected})
            elif not overdue and alert:
                store.delete(alert_key)
            result[db_id] = {
                "latest": {"key": latest["Key"], "bytes": latest["Size"],
                           "sha256": store.metadata(latest["Key"]).get("sha256")} if latest else None,
                "last_attempt": store.read(f"status/{db_id}.json"),
                "retained": len(backups),
                "bytes": sum(obj["Size"] for obj in backups),
                "overdue": overdue,
            }
        except Exception as error:
            result[db_id] = {"monitor_error": describe(error)}
    Path(snapshot).write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return all("monitor_error" not in entry for entry in result.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="databases.yml")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--executor", required=True)
    run.add_argument("--database", help="Força um backup manual do banco informado")
    check = commands.add_parser("monitor")
    check.add_argument("--snapshot", required=True)
    commands.add_parser("validate")
    args = parser.parse_args()
    try:
        zone, databases = load_config(args.config, validate_filters=args.command == "validate")
        if args.command == "validate":
            return 0
        if args.command == "monitor":
            return 0 if monitor(Storage(), databases, zone, utcnow(), args.snapshot) else 1
        selected = {key: db for key, db in databases.items() if db["executor"] == args.executor}
        if args.database:
            if args.database not in selected:
                raise BackupError("Banco inexistente ou associado a outro executor.")
            selected = {args.database: selected[args.database]}
        if not selected:
            return 0
        lock_dir = Path(os.getenv("BACKUP_LOCK_DIR", str(Path.home() / ".db-backups")))
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        store = Storage()
        now = utcnow()
        success = True
        for db_id, db in selected.items():
            with (lock_dir / f"{db_id}.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print(f"{db_id}: já em execução.", flush=True)
                    success = not args.database and success
                    continue
                try:
                    store.retry(f"backup-{db_id}")
                    slot = "manual" if args.database else due(db, zone, now)
                    success = run_backup(store, db_id, db, zone, slot) and success
                except Exception as error:
                    print(f"{db_id}: {describe(error)}", flush=True)
                    success = False
        return 0 if success else 1
    except Exception as error:
        print(describe(error), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
