# Manual do migra-agent

> Guia passo a passo para quem tem conhecimento **básico de informática** (sabe instalar programas, abrir terminal, editar arquivos de texto).  
> Não precisa saber programar.

---

## 1. O que este projeto faz

O **migra-agent** ajuda a migrar dados de um **banco de dados antigo (legado)** — Firebird, SQL Server ou PostgreSQL — para o **banco novo do sistema de gestão municipal** (PostgreSQL).

Ele faz isso em 3 etapas:

| Etapa | Comando | O que acontece |
|-------|---------|----------------|
| **1. Descoberta** | `discover` | Lê o banco antigo, entende tabelas/colunas, detecta dados sensíveis (CPF, nome, etc.) e salva um relatório (`discovery_report.json`). |
| **2. Mapeamento** | `map` | Propõe como cada campo do banco novo vem do banco antigo. Gera uma planilha Excel (`mapeamento_para_revisao.xlsx`) para você conferir e aprovar. |
| **3. Geração** | `generate` | Depois que você aprova a planilha, gera **4 scripts SQL** prontos para rodar no banco novo (cria tabelas, carrega dados, cria chaves estrangeiras, valida). |

---

## 2. Pré-requisitos (instale uma vez)

| Ferramenta | Versão mínima | Como instalar |
|------------|---------------|---------------|
| **Docker** | 24+ | Baixe em <https://www.docker.com/products/docker-desktop/> (Windows/Mac) ou `sudo apt install docker.io docker-compose` (Linux). |
| **Git** | qualquer | <https://git-scm.com/downloads> |
| **Editor de texto** | — | VS Code (<https://code.visualstudio.com/>), Notepad++ ou até o Bloco de Notas. |

> **Teste**: abra o terminal (PowerShell no Windows, Terminal no Linux/Mac) e rode:
> ```bash
> docker --version
> git --version
> ```
> Deve aparecer a versão de cada um sem erro.

---

## 3. Baixar o projeto

```bash
# 1. Escolha uma pasta (ex.: Documentos)
cd ~/Documentos

# 2. Clone o repositório
git clone https://github.com/FilipeSchweitzer/db_agent.git

# 3. Entre na pasta
cd db_agent
```

---

## 4. Preparar o arquivo de configuração (`.env`)

O projeto usa variáveis de ambiente. Copie o exemplo e edite:

```bash
cp .env.example .env
```

Abra o `.env` no seu editor e preencha **o mínimo abaixo**:

```ini
# ─── Backend de LLM (obrigatório) ───
# Use "ollama" para rodar 100% local (privacidade total).
# Deixe vazio se for usar OpenAI / OpenRouter / vLLM remoto.
LLM_BACKEND=ollama

# Se usar Ollama local (padrão do docker-compose):
OLLAMA_BASE_URL=http://ollama:11434/v1
OLLAMA_MODEL=qwen2.5:14b-instruct-q4_K_M

# Se preferir OpenAI / OpenRouter (remoto):
# LLM_BACKEND=openai
# LLM_BASE_URL=https://api.openai.com/v1      # ou https://openrouter.ai/api/v1
# LLM_API_KEY=sk-...                          # sua chave
# LLM_MODEL=gpt-4o-mini

# ─── Onde o projeto grava os arquivos gerados ───
# No Docker, use /app/output (já mapeado no compose).
OUTPUT_ROOT=/app/output

# ─── Onde ficam as configs por cliente ───
CONFIG_ROOT=/app/config
```

> **Dica**: se não quiser mexer em `.env`, pode passar as variáveis na hora do comando:
> `LLM_BACKEND=ollama docker compose exec app python -m app.main discover cliente`

---

## 5. Subir o ambiente (Docker)

O `docker-compose.yml` já traz:

- **app** — o código Python do migra-agent
- **ollama** — LLM local (roda o modelo `qwen2.5:14b-instruct-q4_K_M`, ~9 GB)

```bash
# Primeira vez: baixa a imagem e o modelo (pode demorar 5–15 min)
docker compose --profile local up -d --build

# Verifique se subiu
docker compose ps
# Deve mostrar: app (Up), ollama (Up)
```

> **O modelo já vem pré-carregado** no build. Se quiser trocar, edite `OLLAMA_MODEL` no `.env` e rode:
> ```bash
> docker compose exec ollama ollama pull qwen2.5:14b-instruct-q4_K_M
> ```

---

## 6. Criar a configuração do seu cliente

Cada cliente (prefeitura, órgão) tem sua própria pasta em `config/`.

```bash
# 1. Copie o exemplo
cp -r config/example-client config/meu-cliente

# 2. Edite os dois arquivos
code config/meu-cliente/project.yaml      # ou use seu editor
code config/meu-cliente/target_schema.yaml
```

### 6.1 `project.yaml` — conexões e escopo

```yaml
project: "meu-cliente"              # nome interno (sem espaços)
source:                             # BANCO ANTIGO (legado)
  dialect: "firebird"               # opções: firebird, sqlserver, postgres
  host: "192.168.1.50"              # IP do servidor do banco antigo
  port: 3050                        # porta (Firebird=3050, SQL Server=1433, PG=5432)
  database: "C:/dados/LEGADO.FDB"   # caminho do arquivo .FDB (Firebird) ou nome do DB
  user: "SYSDBA"
  password: "masterkey"             # ⚠️ nunca comite senha real no git!
  schema_name: null                 # Firebird/SQL Server: deixe null

target:                             # BANCO NOVO (destino, sempre PostgreSQL)
  dialect: "postgres"
  host: "192.168.1.100"             # IP do servidor PostgreSQL novo
  port: 5432
  database: "municipal"
  user: "migra_agent"
  password: "senha_forte_aqui"
  schema_name: "public"

etl:
  batch_size: 5000                  # linhas por INSERT (ajuste se travar)
  truncate_before_load: true        # limpa tabelas antes de carregar
  fk_deferrable: true               # cria FKs como DEFERRABLE (mais rápido)

# Opcional: limitar quais tabelas do legado serão perfiladas
# scope_tables:
#   - "CAD_PESSOA"
#   - "CAD_BAIRRO"
```

> **Importante**: o usuário do banco **antigo** deve ter **apenas `SELECT`** nas tabelas do escopo.  
> O usuário do banco **novo** precisa de `CREATE`, `INSERT`, `UPDATE`, `DELETE`, `ALTER` no schema de destino.

### 6.2 `target_schema.yaml` — estrutura do banco novo

Este arquivo define **como o banco novo deve ser**. Ele já vem com um exemplo (pessoa, bairro, município).  
**Você deve adaptar** para o schema real do seu sistema de gestão.

```yaml
version: "2026.1"   # suba a versão quando mudar o schema

tables:
  pessoa:
    fields:
      id:
        type: integer
        required: true
        primary_key: true
      cpf:
        type: string
        format: cpf
        required: true
        pii: true              # dado sensível → mascara antes de mandar pra LLM
        validator: validar_cpf
      nome_completo:
        type: string
        required: true
        pii: true
      data_nascimento:
        type: date
        required: false
        pii: true
      cod_bairro:
        type: integer
        required: false
        references: bairro.id  # chave estrangeira → bairro.id

  bairro:
    fields:
      id:
        type: integer
        required: true
        primary_key: true
      nome:
        type: string
        required: true
      cod_municipio_ibge:
        type: string
        required: false
```

> **Campos principais**:
> - `type`: `integer`, `string`, `date`, `datetime`, `boolean`, `decimal`
> - `required`: `true` = NOT NULL
> - `primary_key`: `true` = PK da tabela
> - `references`: `tabela.campo` = cria FK automaticamente
> - `pii: true` = dado pessoal (CPF, nome, endereço, telefone, data nasc.) → **nunca sai da rede**
> - `format`: `cpf`, `cnpj`, `cep`, `email`, `phone` → validação extra
> - `validator`: nome de função SQL customizada (opcional)

---

## 7. Rodar o pipeline (3 comandos)

Todos os comandos rodam **dentro do container `app`**:

```bash
# 1. DESCOBERTA — lê o banco antigo, gera discovery_report.json
docker compose exec app python -m app.main discover meu-cliente

# 2. MAPEAMENTO — propõe de-para, gera planilha Excel
docker compose exec app python -m app.main map meu-cliente

# 3. Abra a planilha em output/meu-cliente/mapeamento_para_revisao.xlsx
#    - Coluna "status": mude para "approved" (aprovado), "edited" (você corrigiu) ou "rejected"
#    - Se editou source_table/source_field/transformation, salve o arquivo.

# 4. GERAÇÃO — valida a planilha e gera os 4 scripts SQL
docker compose exec app python -m app.main generate meu-cliente --dry-run
#    --dry-run só mostra o plano, não escreve scripts. Confira se faz sentido.

docker compose exec app python -m app.main generate meu-cliente
#    Agora sim: gera os .sql em output/meu-cliente/
```

### O que o `generate` produz (em `output/meu-cliente/`)

| Arquivo | Para que serve | Ordem de execução no banco novo |
|---------|----------------|----------------------------------|
| `etl_schema.sql` | `CREATE SEQUENCE` + `CREATE TABLE` | 1º |
| `etl_script.sql` | `INSERT` dos dados (ordem respeita FKs) | 2º |
| `etl_constraints.sql` | `ALTER TABLE ... ADD CONSTRAINT` (FKs) | 3º |
| `etl_validation.sql` | `SELECT count(*)` origem vs destino + órfãos de FK | 4º (confere se tudo migrou) |

> **Rode no banco novo (PostgreSQL) nessa ordem**:
> ```bash
> psql -h 192.168.1.100 -U migra_agent -d municipal -f output/meu-cliente/etl_schema.sql
> psql -h 192.168.1.100 -U migra_agent -d municipal -f output/meu-cliente/etl_script.sql
> psql -h 192.168.1.100 -U migra_agent -d municipal -f output/meu-cliente/etl_constraints.sql
> psql -h 192.168.1.100 -U migra_agent -d municipal -f output/meu-cliente/etl_validation.sql
> ```

---

## 8. Privacidade & LGPD (importante!)

- **Dados sensíveis (CPF, nome, endereço, telefone, data nasc.) NUNCA saem da sua rede.**
- Se o `discover` detectar PII, **o pipeline só roda com LLM local** (`LLM_BACKEND=ollama` ou `LLM_BASE_URL` em IP privado/localhost).
- Se você tentar rodar com OpenAI remoto tendo PII, o comando **para com erro** antes de qualquer chamada.
- O arquivo `auditoria.json` (em `output/meu-cliente/`) registra: quem rodou, quando, hash dos arquivos, quais colunas PII, qual backend foi usado.

---

## 9. Avaliar a qualidade do mapeamento (Fase 5)

Se você tem **2–3 migrações antigas já feitas à mão** (com o mapeamento correto conhecido), pode medir a precisão do agente:

```bash
# 1. Crie um "caso golden" a partir de uma migração real já feita
docker compose exec app python -m app.main eval-init caso-real --project meu-cliente

# 2. Edite eval/caso-real/golden.yaml conferindo CADA linha contra a migração manual
#    (source_table/source_field nulos = campo deve ficar sem origem)

# 3. Meça offline (sem gastar LLM) comparando um mapping.json já gerado
docker compose exec app python -m app.main evaluate caso-real \
    --mapping output/meu-cliente/mapping.json

# Ou meça live (roda o agente de novo, mede estabilidade com --repeats)
docker compose exec app python -m app.main evaluate caso-real --repeats 3
```

Relatórios em `output/eval/caso-real/eval_report.md` e `.json`.

---

## 10. Problemas comuns & soluções

| Sintoma | Causa provável | Solução |
|---------|----------------|---------|
| `connection refused` no `discover` | IP/porta/senha errados no `project.yaml` | Confira `source.host`, `port`, `database`, `user`, `password`. Teste com `telnet IP PORTA` ou DBeaver. |
| `PIIPolicyError: backend remoto` | Banco antigo tem CPF/nome e `.env` tem `LLM_BACKEND=openai` sem `LLM_BASE_URL` local | Use `LLM_BACKEND=ollama` ou aponte `LLM_BASE_URL` para `http://localhost:11434/v1` (ou IP da sua rede). |
| `mapping.json mudou desde a revisão` no `generate` | Alguém rodou `map` de novo depois que você editou a planilha | Rode `map` de novo, revise a planilha nova, depois `generate`. |
| `target_schema.yaml mudou` | Você editou o schema depois do `map` | Rode `map` de novo ou use `--force` (registra risco na auditoria). |
| `ollama` não responde / modelo não encontrado | Modelo não foi baixado | `docker compose exec ollama ollama pull qwen2.5:14b-instruct-q4_K_M` |
| Planilha Excel não abre / dá erro | LibreOffice/Excel não instalado ou arquivo corrompido | Instale LibreOffice grátis; ou use `pandas` no Python para ler. |
| `sqlglot` erro de dialeto no `generate` | `target.dialect` não é `postgres` | O banco novo **sempre** é PostgreSQL. `source.dialect` é que varia. |

---

## 11. Estrutura de pastas (resumo)

```
db_agent/
├── config/
│   └── meu-cliente/
│       ├── project.yaml          # conexões, escopo, ETL
│       └── target_schema.yaml    # schema do banco novo (versão!)
├── eval/
│   ├── _example/                 # fixture sintética (não apague)
│   └── caso-real/                # seus golden sets (um por migração real)
│       ├── golden.yaml           # a verdade (mapeamento manual)
│       ├── target_schema.yaml    # schema congelado
│       └── discovery_report.json # discovery congelado
├── output/
│   └── meu-cliente/              # gerado em runtime (gitignored)
│       ├── discovery_report.json
│       ├── mapping.json
│       ├── mapeamento_para_revisao.xlsx   # VOCÊ EDITA ESTA
│       ├── approval.json         # prova de qual mapping foi revisado
│       ├── auditoria.json        # trilha LGPD
│       ├── run_log.json          # log de cada fase (tokens, tempo)
│       ├── etl_schema.sql
│       ├── etl_script.sql
│       ├── etl_constraints.sql
│       ├── etl_validation.sql
│       └── relatorio.md
├── app/                          # código fonte (não mexa)
├── docker-compose.yml
├── Dockerfile
├── .env                          # SUAS variáveis (não comite!)
└── README.md                     # documentação técnica
```

---

## 12. Atualizando o projeto

```bash
cd ~/Documentos/db_agent
git pull
docker compose --profile local up -d --build   # reconstrói se mudou Dockerfile/requirements
```

---

## 13. Onde pedir ajuda

- **Issues no GitHub**: <https://github.com/FilipeSchweitzer/db_agent/issues>
- Leia o `README.md` para detalhes técnicos avançados.
- O `resumo.md` tem o histórico de decisões e roadmap.

---

## 14. Checklist rápido antes de migrar em produção

- [ ] `.env` configurado com `LLM_BACKEND=ollama` (ou endpoint local)
- [ ] `config/meu-cliente/project.yaml` com IPs, portas, usuários, senhas corretos
- [ ] Usuário do banco **antigo** tem só `SELECT` nas tabelas do escopo
- [ ] Usuário do banco **novo** tem permissões de DDL/DML no schema destino
- [ ] `target_schema.yaml` reflete **exatamente** o schema do sistema novo
- [ ] Rodei `discover` → `map` → abri a planilha → conferi **todas** linhas → salvei
- [ ] Rodei `generate --dry-run` e o plano fez sentido
- [ ] Tenho backup do banco novo antes de rodar os scripts
- [ ] Rodei os 4 scripts na ordem e `etl_validation.sql` deu tudo OK

---

**Pronto!** Com este manual, qualquer pessoa com conhecimento básico de informática consegue instalar, configurar e rodar uma migração completa com o migra-agent. Boa migração! 🚀