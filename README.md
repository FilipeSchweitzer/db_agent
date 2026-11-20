# migra-agent

Pipeline de IA para análise de bancos legados e geração assistida de
scripts de migração para o sistema de gestão municipal.

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
│   ├── main.py               # CLI (discover / map / generate)
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

## Como rodar (piloto)

```bash
cp .env.example .env
# escolher o backend de LLM (ver "Backends de LLM" abaixo)
# criar config/<seu-cliente>/project.yaml e target_schema.yaml (ver example-client/)

docker compose up -d --build

docker compose exec app python -m app.main discover example-client
docker compose exec app python -m app.main map example-client
# -> abrir output/example-client/mapeamento_para_revisao.xlsx,
#    revisar a coluna "status" (approved / edited / rejected), salvar

docker compose exec app python -m app.main generate example-client --dry-run
# -> só valida o mapeamento e mostra o plano; não escreve script executável

docker compose exec app python -m app.main generate example-client
```

O `generate` produz quatro arquivos SQL que devem ser executados nesta ordem:

| Arquivo | Conteúdo |
|---|---|
| `etl_schema.sql` | sequences + tabelas do destino |
| `etl_script.sql` | a carga, na ordem de dependência das FKs |
| `etl_constraints.sql` | FKs do destino (aplicadas depois da carga) |
| `etl_validation.sql` | contagens origem x destino e órfãos de FK |

## O que o SQL gerado garante

- **Ordem de dependência**: as tabelas são carregadas na ordem topológica do
  `target_schema.yaml`; ciclo de referência falha na geração, não na execução.
- **De-para de chaves**: quando a PK do destino é gerada (sequence), cada
  tabela ganha `_map_<tabela>` (chave antiga -> chave nova). FK entre tabelas
  migradas é traduzida por esse mapa — `pessoa.cod_bairro` aponta para o
  bairro certo, não para o código do legado.
- **Reexecutável**: o de-para preserva a chave nova e cada INSERT é
  idempotente por dialeto (`ON CONFLICT DO NOTHING`, `WHERE NOT EXISTS`,
  `UPDATE OR INSERT`). Rodar o script duas vezes não duplica dados.
- **Transação por tabela**: uma falha no meio da carga não deixa a tabela
  anterior inconsistente; o `COMMIT` é por tabela, não no fim de tudo.
- **FK só depois da carga**: validar FK linha a linha durante o `INSERT`
  custa muito mais caro do que conferir o resultado no final.
- **FK órfã não passa em silêncio**: se o legado referenciar um registro que não
  foi migrado, a carga grava `NULL` (LEFT JOIN no de-para). `etl_validation.sql`
  conta essas ocorrências na origem — é a única forma de enxergar o problema
  depois que a coluna já perdeu o valor.

`etl_script.sql` assume que origem e destino são alcançáveis pela mesma
conexão (schema anexado / linked server). Se não forem, o script serve como
especificação para um executor em lote — que é o próximo passo natural para
volumes grandes.

No Firebird 4+ a transformação `regex_extract_digits` usa `REGEXP_REPLACE`.
No SQL Server ela chama `dbo.fn_strip_nondigits`, que precisa existir no banco
de **origem**; o `relatorio.md` marca esse pré-requisito explicitamente.

## Ajustes de ETL (`project.yaml` -> `etl`)

| Chave | Padrão | Quando mudar |
|---|---|---|
| `create_schema` | `true` | `false` quando as tabelas/sequences do destino são gerenciadas por outro time: `etl_schema.sql` só lista o que a carga assume. |
| `idempotent` | `true` | `false` só para carga única em base vazia (perde a proteção contra duplicar). |
| `pk_strategy` | por tabela | `preserve` mantém a PK da origem em vez de gerar pela sequence — útil para tabelas de domínio pequenas. |
| `sequence_pattern` | `seq_{table}` | quando a convenção do destino é outra; com `create_schema: false`, é o nome que precisa já existir. |
| `allow_unresolved_fks` | `false` | `true` aceita FK para tabela não migrada (copia a chave antiga sem tradução). O padrão falha alto. |
| `high_volume_rows` | `100000` | limiar para o relatório avisar que a carga em uma transação por tabela ficou pesada (lote é Fase 2b). |

Uma tabela cuja PK não tem origem aprovada não ganha de-para: o banco gera a
chave e o `relatorio.md` avisa. Se alguma FK apontar para ela, a geração falha —
não existe chave nova para traduzir.

## Backends de LLM

Não existe backend obrigatório. A escolha é só `LLM_BACKEND` no `.env`:

| `LLM_BACKEND` | O que usa | Configuração |
|---|---|---|
| `openai` (padrão) | qualquer endpoint compatível com a API OpenAI — OpenAI, OpenRouter, vLLM, LM Studio, Ollama | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` |
| `ollama` | atalho para o Ollama local (`local` também é aceito) | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |
| `anthropic` | opcional — exige `pip install "migra-agent[anthropic]"` | `ANTHROPIC_API_KEY` |

O backend `openai` com `LLM_BASE_URL` vazio fala com a OpenAI oficial; com
`LLM_BASE_URL` preenchido fala com qualquer servidor compatível.

**Exceção que não é sugestão:** quando o discovery encontra PII, só backend
local é aceito — ver "Privacidade e LGPD" abaixo.

Todos os backends passam pela mesma camada de robustez (`app/llm/client.py`):
parse tolerante, validação Pydantic, até `LLM_MAX_ATTEMPTS` tentativas com
backoff exponencial, e **o erro de validação volta no prompt** para o modelo
corrigir a própria resposta.

## Privacidade e LGPD (Fase 4)

O pipeline parte de uma regra: **o LLM precisa do padrão, não do valor**.

- **Máscara antes de enviar.** `top_values` de toda coluna vira forma
  (`###.###.###-##`, `DD/MM/AAAA`, `<nome>`, `<endereço>`, `AA` para UF...).
  A máscara é aplicada duas vezes: ao montar o prompt do Agente 1 e ao
  gravar/reler o `discovery_report.json`. Notas e riscos também passam por
  uma varredura que troca CPF/CNPJ/e-mail/telefone/data reconhecíveis.
- **PII não sai da rede.** Se o perfilamento encontra indício de dado pessoal
  (nome de coluna ou forma do valor), o backend de LLM precisa ser local:
  `LLM_BACKEND=local`/`ollama`, ou `openai` com `LLM_BASE_URL` em loopback,
  IP privado, nome de serviço (`ollama`) ou domínio interno. Um backend
  remoto **interrompe** a execução antes da primeira chamada. Sem PII,
  nenhum backend é exigido — o invariante da Fase 1 continua valendo.
- **Trilha de auditoria.** Cada `discover`/`map`/`generate` acrescenta uma
  entrada em `auditoria.json`: `run_id`, horários, usuário/host, aprovador,
  versão do `target_schema`, SHA-256 do `mapping.json`/estado revisado e dos
  artefatos, além do resumo de PII (colunas, backend, se é local).
- **Versão do schema vale de verdade.** `mapping.json` guarda a versão do
  `target_schema.yaml` contra a qual foi proposto. Se o YAML mudar antes do
  `generate`, a geração se recusa a rodar (`--force` assume o risco e o
  registro sai com `compatible: false`).
- **Aprovador nomeado.** `generate --approved-by "nome"`, ou `MIGRA_APPROVER`
  no ambiente; sem nenhum dos dois, usa o usuário do sistema. O nome fica no
  `auditoria.json` e no `relatorio.md`.

## Avaliação com golden set (Fase 5)

Mudar prompt sem medir é chute. O harness compara o Agente 2 com um
**mapeamento humano congelado** e devolve precisão, recall e onde errou — campo
a campo.

Um caso é uma pasta com três arquivos:

```
eval/<caso>/
├── golden.yaml            # a verdade: o que cada campo deve ter como origem
├── target_schema.yaml     # schema de destino usado no caso
└── discovery_report.json  # perfilamento congelado (a avaliação não abre banco)
```

Congelar o discovery é o que torna o número comparável: duas execuções do
mesmo caso medem prompt/agente, não a variação do banco.

```bash
# 1. criar o rascunho a partir de um `map` real (discovery já sanitizado)
docker compose exec app python -m app.main eval-init caso-cliente --project example-client

# 2. revisar CADA linha de eval/caso-cliente/golden.yaml contra a migração
#    manual — é a revisão humana que transforma o rascunho em golden set

# 3. medir: offline compara um mapping.json existente (custo zero, sem LLM)
docker compose exec app python -m app.main evaluate caso-cliente \
    --mapping output/example-client/mapping.json

# ou live: roda o Agente 2 contra o discovery congelado
docker compose exec app python -m app.main evaluate caso-cliente --repeats 3
```

Relatórios em `output/eval/<caso>/eval_report.json` e `.md`. O modo live passa
pela mesma política de PII do `map` (PII + backend remoto = nenhuma chamada).

### Regras do `golden.yaml`

| Campo | Efeito |
|---|---|
| `source_table`/`source_field` nulos | o campo **deve** ficar sem origem |
| `transformation` ausente | não avalia transformação |
| `transformation: null` | deve sair sem transformação |
| `discovery` | opcional; preencha `inferred_type`/`pii` para medir o Agente 1 |

O `golden.yaml` tem que listar **todos** os campos do `target_schema.yaml` —
inclusive os que devem ficar sem origem. Caso incompleto falha na carga, em vez
de transformar "não avaliei" em "acertou".

### O que é medido

- **Mapeamento:** precisão/recall/F1 por campo de destino; "mapeou errado" conta
  como falso positivo e falso negativo ao mesmo tempo. Acerto exato, acerto em
  deixar sem origem, por tabela, e acerto de transformação.
- **Estabilidade:** com `--repeats N`, média/desvio das métricas e a fração de
  campos em que todas as execuções concordaram.
- **Discovery:** acurácia de tipo por família (`varchar` ≡ `string`,
  `int` ≡ `integer`) e precisão/recall da flag de PII. **Falso negativo de PII**
  aparece destacado: é o erro caro.

`eval/_example/` é uma fixture sintética de formato — serve para exercitar o
harness no CI, não como benchmark. Números que valem vêm de migrações reais.

Casos de cliente podem ficar **fora do repositório** (`EVAL_ROOT=/caminho`),
para nome de tabela e de cliente não vazarem; o `.gitignore` já cobre
`output/`.

## Rodando com modelo local (Ollama)

```bash
# no .env: LLM_BACKEND=ollama
docker compose --profile local up -d --build
docker compose exec ollama ollama pull qwen2.5:14b-instruct-q4_K_M
```

## Checkpoint humano

O comando `generate` só processa entradas com `status` em
`approved`/`edited` na planilha. Nada é movido para um script executável
sem essa revisão — ver `app/review/export.py` e `app/agents/agent3_etl.py`.

Além disso, `map` grava `approval.json` com o SHA-256 do `mapping.json`. Se o
`mapping.json` for regerado depois da revisão, o `generate` se recusa a rodar
(use `--force` para assumir o risco): o script sairia diferente do que foi
aprovado.

A aprovação também fica nominal: `generate --approved-by "analista"` (ou
`MIGRA_APPROVER`) registra quem aprovou em `auditoria.json` e no
`relatorio.md`, junto com a versão do `target_schema` usada no mapeamento.

## Artefatos gerados em `output/<cliente>/`

| Arquivo | Quando | Conteúdo |
|---|---|---|
| `discovery_report.json` | `discover` | perfil de cada tabela/coluna |
| `mapping.json` | `map` | `MappingDocument` completo |
| `mapeamento_para_revisao.xlsx` | `map` | planilha para revisão humana |
| `approval.json` | `map` | hash do mapeamento que foi revisado |
| `etl_plan.json` | `generate` | o que será migrado, em que ordem e com qual chave |
| `etl_schema.sql` | `generate` | sequences e tabelas do destino |
| `etl_script.sql` | `generate` | a carga |
| `etl_constraints.sql` | `generate` | FKs do destino |
| `etl_validation.sql` | `generate` | contagens e órfãos |
| `relatorio.md` | `generate` | aprovados, pendentes, avisos e ordem de execução |
| `auditoria.json` | toda fase | quem executou/aprovou, quando, versões, hashes e PII |
| `run_log.json` | toda fase | tokens, latência e tentativas por chamada de LLM |

`run_log.json` é cumulativo: cada execução de `discover`/`map`/`generate`
acrescenta uma entrada em `runs`, com o modelo usado e o custo de cada
chamada — inclusive as tentativas que falharam. A fase `generate` registra
também o hash do mapeamento, o hash do estado revisado e a lista de artefatos.

## Adicionando um novo cliente

1. Criar `config/<nome-cliente>/project.yaml` e `target_schema.yaml`
2. Rodar `discover` → `map` → revisar planilha → `generate`

Nenhum código muda entre clientes — só a pasta `config/<nome>/`.

## Adicionando um novo dialeto de banco

1. `app/db/connectors.py` — adicionar driver em `_DRIVER_BY_DIALECT`
2. `app/transforms/registry.py` — adicionar template SQL do novo dialeto
   nas transformações relevantes
3. `app/etl/dialects.py` — tipo, sequence, insert idempotente e transação
   do dialeto como **destino**

## Desenvolvimento

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

O CI roda exatamente esses dois comandos em cada push
(`.github/workflows/ci.yml`), com um Postgres 16 de serviço: os testes de
integração (marcados `integration`) só rodam quando
`MIGRA_TEST_POSTGRES_DSN` está definido. Localmente eles são pulados:

```bash
docker run -d --name migra-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16-alpine
MIGRA_TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest
```
