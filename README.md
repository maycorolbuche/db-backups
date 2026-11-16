# db-backups

Backup lógico de MySQL e MariaDB por banco, com arquivos compactados e criptografados em um bucket privado do Cloudflare R2. O Cloudflare Cron dispara o workflow do GitHub Actions, que executa os bancos acessíveis a partir do runner e monitora também os que rodam pelo cron do cPanel.

## Configuração inicial

1. Crie um bucket privado no R2 dedicado aos backups e um token S3 restrito a esse bucket com leitura e escrita de objetos. A rotina precisa listar, verificar e excluir objetos para aplicar a retenção.
2. Em um computador de confiança, instale `age` e execute `age-keygen -o recovery.agekey`. Guarde a chave privada em pelo menos dois locais seguros e independentes da hospedagem e do GitHub. Configure somente a chave pública, iniciada por `age1`, nos executores.
3. No GitHub, em **Settings → Secrets and variables → Actions**, configure:

| Tipo | Nome | Valor |
|---|---|---|
| Variable | `R2_ACCOUNT_ID` | ID da conta Cloudflare |
| Variable | `R2_BUCKET` | Nome do bucket privado |
| Variable | `AGE_RECIPIENT` | Chave pública do age |
| Secret | `R2_ACCESS_KEY_ID` | Chave de acesso R2 |
| Secret | `R2_SECRET_ACCESS_KEY` | Segredo R2 |
| Secret | `TELEGRAM_BOT_TOKEN` | Token do bot |
| Secret | `TELEGRAM_CHAT_ID` | ID do grupo |
| Secret opcional | `TELEGRAM_MESSAGE_THREAD_ID` | ID do tópico. Vazio envia para o grupo. |
| Secret | `DB_CREDENTIALS` | Credenciais do banco de exemplo. Para outros bancos, adicione um Secret por banco e mapeie no bloco `env` de `.github/workflows/backups.yml`. |

O JSON de conexão tem o formato abaixo. Use uma conta dedicada com os privilégios necessários para ler dados e exportar views, rotinas, eventos e gatilhos. Para verificar o certificado de uma conexão remota, configure `ssl-ca` com o caminho da CA. No Actions, o arquivo público da CA pode ser distribuído no repositório ou use o bundle de CA do cliente quando adequado. Os nomes e credenciais reais ficam na variável de ambiente indicada em `credentials_env`.

```json
{"host":"db.exemplo.com","port":3306,"database":"app","user":"backup","password":"substituir","ssl-ca":"certs/mysql-ca.pem"}
```

Para MySQL remoto, omita `ssl-ca` para usar TLS obrigatório (`REQUIRED`) sem verificar a identidade do servidor. Com `ssl-ca` preenchido, o cliente usa `VERIFY_IDENTITY` para verificar a CA e o hostname. Para MariaDB remoto, informe `ssl-ca`.

Para conexão local via socket, omita `ssl-ca` e use `host: localhost` e, se necessário, o campo `socket` no JSON. `type` seleciona o cliente do dump. O snapshot transacional pressupõe tabelas transacionais, como InnoDB. Para tabelas não transacionais ou alterações de esquema concorrentes, defina uma janela adequada e valide a restauração.

## Cadastro de bancos

Use [databases.example.yml](databases.example.yml) como modelo. O cadastro ativo fica em `databases.yml` e começa vazio.

- `schedule` usa cron de cinco campos no fuso definido. O Cloudflare Cron dispara o Actions a cada dez minutos e o workflow consulta quais bancos estão no horário de backup. O início do job depende da fila do GitHub. Cada execução considera a janela mais recente vencida. Uma interrupção longa gera um backup atual quando o executor voltar, pois o dump não consegue reconstruir estados passados.
- `executor` vincula cada banco a um único executor. Use um nome diferente para cada servidor cPanel. No Actions, os jobs de backup compartilham o mesmo grupo de concorrência. No cPanel, mantenha o mesmo `BACKUP_LOCK_DIR` para as execuções manuais e automáticas do servidor.
- `keep` define a quantidade máxima. `max_storage_mb` é opcional e usa MB decimal. A cota conta os arquivos de backup criptografados daquele banco. O uso pode subir temporariamente durante o upload e a verificação. Se o backup sozinho exceder a cota, ele é rejeitado antes do upload. Se couber sozinho, os arquivos mais antigos são removidos até satisfazer os dois limites.
- Use um prefixo gravável e sem bloqueio de exclusão para `backups/`. Se uma exclusão falhar, a rotina tenta desfazer o novo upload e alerta. Uma trava que impeça a exclusão da cópia recém-enviada pode deixar o uso acima da cota até a regularização. Para retenção imutável, use uma segunda cópia com credenciais independentes.
- `ignore_data` e `ignore_table` são listas opcionais por banco. A primeira preserva a estrutura (incluindo índices, constraints e gatilhos) e omite as linhas originais. A segunda exclui a tabela inteira. Use nomes exatos, sem o prefixo do banco. Tabelas repetidas, sobreposição entre listas e tabelas que não puderem ser validadas causam falha do backup. As listas vazias equivalem ao backup integral.
- Antes de excluir uma tabela ou seus dados, confira as dependências de chaves estrangeiras, views, rotinas e gatilhos. O dump preserva as definições dependentes e elas podem exigir a tabela ou os dados excluídos.
- Em **Actions → Backup de bancos → Run workflow**, informe o identificador para backup manual de um banco do Actions. No cPanel, acrescente `--database identificador` ao comando do executor. Um backup manual não muda a agenda e conta para a retenção do banco.

## Agendamento no Cloudflare

Após o merge na branch `main`, crie um token fine-grained no GitHub. Em **Repository access**, selecione somente `maycorolbuche/db-backups`. Em **Repository permissions**, conceda **Actions → Read and write**, suficiente para disparar o workflow. Guarde esse token como secret do Worker.

A partir da raiz do repositório, autenticado no Wrangler:

```bash
npx wrangler deploy --config cloudflare/wrangler.toml
npx wrangler secret put GITHUB_DISPATCH_TOKEN --config cloudflare/wrangler.toml
npx wrangler tail --config cloudflare/wrangler.toml
```

O primeiro comando publica o Worker `db-backups-cron` e configura o Cron Trigger. O Worker não tem URL pública e usa a API do GitHub para disparar `backups.yml` na `main`. Confirme a mensagem de disparo nos logs e abra **Actions → Backup de bancos** para verificar a execução. A confirmação no Cloudflare indica que o GitHub aceitou o disparo. O resultado do backup é registrado no Actions e no Telegram.

O cron do Worker roda em UTC. O horário de cada banco continua sendo calculado com o fuso do `databases.yml`. Para pausar o agendamento, configure `crons = []` no Wrangler e publique novamente. Os disparos manuais continuam disponíveis no Actions.

## Execução pelo cPanel

Instale ou disponibilize na conta Python 3.12, `gzip`, `age` e o cliente compatível (`mysqldump` ou `mariadb-dump`). O projeto e os arquivos de configuração devem ficar fora de `public_html`. Em hospedagens sem SSH, é possível fazer o upload pelo Gerenciador de Arquivos e usar Cron Jobs para executar os comandos após conferir os executáveis disponíveis no plano.

Crie o ambiente Python e instale as dependências uma vez:

```bash
python3.12 -m venv /home/USUARIO/db-backups/.venv
/home/USUARIO/db-backups/.venv/bin/python -m pip install -r /home/USUARIO/db-backups/requirements.txt
```

Crie `/home/USUARIO/db-backups.env` com permissão `0600`. Use atribuições compatíveis com Bash e inclua as mesmas variáveis R2, age e Telegram da tabela, além do JSON de cada banco daquele servidor. Adicione em Cron Jobs, ajustando os caminhos e o executor:

```cron
*/5 * * * * /bin/bash -c 'set -a; source /home/USUARIO/db-backups.env || exit 1; set +a; cd /home/USUARIO/db-backups && exec .venv/bin/python db_backups.py run --executor cpanel-principal' >> /home/USUARIO/db-backups.log 2>&1
```

Sincronize o `databases.yml` e o código com a versão aprovada no GitHub quando mudar o cadastro. O Actions usa o mesmo cadastro para monitorar os bancos do cPanel. A branch `state` contém um único commit com `snapshot.json`, incluindo a última cópia verificada, o uso da cota e o resultado da consulta. Uma mensagem de sucesso pode incluir o aviso de redução da quantidade por cota. Falhas de envio são reenfileiradas no R2 quando ele está disponível. Uma entrega repetida é possível se o Telegram aceitar uma mensagem e a confirmação persistente falhar.

## Restauração

Em um computador autorizado, baixe do R2 o arquivo indicado no snapshot ou uma cópia anterior e obtenha o SHA-256 nos metadados `sha256` do objeto. Confirme o hash antes de descriptografar. O dump inclui o nome original do banco. Execute a restauração inicial em um **servidor descartável ou isolado** com cliente compatível com a origem e revisão das rotinas/eventos antes de aplicar em produção.

```bash
set -o pipefail
printf '%s  %s\n' "$SHA256" backup.sql.gz.age | sha256sum --check - &&
  age --decrypt -i recovery.agekey backup.sql.gz.age |
  gzip -dc |
  mysql --defaults-file=/caminho/privado/restore.cnf
```

Para MariaDB, use o cliente `mariadb`. Confira tabelas, dados e objetos de esquema após importar. Repita o exercício periodicamente e mantenha uma cópia separada para os bancos críticos.

## Validação local

```bash
python -m pip install -r requirements.txt
python db_backups.py validate
python -m unittest discover -s tests -v
node --test tests/test_scheduler.mjs
```
