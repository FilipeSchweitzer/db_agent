# migra-agent

Pipeline de IA para análise de bancos legados e geração assistida de
scripts de migração para o sistema de gestão municipal.

---

## 📖 Qual documento ler?

| Perfil | Comece por aqui |
|--------|-----------------|
| **Usuário com conhecimento básico de informática** (sabe instalar programas, abrir terminal, editar arquivos de texto) | 👉 **[manual.md](manual.md)** — guia passo a passo completo: instalar Docker, clonar, configurar `.env`, criar `project.yaml`/`target_schema.yaml`, rodar as 3 fases, privacidade, avaliação, troubleshooting. |
| **Desenvolvedor / Engenheiro de dados / Usuário avançado** (já conhece Docker, YAML, CLI, SQL, CI/CD) | Continue lendo este **README.md** — referência técnica, arquitetura, comandos, backends, ETL, dialetos, desenvolvimento. |

---

## Estrutura

```
migra-agent/
├── docker-compose.yml       # app + ollama (opcional, perfil "local")
├── Dockerfile
├── pyproject.toml           # pacote, ruff, pytest
├── requirements.txt         # dependências instaladas pela imagem Docker
├── .env.example             # copiar para .env
├── config/
│   └── <cliente>/
│       ├── project.yaml         # conexões, dialeto, escopo — por cliente
│       └── target_schema.yaml   # schema do produto, versionado por release
├── app/
│   ├── main.py               # CLI (discover / map / generate / evaluate / eval-init)
│   ├── orchestrator.py        # amarra as 3 fases + checkpoint humano + run_log
│   ├── config.py
│   ├── llm/                   # interface trocável: openai-compatível ou Anthropic
│   ├── db/                    # conexão e introspecção multi-dialeto (Firebird/SQL Server/Postgres)
│   ├── schemas/                # contratos Pydantic entre as fases
│   ├── agents/                 # Agente 1 (discovery), Agente 2 (mapeamento), Agente 3 (ETL)
│   ├── etl/                    # plano de migração + SQL por dialeto
│   ├── privacy/                # mascaramento de PII, política de backend e auditoria
│   ├── evaluation/             # golden set, métricas e execução da avaliação
│   └── review/                 # planilha de revisão humana + aprovação por hash
├── eval/<caso>/                # golden set versionado (golden.yaml + schema + discovery)
├── output/<cliente>/           # gerado em runtime: relatórios, mapeamento, script final
└── tests/
```

---

## Como usar (avançado)

> Resumo para quem já domina o ambiente. Para detalhes didáticos, veja **[manual.md](manual.md)**.

### 1. Preparar ambiente

```bash
# 1. Clone e entre na pasta
git clone https://github.com/FilipeSchweitzer/db_agent.git
cd db_agent

# 2. Configure variáveis (copie e edite)
cp .env.example .env
# Mínimo no .env:
# LLM_BACKEND=ollama
# OLLAMA_BASE_URL=http://ollama:11434/v1
# OLLAMA_MODEL=qwen2.5:14b-instruct-q4_K_M
# OUTPUT_ROOT=/app/output
# CONFIG_ROOT=/app/config

# 3. Suba o stack (Ollama local incluído no perfil "local")
docker compose --profile local up -d --build
```

### 2. Criar config do cliente

```bash
cp -r config/example-client config/meu-cliente
# Edite os dois arquivos:
# config/meu-cliente/project.yaml      # conexões legacy/novo, escopo, ETL
# config/meu-cliente/target_schema.yaml # schema do banco novo (versionado!)
```

**`project.yaml` (resumo):**
```yaml
project: "meu-cliente"
source:
  dialect: "firebird"          # firebird | sqlserver | postgres
  host: "192.168.1.50"
  port: 3050
  database: "C:/dados/LEGADO.FDB"
  user: "SYSDBA"
  password: "masterkey"
  schema_name: null
target:
  dialect: "postgres"          # sempre postgres
  host: "192.168.1.100"
  port: 5432
  database: "municipal"
  user: "migra_agent"
  password: "senha_forte"
  schema_name: "public"
etl:
  batch_size: 5000
  truncate_before_load: true
  fk_deferrable: true
# scope_tables: ["CAD_PESSOA", "CAD_BAIRRO"]  # opcional
```

**`target_schema.yaml` (resumo):**
```yaml
version: "2026.1"
tables:
  pessoa:
    fields:
      id: {type: integer, required: true, primary_key: true}
      cpf: {type: string, format: cpf, required: true, pii: true, validator: validar_cpf}
      nome_completo: {type: string, required: true, pii: true}
      data_nascimento: {type: date, pii: true}
      cod_bairro: {type: integer, references: bairro.id}
  bairro:
    fields:
      id: {type: integer, required: true, primary_key: true}
      nome: {type: string, required: true}
      cod_municipio_ibge: {type: string}
```

> **Regra de ouro**: usuário do banco **antigo** = só `SELECT`; usuário do banco **novo** = `CREATE/INSERT/UPDATE/DELETE/ALTER` no schema destino.

### 3. Pipeline (3 comandos)

```bash
# 1. Descoberta — lê legado, detecta PII, mascara, salva discovery_report.json
docker compose exec app python -m app.main discover meu-cliente

# 2. Mapeamento — propõe de-para, gera planilha Excel para revisão
docker compose exec app python -m app.main map meu-cliente

# 3. Abra output/meu-cliente/mapeamento_para_revisao.xlsx
#    Edite a coluna "status": approved | edited | rejected
#    Salve o arquivo.

# 4. Geração — valida planilha + approval.json + versão do schema
docker compose exec app python -m app.main generate meu-cliente --dry-run
docker compose exec app python -m app.main generate meu-cliente
```

**Scripts gerados em `output/meu-cliente/` (rodar no Postgres destino nessa ordem):**
| Arquivo | Conteúdo |
|---------|----------|
| `etl_schema.sql` | `CREATE SEQUENCE` + `CREATE TABLE` |
| `etl_script.sql` | `INSERT` na ordem topológica das FKs |
| `etl_constraints.sql` | `ALTER TABLE ... ADD CONSTRAINT` (FKs) |
| `etl_validation.sql` | `count(*)` origem vs destino + órfãos de FK |

### 4. Avaliação com Golden Set (Fase 5)

Se você tem 2–3 migrações manuais anteriores com mapeamento correto conhecido:

```bash
# 1. Crie rascunho a partir de um map real
docker compose exec app python -m app.main eval-init caso-real --project meu-cliente

# 2. Edite eval/caso-real/golden.yaml conferindo CADA linha contra a migração manual
#    (source_table/source_field nulos = campo deve ficar sem origem)

# 3. Meça offline (custo zero, sem LLM)
docker compose exec app python -m app.main evaluate caso-real \
    --mapping output/meu-cliente/mapping.json

# Ou live (roda Agente 2, mede estabilidade)
docker compose exec app python -m app.main evaluate caso-real --repeats 3
```

Relatórios em `output/eval/<caso>/eval_report.json` e `.md`.

---

## O que o SQL gerado garante

- **Ordem de dependência**: tabelas carregadas na ordem topológica do `target_schema.yaml`; ciclo falha na geração, não na execução.
- **De-para de chaves**: PK gerada por sequence → tabela `_map_<tabela>` (chave antiga → nova). FK traduzida pelo mapa.
- **Reexecutável**: `INSERT` idempotente por dialeto (`ON CONFLICT DO NOTHING`, `WHERE NOT EXISTS`, `UPDATE OR INSERT`).
- **Transação por tabela**: falha no meio não deixa tabela anterior inconsistente.
- **FK só depois da carga**: validar linha a linha durante `INSERT` custa caro; `etl_validation.sql` conta órfãos no final.
- **FK órfã não passa em silêncio**: legado referenciando registro não migrado → grava `NULL` (LEFT JOIN no de-para); validação expõe a contagem.

> `etl_script.sql` assume origem e destino na mesma conexão (schema anexado / linked server). Se não forem, o script serve como especificação para executor em lote (próximo passo para volumes grandes).

---

## Ajustes de ETL (`project.yaml` -> `etl`)

| Chave | Padrão | Quando mudar |
|-------|--------|--------------|
| `create_schema` | `true` | `false` quando tabelas/sequences do destino são gerenciadas por outro time. |
| `idempotent` | `true` | `false` só para carga única em base vazia (perde proteção contra duplicar). |
| `pk_strategy` | por tabela | `preserve` mantém PK da origem em vez de gerar pela sequence (tabelas de domínio pequenas). |
| `sequence_pattern` | `seq_{table}` | convenção do destino diferente; com `create_schema: false`, é o nome que já deve existir. |
| `allow_unresolved_fks` | `false` | `true` aceita FK para tabela não migrada (copia chave antiga sem tradução). Padrão falha alto. |
| `high_volume_rows` | `100000` | limiar para aviso de carga pesada em uma transação por tabela (lote é Fase 2b). |

---

## Backends de LLM

Não existe backend obrigatório. A escolha é só `LLM_BACKEND` no `.env`:

| `LLM_BACKEND` | O que usa | Configuração |
|---|---|---|
| `openai` (padrão) | Qualquer endpoint compatível com API OpenAI — OpenAI, OpenRouter, vLLM, LM Studio, Ollama | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` |
| `ollama` | Atalho para Ollama local (`local` também aceito) | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |
| `anthropic` | Opcional — exige `pip install "migra-agent[anthropic]"` | `ANTHROPIC_API_KEY` |

O backend `openai` com `LLM_BASE_URL` vazio fala com a OpenAI oficial; preenchido fala com qualquer servidor compatível.

**Exceção que não é sugestão:** quando o discovery encontra PII, **só backend local é aceito** — ver "Privacidade e LGPD" abaixo.

Todos os backends passam pela mesma camada de robustez (`app/llm/client.py`): parse tolerante, validação Pydantic, até `LLM_MAX_ATTEMPTS` tentativas com backoff exponencial, e **o erro de validação volta no prompt** para o modelo corrigir a própria resposta.

---

## Privacidade e LGPD (Fase 4)

Regra: **o LLM precisa do padrão, não do valor**.

- **Máscara antes de enviar.** `top_values` de toda coluna vira forma (`###.###.###-##`, `DD/MM/AAAA`, `<nome>`, `<endereço>`, `AA` para UF...). A máscara é aplicada duas vezes: ao montar o prompt do Agente 1 e ao gravar/reler o `discovery_report.json`. Notas e riscos também passam por varredura que troca CPF/CNPJ/e-mail/telefone/data reconhecíveis.
- **PII não sai da rede.** Se o perfilamento encontra indício de dado pessoal (nome de coluna ou forma do valor), o backend de LLM precisa ser local: `LLM_BACKEND=local`/`ollama`, ou `openai` com `LLM_BASE_URL` em loopback, IP privado, nome de serviço (`ollama`) ou domínio interno. Um backend remoto **interrompe** a execução antes da primeira chamada. Sem PII, nenhum backend é exigido — o invariante da Fase 1 continua valendo.
- **Trilha de auditoria.** Cada `discover`/`map`/`generate` acrescenta entrada em `auditoria.json`: `run_id`, horários, usuário/host, aprovador, versão do `target_schema`, SHA-256 do `mapping.json`/estado revisado e dos artefatos, além do resumo de PII (colunas, backend, se é local).
- **Versão do schema vale de verdade.** `mapping.json` guarda a versão do `target_schema.yaml` contra a qual foi proposto. Se o YAML mudar antes do `generate`, a geração se recusa a rodar (`--force` assume o risco e o registro sai com `compatible: false`).
- **Aprovador nomeado.** `generate --approved-by "nome"`, ou `MIGRA_APPROVER` no ambiente; sem nenhum dos dois, usa o usuário do sistema. O nome fica no `auditoria.json` e no `relatorio.md`.

---

## Avaliação com golden set (Fase 5) — referência técnica

Mudar prompt sem medir é chute. O harness compara o Agente 2 com um **mapeamento humano congelado** e devolve precisão, recall e onde errou — campo a campo.

### Formato do caso (`eval/<caso>/`)

```
eval/<caso>/
├── golden.yaml            # a verdade: mapeamento manual campo a campo
├── target_schema.yaml     # schema de destino congelado (versão exata)
└── discovery_report.json  # perfilamento congelado (avaliação não abre banco)
```

Congelar o discovery é o que torna o número comparável: execuções repetidas medem prompt/agente, não variação do banco.

### Regras do `golden.yaml`

| Campo | Efeito |
|-------|--------|
| `source_table`/`source_field` nulos | o campo **deve** ficar sem origem |
| `transformation` ausente | não avalia transformação |
| `transformation: null` | deve sair sem transformação |
| `discovery` (opcional) | preencha `inferred_type`/`pii` para medir o Agente 1 |

O `golden.yaml` **deve listar todos os campos** do `target_schema.yaml` — inclusive os que devem ficar sem origem. Caso incompleto falha na carga.

### O que é medido

- **Mapeamento:** precisão/recall/F1 por campo de destino; "mapeou errado" = FP + FN simultâneos. Acerto exato, acerto em deixar sem origem, por tabela, acerto de transformação.
- **Estabilidade:** com `--repeats N`, média/desvio das métricas e fração de campos onde todas as execuções concordaram.
- **Discovery:** acurácia de tipo por família (`varchar` ≡ `string`, `int` ≡ `integer`) e precisão/recall da flag de PII. **Falso negativo de PII** destacado: é o erro caro.

`eval/_example/` é fixture sintética de formato (marcada `synthetic: true`) — serve para CI, não como benchmark. Números que valem vêm de migrações reais.

Casos de cliente podem ficar **fora do repositório** (`EVAL_ROOT=/caminho/privado`) para não vazar nomes de tabela/cliente; o `.gitignore` já cobre `output/`.

---

## Rodando com modelo local (Ollama)

```bash
# no .env: LLM_BACKEND=ollama
docker compose --profile local up -d --build
docker compose exec ollama ollama pull qwen2.5:14b-instruct-q4_K_M
```

---

## Checkpoint humano

O comando `generate` só processa entradas com `status` em `approved`/`edited` na planilha. Nada vira script executável sem essa revisão — ver `app/review/export.py` e `app/agents/agent3_etl.py`.

Além disso, `map` grava `approval.json` com o SHA-256 do `mapping.json`. Se o `mapping.json` for regerado depois da revisão, o `generate` se recusa a rodar (use `--force` para assumir o risco): o script sairia diferente do que foi aprovado.

A aprovação também fica nominal: `generate --approved-by "analista"` (ou `MIGRA_APPROVER`) registra quem aprovou em `auditoria.json` e no `relatorio.md`, junto com a versão do `target_schema` usada no mapeamento.

---

## Artefatos gerados em `output/<cliente>/`

| Arquivo | Quando | Conteúdo |
|---------|--------|----------|
| `discovery_report.json` | `discover` | perfil de cada tabela/coluna (mascarado) |
| `mapping.json` | `map` | `MappingDocument` completo |
| `mapeamento_para_revisao.xlsx` | `map` | planilha para revisão humana |
| `approval.json` | `map` | hash do mapeamento que foi revisado |
| `etl_plan.json` | `generate` | o que será migrado, ordem, chaves |
| `etl_schema.sql` | `generate` | sequences e tabelas do destino |
| `etl_script.sql` | `generate` | a carga (INSERTs) |
| `etl_constraints.sql` | `generate` | FKs do destino |
| `etl_validation.sql` | `generate` | contagens e órfãos |
| `relatorio.md` | `generate` | aprovados, pendentes, avisos, ordem |
| `auditoria.json` | toda fase | quem executou/aprovou, versões, hashes, PII |
| `run_log.json` | toda fase | tokens, latência, tentativas por chamada LLM |

`run_log.json` é cumulativo: cada execução de `discover`/`map`/`generate` acrescenta entrada em `runs`, com modelo e custo de cada chamada — inclusive tentativas que falharam. A fase `generate` registra também hash do mapeamento, hash do estado revisado e lista de artefatos.

---

## Adicionando um novo cliente

1. Criar `config/<nome-cliente>/project.yaml` e `target_schema.yaml`
2. Rodar `discover` → `map` → revisar planilha → `generate`

Nenhum código muda entre clientes — só a pasta `config/<nome>/`.

---

## Adicionando um novo dialeto de banco

1. `app/db/connectors.py` — adicionar driver em `_DRIVER_BY_DIALECT`
2. `app/transforms/registry.py` — adicionar template SQL do novo dialeto nas transformações relevantes
3. `app/etl/dialects.py` — tipo, sequence, insert idempotente e transação do dialeto como **destino**

---

## Desenvolvimento

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

O CI roda exatamente esses dois comandos em cada push (`.github/workflows/ci.yml`), com um Postgres 16 de serviço: os testes de integração (marcados `integration`) só rodam quando `MIGRA_TEST_POSTGRES_DSN` está definido. Localmente eles são pulados:

```bash
docker run -d --name migra-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16-alpine
MIGRA_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest
```

---

## Estado do projeto (resumo)

| Fase | Status | Entregável principal |
|------|--------|----------------------|
| 0. Higiene | ✅ | versionamento, CI, ruff/pytest |
| 1. LLM robusto | ✅ | retry, validação, erro no prompt, backends trocáveis |
| 2. ETL correto | ✅ | de-para de chaves, FK traduzida, idempotência, ordem topológica |
| 3. Multi-dialeto | ✅ | Firebird, SQL Server, Postgres (origem e destino) |
| 4. Governança/LGPD | ✅ | máscara PII, backend local forçado, auditoria, versão schema, aprovador |
| 5. Avaliação | 🔄 harness ✅ | golden set, métricas, offline/live, `--repeats`, CLI `evaluate`/`eval-init` |
| 5b. Golden set real | ⏳ pendente | popular `eval/` com 2–3 migrações manuais reais |
| 5c. UI de revisão | ⏳ pendente | substituir/complementar Excel |
| 5d. Discovery incremental | ⏳ pendente | cache, re-perfilamento seletivo |

> **Próximo passo prioritário**: popular o golden set com migrações reais do cliente (`eval-init` + revisão manual do `golden.yaml`). Sem isso, toda mudança de prompt é fé, não engenharia.