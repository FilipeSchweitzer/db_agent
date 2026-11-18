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
│   └── review/                 # exporta/lê a planilha de revisão humana
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

docker compose exec app python -m app.main generate example-client
# -> output/example-client/etl_script.sql + relatorio.md
```

## Backends de LLM

Não existe backend obrigatório. A escolha é só `LLM_BACKEND` no `.env`:

| `LLM_BACKEND` | O que usa | Configuração |
|---|---|---|
| `openai` (padrão) | qualquer endpoint compatível com a API OpenAI — OpenAI, OpenRouter, vLLM, LM Studio, Ollama | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` |
| `ollama` | atalho para o Ollama local (`local` também é aceito) | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |
| `anthropic` | opcional — exige `pip install "migra-agent[anthropic]"` | `ANTHROPIC_API_KEY` |

O backend `openai` com `LLM_BASE_URL` vazio fala com a OpenAI oficial; com
`LLM_BASE_URL` preenchido fala com qualquer servidor compatível.

Todos os backends passam pela mesma camada de robustez (`app/llm/client.py`):
parse tolerante, validação Pydantic, até `LLM_MAX_ATTEMPTS` tentativas com
backoff exponencial, e **o erro de validação volta no prompt** para o modelo
corrigir a própria resposta.

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

## Artefatos gerados em `output/<cliente>/`

| Arquivo | Quando | Conteúdo |
|---|---|---|
| `discovery_report.json` | `discover` | perfil de cada tabela/coluna |
| `mapping.json` | `map` | `MappingDocument` completo |
| `mapeamento_para_revisao.xlsx` | `map` | planilha para revisão humana |
| `etl_script.sql` | `generate` | script de migração |
| `relatorio.md` | `generate` | aprovados, pendentes e rejeitados |
| `run_log.json` | toda fase | tokens, latência e tentativas por chamada de LLM |

`run_log.json` é cumulativo: cada execução de `discover`/`map`/`generate`
acrescenta uma entrada em `runs`, com o modelo usado e o custo de cada
chamada — inclusive as tentativas que falharam.

## Adicionando um novo cliente

1. Criar `config/<nome-cliente>/project.yaml` e `target_schema.yaml`
2. Rodar `discover` → `map` → revisar planilha → `generate`

Nenhum código muda entre clientes — só a pasta `config/<nome>/`.

## Adicionando um novo dialeto de banco legado

1. `app/db/connectors.py` — adicionar driver em `_DRIVER_BY_DIALECT`
2. `app/transforms/registry.py` — adicionar template SQL do novo dialeto
   nas transformações relevantes

## Desenvolvimento

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

O CI roda exatamente esses dois comandos em cada push
(`.github/workflows/ci.yml`).
