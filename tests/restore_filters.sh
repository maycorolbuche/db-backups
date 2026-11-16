#!/usr/bin/env bash
set -euo pipefail

kind=${1:?mysql ou mariadb}
case "$kind" in
  mysql) image=mysql:8.4; cli=mysql; dump=mysqldump ;;
  mariadb) image=mariadb:11.4; cli=mariadb; dump=mariadb-dump ;;
  *) exit 2 ;;
esac
directory=$(mktemp -d)
container=""
cleanup() {
  if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi
  rm -rf "$directory"
}
trap cleanup EXIT
password=disposable-fixture
container=$(docker run -d -p 127.0.0.1::3306 \
  -e MYSQL_ROOT_PASSWORD="$password" -e MYSQL_ROOT_HOST=% "$image")
sql() { docker exec -i "$container" "$cli" -h 127.0.0.1 -uroot "-p$password" "$@"; }
ready=0
for i in {1..60}; do
  if sql -e 'SELECT 1' >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
if [[ "$ready" == 0 ]]; then docker logs "$container"; exit 1; fi
port=$(docker port "$container" 3306/tcp)
port=${port##*:}

sql <<'SQL'
CREATE DATABASE fixture;
USE fixture;
CREATE TABLE orders (id INT PRIMARY KEY);
CREATE TABLE logs (id INT PRIMARY KEY);
CREATE TABLE cache (id INT PRIMARY KEY);
CREATE TRIGGER log_order AFTER INSERT ON orders FOR EACH ROW INSERT INTO logs VALUES (NEW.id);
INSERT INTO orders VALUES (1);
INSERT INTO cache VALUES (1);
SQL

cat > "$directory/$dump" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
options=${1#--defaults-file=}
config_dir=$(dirname "$options")
exec docker run --rm --network host --volume "$config_dir:$config_dir:ro" \
  --entrypoint "$DUMP_CLIENT" "$DUMP_IMAGE" "$@"
SH
chmod +x "$directory/$dump"
export PATH="$directory:$PATH" DUMP_CLIENT="$dump" DUMP_IMAGE="$image"
age-keygen -o "$directory/recovery.agekey"
AGE_RECIPIENT=$(age-keygen -y "$directory/recovery.agekey")
export AGE_RECIPIENT
export FIXTURE_DB="{\"host\":\"127.0.0.1\",\"port\":$port,\"user\":\"root\",\"password\":\"$password\",\"database\":\"fixture\"}"
python3 - "$kind" "$directory" <<'PY'
import sys
from pathlib import Path

from db_backups import BackupError, make_archive

kind, path = sys.argv[1:]
directory = Path(path)
config = {
    "type": kind, "credentials_env": "FIXTURE_DB",
    "ignore_data": ["logs"], "ignore_table": ["cache"],
}
try:
    make_archive(config | {"ignore_table": ["missing_table"]}, directory, directory / "invalid.age")
except BackupError as error:
    assert "missing_table" in str(error), error
else:
    raise AssertionError("A tabela ausente deveria invalidar o backup.")
make_archive(config, directory, directory / "backup.age")
PY

sql -e 'DROP DATABASE fixture'
age --decrypt -i "$directory/recovery.agekey" "$directory/backup.age" | gzip -dc | sql
[[ "$(sql -N -e 'SELECT id FROM fixture.orders')" == 1 ]]
[[ "$(sql -N -e 'SELECT COUNT(*) FROM fixture.logs')" == 0 ]]
[[ -z "$(sql -N -e "SHOW TABLES FROM fixture LIKE 'cache'")" ]]
sql -e 'INSERT INTO fixture.orders VALUES (2)'
[[ "$(sql -N -e 'SELECT id FROM fixture.logs')" == 2 ]]
