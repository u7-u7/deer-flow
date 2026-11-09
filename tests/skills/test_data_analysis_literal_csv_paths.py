"""CSV inputs are literal paths, not DuckDB glob patterns."""

import hashlib
import json
import os
import runpy
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("openpyxl")
SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills/public/data-analysis/scripts/analyze.py"
)


@pytest.fixture
def analysis():
    return runpy.run_path(str(SCRIPT))


def write_csv(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"value\n{value}\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("with_decoy", [False, True])
@pytest.mark.parametrize(
    "requested, decoy",
    [
        ("report[1].csv", "report1.csv"),
        ("report[!a].csv", "reportb.csv"),
        ("report[[]].csv", "report[].csv"),
        ("uploads[1]/report.csv", "uploads1/report.csv"),
        ("uploads[1]/report[2].csv", "uploads1/report2.csv"),
        ("ordinary.csv", "other.csv"),
        ("owner's uploads/中文.csv", "other.csv"),
        pytest.param(
            "report*.csv",
            "report-other.csv",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX filename"),
        ),
        pytest.param(
            "report?.csv",
            "report1.csv",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX filename"),
        ),
    ],
)
def test_csv_loader_reads_only_the_literal_input(
    analysis, tmp_path, requested, decoy, with_decoy
):
    source = write_csv(tmp_path / requested, 11)
    if with_decoy:
        write_csv(tmp_path / decoy, 99)

    tables = {}
    with duckdb.connect() as con:
        analysis["_load_csv"](con, source, tables)
        assert len(tables) == 1, "an existing literal CSV must load"
        table = analysis["_quote_identifier"](next(iter(tables.values())))
        assert con.execute(f"SELECT value FROM {table}").fetchall() == [(11,)]


def test_legacy_glob_cache_is_rebuilt_then_reused(analysis, tmp_path):
    source = Path(write_csv(tmp_path / "report[1].csv", 11))
    write_csv(tmp_path / "report1.csv", 99)
    scope = analysis["main"].__globals__
    scope["CACHE_DIR"] = str(tmp_path / "cache")

    # Freeze the previous v2 identity for this one real input. Its manifest is
    # structurally valid even when glob expansion loaded another file's rows.
    legacy_hash = hashlib.sha256(b"data-analysis-inputs-v2\0")
    name = os.fsencode(source.name)
    legacy_hash.update(len(name).to_bytes(8, "big"))
    legacy_hash.update(name)
    legacy_hash.update(b"readable\0")
    legacy_hash.update(hashlib.sha256(source.read_bytes()).digest())
    digest = legacy_hash.hexdigest()
    generation = f"{digest}.{'a' * 32}"
    root = Path(scope["CACHE_DIR"]) / "generations-v1"
    directory = root / generation
    directory.mkdir(parents=True)
    database = directory / "data.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute('CREATE TABLE "report_1_" AS SELECT 99 AS value')
    manifest = root / f"{digest}.json"
    manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "generation": generation,
                "table_map": {"report[1]": "report_1_"},
            }
        ),
        encoding="utf-8",
    )

    def load_csv(con, paths):
        # Avoid only optional spatial installation; the CSV reader, cache
        # publication, read-only reopening and SQL engine remain real.
        tables = {}
        for path in paths:
            scope["_load_csv"](con, path, tables)
        return tables

    scope["load_files"] = load_csv
    for _ in range(2):
        with analysis["open_analysis_cache"]([str(source)]) as (con, tables):
            assert tables == {"report[1]": "report_1_"}
            assert con.execute('SELECT value FROM "report_1_"').fetchall() == [(11,)]
        scope["load_files"] = lambda *_: pytest.fail("new generation must be reused")

    assert manifest.exists(), "leave old generations available to active readers"
    with duckdb.connect(str(database), read_only=True) as con:
        assert con.execute('SELECT value FROM "report_1_"').fetchall() == [(99,)]
