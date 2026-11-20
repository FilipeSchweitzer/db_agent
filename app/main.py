"""CLI do pipeline. Uso típico, dentro do container:

    docker compose exec app python -m app.main discover example-client
    docker compose exec app python -m app.main map example-client
    # -> revisar e salvar output/example-client/mapeamento_para_revisao.xlsx
    docker compose exec app python -m app.main generate example-client
    docker compose exec app python -m app.main generate example-client --dry-run
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich import print

from app import orchestrator
from app.config import load_project_config
from app.evaluation import (
    EVAL_ROOT,
    GoldenError,
    evaluate_case,
    load_case,
    resolve_case_ref,
    scaffold_case,
    write_reports,
)
from app.llm import get_llm_client
from app.privacy.policy import PIIPolicyError, enforce_pii_backend_policy
from app.privacy.report import discovery_pii_columns
from app.schemas.mapping import MappingDocument
from app.schemas.profiling import DiscoveryReport

app = typer.Typer(help="Pipeline de migração de dados legados.")


@app.command()
def discover(client: str):
    """Fase 1 — perfila o banco legado e gera discovery_report.json."""
    path = orchestrator.run_discovery(client)
    print(f"[green]Relatório de discovery gerado:[/green] {path}")


@app.command()
def map(client: str):
    """Fase 2 — gera o mapeamento proposto e a planilha de revisão."""
    total_tables = 0

    def progress(position: int, total: int, table: str) -> None:
        nonlocal total_tables
        total_tables = total
        print(f"[dim]({position}/{total})[/dim] mapeando [bold]{table}[/bold]")

    path = orchestrator.run_mapping(client, on_table=progress)
    print(f"[green]Planilha de revisão gerada:[/green] {path}")
    print(f"[dim]{total_tables} tabela(s) de destino mapeada(s).[/dim]")
    print("[yellow]Revise a coluna 'status' antes de rodar 'generate'.[/yellow]")


@app.command()
def generate(
    client: str,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Só valida o mapeamento e mostra o plano; não escreve script executável.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Gera mesmo se o mapping.json não bater com o que foi revisado.",
    ),
    approved_by: str | None = typer.Option(
        None,
        "--approved-by",
        help=(
            "Quem aprovou o mapeamento (vai para auditoria.json). "
            "Padrão: $MIGRA_APPROVER ou o usuário do sistema."
        ),
    ),
):
    """Fase 3 — gera os artefatos de migração a partir do mapeamento APROVADO."""
    path = orchestrator.run_etl_generation(
        client, dry_run=dry_run, force=force, approved_by=approved_by
    )
    if dry_run:
        print(f"[green]Plano validado:[/green] {path}")
        print("[yellow]Nada foi executado nem gerado além do plano.[/yellow]")
        return
    print(f"[green]Artefatos gerados em[/green] {path.parent}")
    print("[dim]Ordem: etl_schema.sql -> etl_script.sql -> etl_constraints.sql "
          "-> etl_validation.sql[/dim]")


@app.command()
def evaluate(
    case: Annotated[
        str,
        typer.Argument(help="Caso em eval/<nome> (ou caminho para a pasta do caso)."),
    ],
    mapping: Annotated[
        Path | None,
        typer.Option(
            "--mapping",
            exists=True,
            dir_okay=False,
            help="Compara um mapping.json já existente (modo offline, sem LLM).",
        ),
    ] = None,
    discovery: Annotated[
        Path | None,
        typer.Option(
            "--discovery",
            exists=True,
            dir_okay=False,
            help="Relatório de discovery a avaliar (padrão: o congelado no caso).",
        ),
    ] = None,
    repeats: Annotated[
        int,
        typer.Option(
            "--repeats",
            help="Quantas execuções do Agente 2 no modo live (mede estabilidade).",
        ),
    ] = 1,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Pasta dos relatórios (padrão: output/eval/<caso>)."),
    ] = None,
):
    """Fase 5 — mede o agente contra um golden set (mapeamento manual)."""
    if repeats < 1:
        raise typer.BadParameter("--repeats precisa ser >= 1")

    try:
        loaded = load_case(resolve_case_ref(case))
    except GoldenError as exc:
        print(f"[red]Caso inválido:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    report_discovery = loaded.discovery
    if discovery is not None:
        report_discovery = DiscoveryReport.model_validate_json(
            discovery.read_text(encoding="utf-8")
        )

    if mapping is not None:
        document = MappingDocument.model_validate_json(mapping.read_text(encoding="utf-8"))
        result = evaluate_case(loaded, mapping=document, discovery=report_discovery)
    else:
        # Mesma regra de LGPD do `map`: PII + backend remoto não faz chamada.
        try:
            enforce_pii_backend_policy(bool(discovery_pii_columns(report_discovery)))
        except PIIPolicyError as exc:
            print(f"[red]Política de PII:[/red] {exc}")
            raise typer.Exit(code=3) from exc
        result = evaluate_case(
            loaded,
            llm=get_llm_client(),
            repeats=repeats,
            discovery=report_discovery,
        )

    destination = output or (orchestrator.OUTPUT_ROOT / "eval" / loaded.name)
    json_path, md_path = write_reports(result, destination)

    summary = result["mapping"]["summary"]
    pii = result["discovery"]["pii"]
    print(f"[green]Caso:[/green] {loaded.name} ({result['mode']})")
    print(
        f"[bold]Mapeamento[/bold] — precisão "
        f"{_percent(summary['precision'])}, recall {_percent(summary['recall'])}, "
        f"F1 {_percent(summary['f1'])}"
    )
    print(
        f"[bold]Discovery[/bold] — tipo {_percent(result['discovery']['type']['accuracy'])}, "
        f"PII recall {_percent(pii['recall'])}"
    )
    if pii["false_negatives"]:
        print(
            f"[red]{len(pii['false_negatives'])} PII não marcada(s)[/red] — "
            f"{', '.join(item['column'] for item in pii['false_negatives'])}"
        )
    print(f"[dim]Relatórios: {json_path} · {md_path}[/dim]")


@app.command("eval-init")
def eval_init(
    case: str = typer.Argument(..., help="Nome do novo caso (pasta em eval/)."),
    project: str = typer.Option(
        ..., "--project", help="Projeto já mapeado em output/<projeto>."
    ),
    force: bool = typer.Option(False, "--force", help="Sobrescreve um caso existente."),
):
    """Cria um rascunho de golden set a partir de um `map` já executado."""
    cfg = load_project_config(project)
    out_dir = orchestrator.OUTPUT_ROOT / cfg.project

    destination = Path(case).expanduser()
    if not destination.is_absolute():
        destination = EVAL_ROOT / destination

    try:
        path = scaffold_case(
            destination,
            schema_path=cfg.target_schema_path,
            discovery_path=out_dir / "discovery_report.json",
            mapping_path=out_dir / "mapping.json",
            project=cfg.project,
            force=force,
        )
    except (GoldenError, FileNotFoundError) as exc:
        print(f"[red]Não foi possível criar o caso:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    print(f"[green]Rascunho criado:[/green] {path}")
    print(
        "[yellow]Revise golden.yaml contra o mapeamento manual: "
        "é o golden que define o que é certo.[/yellow]"
    )


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


if __name__ == "__main__":
    app()
