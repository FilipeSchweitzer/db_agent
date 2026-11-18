# migra-agent

Pipeline de IA para análise de bancos legados e geração assistida de
scripts de migração para o sistema de gestão municipal.

## Estrutura

```
migra-agent/
├── docker-compose.yml       # app + ollama (opcional, perfil "local")
├── Dockerfile
├── requirements.txt
├── .env.example             # copiar para .env
├── config/
│   └── <cliente>/
│       ├── project.yaml         # conexões, dialeto, escopo — por cliente
│       └── target_schema.yaml   # schema do produto, versionado por release
├── app/
│   ├── main.py               # CLI (discover / map / generate)
│   ├── orchestrator.py        # amarra as 3 fases + checkpoint humano
│   ├── config.py
│   ├── llm/                   # interface trocável: cloud (Anthropic) ou local (Ollama)
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
# preencher ANTHROPIC_API_KEY (ou LLM_BACKEND=local, ver abaixo)
# criar config/<seu-cliente>/project.yaml e target_schema.yaml (ver example-client/)

docker compose up -d --build

docker compose exec app python -m app.main discover example-client
docker compose exec app python -m app.main map example-client
# -> abrir output/example-client/mapeamento_para_revisao.xlsx,
#    revisar a coluna "status" (approved / edited / rejected), salvar

docker compose exec app python -m app.main generate example-client
# -> output/example-client/etl_script.sql + relatorio.md
```

## Rodando com modelo local (Ollama)

```bash
# no .env: LLM_BACKEND=local
docker compose --profile local up -d --build
docker compose exec ollama ollama pull qwen2.5:14b-instruct-q4_K_M
```

## Checkpoint humano

O comando `generate` só processa entradas com `status` em
`approved`/`edited` na planilha. Nada é movido para um script executável
sem essa revisão — ver `app/review/export.py` e `app/agents/agent3_etl.py`.

## Adicionando um novo cliente

1. Criar `config/<nome-cliente>/project.yaml` e `target_schema.yaml`
2. Rodar `discover` → `map` → revisar planilha → `generate`

Nenhum código muda entre clientes — só a pasta `config/<nome>/`.

## Adicionando um novo dialeto de banco legado

1. `app/db/connectors.py` — adicionar driver em `_DRIVER_BY_DIALECT`
2. `app/transforms/registry.py` — adicionar template SQL do novo dialeto
   nas transformações relevantes
