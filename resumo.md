# resumo.md — migra-agent

> Documento de leitura do estado atual do projeto: o que ele é, como foi
> construído, como funciona por dentro, o que já está resolvido, o que falta e
> por onde continuar.
>
> Base: leitura integral de `README.md`, `app/**` (23 arquivos, ~900 linhas),
> `tests/`, `config/example-client/`, `docker-compose.yml`, `Dockerfile`,
> `requirements.txt` e `.env.example`.

---

## 1. Em uma frase

Um pipeline de IA em 3 fases (`discover` → `map` → `generate`) que lê um banco
legado (Firebird / SQL Server / Postgres), descobre o significado das tabelas e
colunas, propõe um mapeamento para o schema de um sistema de gestão municipal e
gera o script SQL de migração — com um checkpoint humano **obrigatório** entre a
proposta e o script.

O problema que ele ataca é o clássico de modernização de prefeituras: um sistema
legado com 20–30 anos de idade, sem documentação, sem FKs declaradas, com campos
reaproveitados para várias finalidades, e um schema de destino novo que ninguém
sabe como preencher.

---

## 2. O que ele faz

### 2.1 As três fases

| Fase | Comando | Entrada | Saída |
|---|---|---|---|
| 1. Discovery | `python -m app.main discover <cliente>` | banco legado | `output/<projeto>/discovery_report.json` |
| 2. Mapping | `python -m app.main map <cliente>` | discovery + `target_schema.yaml` | `mapping.json` + `mapeamento_para_revisao.xlsx` |
| 3. Generate | `python -m app.main generate <cliente>` | `mapping.json` + planilha revisada | `etl_script.sql` + `relatorio.md` |

Entre 2 e 3 existe o **checkpoint humano**: o analista abre a planilha, edita a
coluna `status` de cada campo (`pending_review` / `approved` / `edited` /
`rejected`), salva, e só então roda `generate`. O Agente 3 nunca decide sozinho o
que entra no script — ele só processa `approved` e `edited` e lista o resto como
pendência no relatório (`app/agents/agent3_etl.py:23-28`).

### 2.2 O que cada fase faz de fato

**Fase 1 — Discovery.** Para cada tabela do escopo: lê o schema via
`inspect()` (colunas, tipos, PK, FKs), e para cada coluna roda
`profile_column()` — contagem de linhas, taxa de nulos, cardinalidade e os 10
valores mais frequentes. Manda esse JSON bruto de **uma tabela por vez** para o
LLM, que devolve um `TableProfile` validado por Pydantic: tipo inferido, problemas
de formato, flag `pii`, notas e uma lista de `risks`. O código então **sobrescreve**
`table` e `row_count` com os valores determinísticos, para o modelo não inventar
números (`app/agents/agent1_discovery.py:65-67`).

**Fase 2 — Mapping.** Recebe o relatório de discovery + o `target_schema.yaml` e
pede ao LLM, **uma tabela de destino por chamada**, as entradas daquela tabela:
origem provável, transformação (escolhida de uma lista fechada), confiança
(`high`/`medium`/`low`), `requires_human_review` e justificativa. Regra dura no
prompt: campo `pii=true` no destino **sempre** exige revisão humana, mesmo com
confiança alta. Se não houver correspondência razoável, o campo vai para
`unmapped_target_fields` em vez de o modelo forçar um palpite. O código então
normaliza a resposta: descarta campo inventado, remove entrada duplicada, força
`target_table` a vir do schema e registra em `warnings` tudo o que não veio do
modelo (`app/agents/agent2_mapping.py`).

**Fase 3 — Generate.** Relê a planilha revisada, reagrupa as entradas aprovadas
por tabela de destino e renderiza `INSERT INTO ... SELECT ... FROM`, aplicando as
transformações via `render_sql()`. Se uma tabela de destino depende de mais de
uma tabela de origem, ele **não tenta adivinhar o JOIN**: emite um comentário de
atenção e pula (`app/agents/agent3_etl.py:56-61`).

### 2.3 O comando de migração é só a metade visível

O produto real é o **contrato**: `MappingDocument` é o formato estável entre as
fases (`app/schemas/mapping.py`). É ele que alimenta a planilha, é ele que volta
revisado, é ele que o Agente 3 consome. A planilha é deliberadamente um `.xlsx` e
não uma tela — decisão registrada em `app/review/export.py:1-7`: reduz atrito na
entrega inicial e pode virar UI web depois sem mudar o formato.

---

## 3. Como foi construído

### 3.1 Arquitetura em camadas

```
CLI (typer)                    app/main.py
  └── Orquestração             app/orchestrator.py        ← único lugar que amarra as fases
        ├── Config             app/config.py              ← project.yaml + env
        ├── Banco              app/db/connectors.py       ← engine por dialeto
        │                      app/db/introspection.py    ← "tools" de leitura
        ├── LLM                app/llm/client.py          ← ABC + retry/parse/telemetria
        │                      app/llm/backend_openai.py  ← OpenAI / Ollama / qualquer compatível
        │                      app/llm/backend_anthropic.py ← opcional (extra)
        ├── Agentes            app/agents/agent1..3.py
        ├── Contratos          app/schemas/*.py           ← Pydantic, fronteira de validação
        ├── Transformações     app/transforms/registry.py ← lista fechada
        └── Revisão            app/review/export.py       ← xlsx ida e volta
```

### 3.2 Cinco decisões de design que sustentam o projeto

1. **Tudo que varia por cliente é dado, não código.** Conexões, dialeto, escopo e
   schema de destino vivem em `config/<cliente>/`; o container monta essa pasta
   como volume read-only. Nenhum `if cliente == ...` no código. Onboarding de
   cliente novo = criar uma pasta.
2. **Lista fechada de transformações.** O Agente 2 não escreve lógica livre — ele
   escolhe um nome de `TRANSFORM_REGISTRY` (`regex_extract_digits`,
   `parse_date_br`, `map_enum`, `split_nome_completo`). Se nenhuma serve, o campo
   é marcado para revisão em vez de o modelo inventar algo ad-hoc. Isso troca
   cobertura por previsibilidade — uma troca correta para dados de cidadão.
3. **Interface de LLM trocável.** Os agentes só conhecem `LLMClient.complete_structured()`
   (`app/llm/client.py`). Todo backend que fala a API OpenAI (OpenAI, Ollama, vLLM,
   OpenRouter) usa `backend_openai.py`; o Anthropic é um extra opcional. Nenhum
   provedor é privilegiado, e não há `if backend == ...` dentro dos agentes.
4. **Toda saída do modelo é validada contra um schema Pydantic.** Nunca texto
   livre. Se o modelo devolver algo fora do contrato, falha alto na borda em vez
   de contaminar a planilha.
5. **O código determinístico tem precedência sobre o modelo.** Onde dá para
   calcular (contagem de linhas, lista de tabelas, renderização de SQL), é código.
   O LLM entra só onde não há regra: interpretar significado, propor
   correspondência, escrever o risco em linguagem legível.

### 3.3 Stack

Python 3.11 · Typer + Rich (CLI) · Pydantic 2 (contratos) · SQLAlchemy 2 +
`fdb`/`pyodbc`/`psycopg2` (bancos) · OpenAI SDK — compatível com OpenAI, Ollama,
vLLM, OpenRouter; Anthropic SDK só no extra opcional (LLMs) ·
pandas + openpyxl (planilha de revisão) · Docker Compose com perfil `local` para
subir o Ollama junto.

---

## 4. O que já está bem resolvido

Isto não é um esqueleto — as partes difíceis do **desenho** estão tomadas:

- **O checkpoint humano é estrutural, não opcional.** `ReviewStatus` está no
  contrato, e `ETLAgent` filtra por ele. Não existe caminho de código que gere
  script a partir de entrada não aprovada.
- **PII tem regra explícita.** `pii: true` no schema de destino força
  `requires_human_review`. Há também heurística por nome de coluna na descoberta
  (`_PII_NAME_HINTS`) com a ressalva correta de que nome não basta.
- **Segredos fora do YAML.** Senhas só entram por `SOURCE_DB_PASSWORD` /
  `TARGET_DB_PASSWORD` (`app/config.py:52-54`).
- **Falha segura em ambiguidade.** Multi-origem → JOIN manual, sem chute.
  Sem correspondência → `unmapped`, sem chute.
- **Separação de responsabilidades limpa.** Cada arquivo tem um trabalho, e os
  docstrings explicam o *porquê* das decisões, não só o *o quê*.
- **Extensibilidade prevista.** Novo dialeto = 1 dicionário + N templates. Novo
  cliente = 1 pasta. Novo backend de LLM = 1 classe.

---

## 5. O que parece estar faltando

Ordenado por impacto real na migração. Severidade = "quão cedo isso te machuca
em um cliente de verdade".

### 5.1 Crítico

| # | Lacuna | Onde | Por que importa |
|---|---|---|---|
| C1 | **Chave substituta (surrogate key) não é tratada.** `references: bairro.id` é declarado no target schema mas **nunca lido por nenhum código**. | `app/schemas/target_schema.py:17` | É *o* problema de migração real: `pessoa.cod_bairro` no legado referencia o código antigo, mas `bairro.id` no destino é `SERIAL`. Sem uma tabela de-para de chaves, todo INSERT com FK quebra ou aponta para o registro errado. |
| C2 | ~~**`max_tokens=4096` fixo + schema inteiro em um único prompt.**~~ **Resolvido na Fase 1:** o Agente 2 agora mapeia **uma tabela de destino por chamada** (`MappingAgent.run`), e o `max_tokens` vem de `LLM_MAX_TOKENS`. | `app/agents/agent2_mapping.py` | Histórico: com 20 tabelas o JSON estourava 4096 tokens, truncava e `json.loads` falhava. Hoje o tamanho da resposta é proporcional a uma tabela, não ao schema inteiro. |
| C3 | **ETL sem DDL, sem lote, sem transação, sem idempotência.** Gera só `INSERT INTO ... SELECT`; `batch_size` e `etl.engine` são config morta. | `app/agents/agent3_etl.py:41-72` | Rodar o script duas vezes duplica os dados. Não cria tabelas/sequences/FKs no destino. Não ordena por dependência de FK. Não tem `BEGIN`/`COMMIT`/rollback. |
| C4 | **PII vai para a nuvem.** `top_values` reais (CPF, nome, endereço) entram no prompt enviado ao Anthropic. | `app/db/introspection.py:61-76` → `agent1_discovery.py:56-60` | Exposição de dado pessoal a terceiro sem base legal explícita — problema de LGPD, não de engenharia. |
| C5 | **Suspeita de dependência faltando para Firebird.** `requirements.txt` pina `sqlalchemy==2.0.35` e inclui `fdb`, mas não `sqlalchemy-firebird`. | `requirements.txt:10-11` | O dialeto Firebird saiu do core do SQLAlchemy 2.0 para o pacote `sqlalchemy-firebird`. Se confirmado, `create_engine("firebird+fdb://...")` levanta `NoSuchModuleError` — ou seja, o dialeto legado principal não conecta. **Verificar rodando `pip install -r requirements.txt` e um `build_url` + `create_engine` de teste.** |

### 5.2 Alto

| # | Lacuna | Onde |
|---|---|---|
| A1 | **Duas das quatro transformações não funcionam na prática.** `split_nome_completo` tem `sql_templates={}` → `render_sql` levanta `ValueError`. `map_enum` exige `when_clauses`, que ninguém constrói a partir de `params["mapping"]` → `KeyError`. | `app/transforms/registry.py:52-65,73-80` |
| A2 | **`engine: python` nunca é implementado.** `python_fn` existe em todas as specs mas não há um único chamador. O Agente 3 sempre renderiza SQL. | `registry.py:19`, `agent3_etl.py:44-53` |
| A3 | **`FETCH FIRST n ROWS ONLY` não é T-SQL.** Quebra a introspecção quando a origem é SQL Server (e o README promete SQL Server). | `app/db/introspection.py:48,65` |
| A4 | **Validação de somente-leitura é regex frágil.** Não bloqueia `merge`, `grant`, `create`, `call`, `execute`, `copy`, nem múltiplos statements. | `app/db/introspection.py:85-87` |
| A5 | **Não há prova do que foi aprovado.** O `generate` relê `mapping.json` (não a planilha) e aplica os status por cima. Se `mapping.json` for regerado entre a revisão e o `generate`, o script sai de um mapeamento diferente do revisado — sem aviso. Falta hash/assinatura do par (mapping + planilha). | `app/orchestrator.py:63-67` |
| A6 | ~~**`OUTPUT_ROOT = /app/output` hardcoded.**~~ **Resolvido na Fase 1:** `OUTPUT_ROOT` lê `OUTPUT_ROOT` do ambiente (padrão `/app/output`), o que permite testar e rodar fora do container. | `app/orchestrator.py:17` |
| A7 | ~~**Sem retry/backoff em chamada de LLM** e sem contabilização de tokens/custo por fase.~~ **Resolvido na Fase 1:** `LLMClient.complete_structured` faz até `LLM_MAX_ATTEMPTS` tentativas com backoff exponencial + jitter, só repete erro transitório, realimenta o erro de validação no prompt e registra tokens/latência por tentativa em `run_log.json`. | `app/llm/client.py`, `app/orchestrator.py:32` |
| A8 | **`load_reviewed_xlsx` descarta linhas desconhecidas em silêncio** e explode com `ValueError` cru se o analista digitar um `status` inválido na planilha. | `app/review/export.py:44-48,67` |

### 5.3 Médio

- **Documentação divergente do código:** o docstring de `introspection.py:3-6`
  afirma que "o LLM decide quando chamar cada tool" (function-calling). Não existe
  tool-use em lugar nenhum — o fluxo é determinístico: código enumera e perfila,
  LLM interpreta o resultado. Ou implementa-se o loop de tool-use, ou corrige-se o
  docstring.
- **Escala:** 1 chamada de LLM por tabela, sequencial, sem cache, sem paralelismo,
  sem orçamento de tokens (`agent1_discovery.py:46-68`).
- **Custo/observabilidade:** sem logging estruturado, sem métricas por fase, sem
  `run_id`, sem artefato de auditoria da execução.
- **Descoberta rasa em relacionamento:** não infere FK implícita ("cod_bairro"
  apontando para "bairro"), não detecta órfãos, não mede min/max de datas nem
  distribuição além do top-10.
- **Config morta / inconsistente:** `unmapped_source_fields` nunca é preenchido;
  `validator` do target schema nunca é usado; `config.py:41` exige que o nome da
  pasta seja idêntico ao campo `project`. (`CLIENT_PROJECT` saiu do `.env.example`
  na Fase 1 — não era lido por ninguém.)
- **Identificadores SQL interpolados sem quoting** em `sample_rows`,
  `profile_column` (`introspection.py:44-67`). Hoje os nomes vêm do `inspect()`,
  não do LLM, então o risco é baixo — mas não há defesa em profundidade.
- **Diretório lixo `{config/`** na raiz (3 níveis de pastas com nomes tipo
  `example-client,app/llm,app`), resquício de uma expansão de shell quebrada.

### 5.4 Baixo (mas cobra juros)

- **Cobertura de teste ainda curta.** A Fase 1 levou a suíte de 3 para 36 testes
  (cliente de LLM, retry, seleção de backend, schema estrito, chunking do Agente 2,
  `run_log`), mas continuam sem teste: `render_sql`, round-trip Excel
  (`export_to_xlsx` → `load_reviewed_xlsx`), geração do `etl_script.sql` e
  `detect_pii_by_name`. Para um produto onde o erro custa dado de cidadão, essa é
  a lacuna silenciosa mais cara que sobrou.
- ~~**Sem `pyproject.toml`**, sem ruff, sem CI.~~ **Resolvido na Fase 0:**
  `pyproject.toml` (projeto instalável, ruff + pytest) e `.github/workflows/ci.yml`
  rodando `ruff check` + `pytest` em push/PR. Ainda falta mypy/pyright e pre-commit.
- ~~**Sem repositório git inicializado.**~~ **Resolvido na Fase 0:** `main`
  versionada e sincronizada com `origin`.
- ~~**Diretório lixo `{config/`**~~ **Resolvido na Fase 0:** removido (continha só
  diretórios vazios com nomes inválidos).
- `from rich import print` sombreia o builtin (`app/main.py:11`).
- Docstrings em português, código em inglês, prompts em português — consistente,
  mas vale registrar como convenção.

---

## 6. Próximos passos sugeridos

Em fases, cada uma com entregável concreto e critério de pronto. A ordem não é
por "o que é mais fácil", é por **o que destrava um piloto real**.

### Fase 0 — Higiene (½ dia) — ✅ concluída

- `git init` + primeiro commit (sem `.env`, sem `output/*`).
- Apagar `{config/`.
- Criar `pyproject.toml` (ruff + pytest config + projeto instalável).
- CI mínimo: `ruff check` + `pytest` em push.
- **Pronto quando:** `git log` existe, CI verde, `{config/` sumiu.

### Fase 1 — Robustez do LLM (2–3 dias) — ✅ concluída — destrava C2, A7

Entregue em `app/llm/`:

- **Nenhum provedor obrigatório.** `LLM_BACKEND` escolhe entre `openai`
  (qualquer endpoint compatível: OpenAI, Ollama, vLLM, LM Studio, OpenRouter),
  `ollama` (atalho) e `anthropic` (opcional, extra `migra-agent[anthropic]`;
  o SDK nem é dependência do projeto). O caminho padrão não depende da Anthropic.
- **Saída estruturada nativa** em vez de "responda JSON no prompt": tool-use
  forçado no Anthropic, `response_format` com JSON Schema no OpenAI/Ollama, com
  downgrade automático e memorizado para `json_object` quando o servidor recusa
  o modo estrito.
- **Retry com backoff** em erro de parse/validação **e** em erro transitório de
  transporte (429/5xx), com o erro realimentado no prompt para o modelo corrigir
  a própria resposta. Erro 4xx "de uso" não é repetido.
- **Chunking do Agente 2:** mapeamento **por tabela de destino** (1 chamada/tabela),
  com índice compacto da origem no prompt em vez do `discovery_report` inteiro.
  Elimina o teto de 4096 tokens por construção.
- **Garantias determinísticas por cima do modelo:** tabela de destino vem do
  schema (não do modelo), entrada inventada é descartada com aviso, campo sem
  resposta vai para `unmapped_target_fields` com warning.
- **Telemetria:** tokens, latência, tentativa e erro por chamada, gravados em
  `output/<projeto>/run_log.json` (cumulativo entre fases).
- **Pronto quando:** mapear um `target_schema` de 10 tabelas completa sem
  truncamento, e uma falha de parse se recupera sozinha. — coberto pelos 36
  testes (`tests/test_llm_client.py`, `test_backend_openai.py`,
  `test_llm_factory.py`, `test_mapping_agent.py`, `test_orchestrator.py`).

### Fase 2 — Corretude do ETL (1–2 semanas) — destrava C1, C3, A1, A2, A5

Esta é a fase que transforma o projeto de "demo convincente" em "ferramenta
usável".

1. **Tabela de-para de chaves (C1).** Para cada relação `references: tabela.col`,
   gerar um passo de staging que cria `_map_<tabela>(old_id, new_id)` e um
   `UPDATE`/`JOIN` que traduz a FK antes do INSERT. Sem isso, migração com
   relacionamento simplesmente não fecha.
2. **DDL + ordem de dependência (C3).** Gerar `CREATE TABLE`/sequences a partir do
   `target_schema.yaml` ou, melhor, validar contra o schema já existente; ordenar
   os INSERTs por grafo de FK (topological sort).
3. **Transação + lote.** `BEGIN`/`COMMIT` por tabela, `batch_size` respeitado,
   idempotência (`ON CONFLICT DO NOTHING` no Postgres / `MERGE` no SQL Server /
   `UPDATE OR INSERT` no Firebird) — escolhido por dialeto.
4. **Dry-run + validação pós-carga.** `--dry-run` que só conta linhas por tabela;
   depois da carga, comparar contagem origem vs. destino e emitir divergências em
   `validacao.md`.
5. **Consertar as transformações (A1/A2):** implementar `map_enum` montando
   `when_clauses` a partir de `params["mapping"]`; dar template SQL para
   `split_nome_completo` (ou removê-la do registry e documentar que só existe em
   Python); implementar o renderizador Python de fato, ou **remover** `engine:
   python` e `python_fn` do código e do YAML até existir.
6. **Hash de aprovação (A5):** gravar SHA-256 de `mapping.json` + planilha no
   momento da revisão; `generate` recusa rodar se não bater.

- **Pronto quando:** um cliente com 3 tabelas relacionadas migra, roda duas vezes
  seguidas sem duplicar, e a contagem bate.

### Fase 3 — Multi-dialeto de verdade (3–5 dias) — destrava A3, A4

- Resolver C5 (dependência Firebird) e validar conexão real nos 3 dialetos.
- Trocar SQL escrito à mão por **`sqlglot`**: gerar em um dialeto canônico
  (Postgres) e transpilar para Firebird/SQL Server. Substitui o
  `sql_templates` por dialeto, que é onde os bugs de dialeto nascem.
- Corrigir a introspecção: paginação por dialeto (`TOP` no T-SQL,
  `LIMIT`/`FETCH` no resto); quoting de identificadores via
  `engine.dialect.identifier_preparer`.
- Endurecer a tool de leitura: allowlist de statement (`SELECT`/`WITH` apenas,
  parseado — não regex), conexão com usuário `GRANT SELECT` (o TODO em
  `connectors.py:39-40` já aponta o caminho certo).

- **Pronto quando:** a mesma migração roda em origem Firebird e origem SQL Server
  sem tocar em código.

### Fase 4 — Governança e LGPD (3–4 dias) — destrava C4

- **Mascarar antes de enviar:** `top_values` de colunas `pii=true` viram
  tipo+formato (`"###.###.###-##"`, `"<nome>"`, `"DD/MM/AAAA"`) em vez do valor
  real. O LLM não precisa do CPF, precisa do **padrão**.
- Modo `LLM_BACKEND=local` obrigatório quando PII é detectada (regra de negócio,
  não sugestão).
- Trilha de auditoria: quem aprovou, quando, qual versão do mapping, qual versão do
  `target_schema` (`version: "2026.1"` já existe — só não é usada para checar
  compatibilidade).
- **Pronto quando:** nenhum valor de PII sai da rede, e existe um
  `auditoria.json` por execução.

### Fase 5 — Avaliação e produto (contínuo)

- **Golden set (o mais importante a longo prazo).** Pegar 2–3 migrações já feitas
  à mão, com o mapeamento correto conhecido, e medir precisão/recall do Agente 2
  campo a campo. Sem isso, toda mudança de prompt é fé, não engenharia.
- Avaliação de discovery: o tipo inferido bate com o tipo real? A flag PII acerta?
- UI de revisão (substituir/complementar o Excel) — o formato `MappingDocument` já
  permite, como o próprio código antecipa.
- Execução incremental: só re-perfilar tabelas que mudaram; cache de discovery.

---

## 7. Ordem recomendada, em uma tabela

| Fase | Esforço | Destrava | Se pular |
|---|---|---|---|
| 0. Higiene ✅ | ½ dia | versionamento, CI | dívida cresce sem histórico |
| 1. LLM robusto ✅ | 2–3 dias | C2, A7 | fase 2 não completa em cliente real |
| 2. ETL correto | 1–2 semanas | C1, C3, A1, A2, A5 | script parece pronto e corrompe/produz FK errada |
| 3. Multi-dialeto | 3–5 dias | C5, A3, A4 | só funciona em Firebird, e talvez nem nele |
| 4. Governança | 3–4 dias | C4 | risco LGPD com dado de cidadão |
| 5. Avaliação | contínuo | qualidade medida | cada ajuste de prompt é chute |

Se só houver tempo para **uma** coisa: **Fase 2, item 1 (de-para de chaves)**. É a
diferença entre um gerador de SQL plausível e um migrador que funciona.

---

## 8. Nota sobre o estado do repositório

> Atualizado depois das Fases 0 e 1. O restante do documento é a análise
> original e continua válido para as lacunas ainda abertas.

- Repositório git em `main`, sincronizado com `origin`; CI verde
  (`ruff check` + `pytest`) em cada push.
- `graphify-out/` está no `.gitignore` — é artefato de análise, não faz parte do
  produto.
- `{config/` foi removido.
- Suíte de testes: 36 testes, todos offline (nenhum fala com LLM ou banco).
- `output/` só tem `.gitkeep`: **o pipeline nunca rodou de ponta a ponta contra um
  banco de verdade**. Essa continua sendo a lacuna mais honesta deste documento —
  tudo acima é análise estática. Fases 2 e 3 são o que destrava o primeiro
  piloto real.
