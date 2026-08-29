"""The `ac` command line. One entry point for generation, verification and review packets.

Does: expose generate / verify / review as typer commands and print human-readable output.
Does not: contain any pipeline logic or any knowledge of incidents — it orchestrates modules and
formats their results, nothing more.
Exists because: rule 6 says one command has to run the whole thing, and a reviewer should never
have to reconstruct an invocation from a docstring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import config as C
from . import enrich as enrich_mod
from . import generator, llm, report

app = typer.Typer(add_completion=False, help="Attention Cascade — tiered signal detection.")
console = Console()

# The ids this project is configured to use, and what each one is for.
CONFIGURED_MODELS = {
    "tier1 triage": C.TIER1_MODEL,
    "tier2 correlation": C.TIER2_MODEL,
    "naive baseline": C.BASELINE_MODEL,
    "optional 2nd baseline": C.MIDTIER_MODEL,
}


# --------------------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------------------

@app.command()
def generate(
    enrich: bool = typer.Option(False, "--enrich",
                                help="Rewrite every event's text via a blind model pass."),
    seed: int = typer.Option(C.SEED, help="Dataset seed. Same seed produces identical event ids."),
) -> None:
    """Build data/events.db from the planted incident definitions."""
    stats = generator.generate(seed=seed)
    console.print(f"[green]generated[/] {stats['event_count']} events -> {stats['db']}")
    for stream, n in sorted(stats["by_stream"].items()):
        console.print(f"    {stream:<12} {n:>5}")
    # Deliberately not printed here: the count of planted rows. The CLI is a pipeline module and
    # the quarantine test forbids it naming the answer key. `ac stats` reads it via report.py.

    lo, hi = C.TARGET_EVENT_COUNT
    if not lo <= stats["event_count"] <= hi:
        console.print(f"[red]event count {stats['event_count']} outside target band {lo}-{hi}[/]")
        raise typer.Exit(1)

    if enrich:
        console.print("[cyan]enriching[/] every event's text (blind, shuffled, cached)...")
        e = asyncio.run(enrich_mod.enrich(seed=seed))
        console.print(f"    rewritten {e['rewritten']}/{e['events']} across "
                      f"{e['batches']} batches, "
                      f"{e['failed_batches']} failed")
        console.print(f"    spend recorded in {C.DATA_DIR / 'enrich.db'}, "
                      "NOT in any run's llm_calls")
        if e["failed_batches"]:
            console.print("[red]enrichment had failed batches — those events kept "
                          "their canned text[/]")
            raise typer.Exit(1)


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------

def _probe(model: str) -> tuple[bool, str]:
    """Actually call the model with a two-token prompt.

    `models.list()` returns the publisher catalogue, which includes ids this project cannot
    call — it reported four available models here that all returned 404. A real call is the
    only honest availability check, and it is metered like everything else.
    """
    bb_path = C.RUNS_DIR / "verify" / "run.db"
    from .blackboard import Blackboard  # noqa: PLC0415 - only needed on this path

    bb = Blackboard(bb_path, run_id="verify", arm="verify")
    try:
        res = asyncio.run(llm.call(
            model=model, system="Reply with exactly: ok", prompt="ping",
            max_tokens=1024, tier="verify", run_id="verify", bb=bb,
            thinking_budget=0, json_output=False,
        ))
        return True, f"in={res.input_tokens} out={res.output_tokens} think={res.thoughts_tokens}"
    except Exception as exc:  # noqa: BLE001 - the failure text is the point of the command
        return False, str(exc).split("\n")[0][:160]
    finally:
        bb.close()


@app.command()
def verify(
    list_models: bool = typer.Option(False, "--list-models",
                                     help="Probe every configured model id with a real call."),
) -> None:
    """Preflight: credentials, project, dataset, cache, and model availability."""
    ok = True

    console.print("[bold]Environment[/]")
    console.print(f"  GOOGLE_CLOUD_PROJECT  {C.GCP_PROJECT or '[red]NOT SET[/]'}")
    console.print(f"  GOOGLE_CLOUD_LOCATION {C.GCP_LOCATION}")
    console.print(f"  AC_LLM_MODE           {C.LLM_MODE}")
    if not C.GCP_PROJECT:
        console.print("[red]  set GOOGLE_CLOUD_PROJECT in .env, then "
                      "`gcloud auth application-default login`[/]")
        console.print("[yellow]  or run with AC_LLM_MODE=replay to use the committed cache[/]")
        ok = False

    console.print("\n[bold]Dataset[/]")
    if C.EVENTS_DB.exists():
        import sqlite3  # noqa: PLC0415

        n = sqlite3.connect(C.EVENTS_DB).execute("SELECT COUNT(*) FROM events").fetchone()[0]
        console.print(f"  {C.EVENTS_DB.name}: {n} events")
    else:
        console.print("[red]  data/events.db missing — run `ac generate`[/]")
        ok = False

    cached = len(list(C.CACHE_DIR.glob("*.json"))) if C.CACHE_DIR.exists() else 0
    console.print(f"\n[bold]LLM cache[/]\n  {cached} cached responses in runs/cache/")

    if list_models:
        console.print("\n[bold]Model availability — real calls, not the catalogue[/]")
        table = Table("role", "model id", "callable", "detail")
        missing: list[str] = []
        for role, model in CONFIGURED_MODELS.items():
            good, detail = _probe(model)
            table.add_row(role, model, "[green]yes[/]" if good else "[red]NO[/]", detail)
            if not good and role != "optional 2nd baseline":
                missing.append(model)
        console.print(table)

        if missing:
            ok = False
            names = ", ".join(sorted(set(missing)))
            console.print(f"\n[red]MISSING REQUIRED MODEL IDS: {names}[/]")
            console.print("[red]Not substituting a different model. Stopping.[/]")
            console.print("\n[bold]Publisher catalogue visible from this project "
                          f"(location={C.GCP_LOCATION}):[/]")
            try:
                for name in llm.list_available_models():
                    if "gemini" in name:
                        console.print(f"  {name}")
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]catalogue listing failed: {exc}[/]")
            console.print("\n[yellow]Note: the catalogue over-reports. Ids listed above may still "
                          "404. The `callable` column is the authority.[/]")

    console.print(f"\n[bold]{'PASS' if ok else 'FAIL'}[/]")
    raise typer.Exit(0 if ok else 1)


# --------------------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------------------

@app.command()
def review(checkpoint: int = typer.Option(..., "--checkpoint", "-c", min=1, max=5)) -> None:
    """Build review/checkpoint-N/ — the only artefact a reviewer sees."""
    extra: dict[str, str] = {}
    claim = ""
    least: list[str] = []
    questions: list[str] = []

    if checkpoint == 1:
        extra["seed_manifest.json"] = Path(C.SEED_MANIFEST).read_text()
        extra["dataset_stats.txt"] = report.dataset_stats()
        extra["sample_events.txt"] = report.sample_events()
        extra["enrichment_check.txt"] = report.enrichment_check()
        extra["model_availability.txt"] = _model_availability_text()
        claim = (
            "The dataset foundation is built, deterministic, quarantined from the pipeline, and "
            "enriched so planted incidents are not textually distinguishable from noise. One box "
            "is NOT met: the configured Tier 2 / baseline model id does not exist in this "
            "project, and per SPEC I stopped rather than substituting one."
        )
        least = [
            "Whether the planted incidents are genuinely findable by a smart human reading raw "
            "events — this is the one thing I cannot judge for myself.",
            "Whether the near-misses are tempting enough to be a real test of the gate, "
            "particularly NM-4, which is a single CRM event.",
            "Whether enrichment left any structural tell (length, jargon density) that separates "
            "incident text from noise text even though the wording is model-written.",
        ]
        questions = [
            "gemini-3.1-pro is not callable in this project; gemini-3.1-pro-preview is. Approve "
            "the substitution for TIER2_MODEL and BASELINE_MODEL, or point at another project?",
            "Do the INC-1 and NM-1 chains in sample_events.txt read as plausible enterprise data?",
        ]

    dest = report.build_packet(checkpoint, claim, least, questions, extra)
    console.print(f"[green]packet built[/] -> {dest}")
    for p in sorted(dest.iterdir()):
        if p.is_file():
            console.print(f"    {p.name:<28} {p.stat().st_size:>8} bytes")


def _model_availability_text() -> str:
    """Probe every configured id and render the result as plain text for the packet."""
    lines = ["MODEL AVAILABILITY — real two-token calls, not the publisher catalogue",
             "=" * 100,
             f"project  {C.GCP_PROJECT}",
             f"location {C.GCP_LOCATION}",
             ""]
    for role, model in CONFIGURED_MODELS.items():
        good, detail = _probe(model)
        lines.append(f"  {'CALLABLE' if good else 'NOT FOUND':<10} {role:<24} {model:<28} {detail}")
    lines += ["", "Publisher catalogue (over-reports; ids here may still 404):"]
    try:
        lines += [f"  {n}" for n in llm.list_available_models() if "gemini" in n]
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  catalogue listing failed: {exc}")
    return "\n".join(lines)


@app.command()
def stats() -> None:
    """Print the dataset statistics block to the terminal."""
    console.print(report.dataset_stats())


if __name__ == "__main__":
    app()
