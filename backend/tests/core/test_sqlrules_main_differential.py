"""#398: the read checks compared with their outcomes before it, in both dialects. No database.

`statements()` builds seeded statements of the parts that bypass a SQL check.
`sqlrules_main_differential.json` next to this file holds what the source at `MAIN` answers for
exactly these statements: for Snowflake whether the run-time check refuses (with `PARAMS` bound) and
the issue codes of `statement_rules`, `raw_word_rules` and `table_rules`; for Databricks one digest
of every message and code. How Snowflake reads these texts is unconfirmed: the test only shows that
nothing refused before is accepted now.

It was recorded with `MAIN`'s source, never with the branch's: `git archive 7cd5284 backend/src`
extracted into a directory `D`, then, in `backend/` with `PYTHONPATH=D/backend/src`,
`python -m tests.core.test_sqlrules_main_differential D`, which refuses to write unless `shifde` was
imported from `D`. The data file is not rewritten when a rule changes later: a changed generator
fails on `statements_digest`.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any  # Any: the decoded data file

from shifde.adapters.base import AdapterFailure, ErrorClass
from shifde.adapters.dataplatforms import sqlapi
from shifde.adapters.dataplatforms.sqlapi import check_read_only
from shifde.core import sqlrules
from shifde.core.sqlrules import raw_word_rules, statement_rules, table_rules

MAIN = "7cd5284"
DATA = Path(__file__).with_name("sqlrules_main_differential.json")
SEED = 398
COUNT = 6000
PARAMS = ["DB.SC.T", "Last_Query_Id"]
TABLES = ["DB.SC.T"]
STARTS = ["select ", "select $1 from ", "with a as (select 1) select ", "select a from DB.SC.T "]
PARTS = [":", "::", "//", "snow://a/b", "a:", "@", "@s", "@s/a", "@s/a\\ ", "@s/a\\\t"]
PARTS += ["\\", "\\ ", "\\\t", "\\\r", "\\\n", " ", " ", " ", "\t", "\r", "\n", "--", "/*", "*/"]
PARTS += ["'", '"', "$$", "'a'", '"A"', "$$b$$", "/"]
PARTS += ["->>", "->>", "remove", "Get_Ddl", "GET_PRESIGNED_URL", "nextVal", "GetNextVal"]
PARTS += ["Result_Scan", "result_scan", "LAST_QUERY_ID", '"last_query_id"', "'RESULT_SCAN'"]
PARTS += ["identifier(", "identifier(?)", "identifier('", "table(", "?", ":n", "%(a)s", "system$x"]
PARTS += [":call", "call", ":1", "\u3164", "\u3164", "\u200b", "\u00a0", "\u00e9", ";", "(", ")"]
PARTS += ["x", "x", "y", ",", ".", "from", " from DB.SC.T", "DB.OTHER.T", "1", "r", "`"]


def statements(seed: int = SEED, count: int = COUNT) -> list[str]:
    rng = random.Random(seed)
    return [
        rng.choice(STARTS) + "".join(rng.choice(PARTS) for _ in range(rng.randint(1, 8)))
        for _ in range(count)
    ]


def statements_digest(texts: list[str]) -> str:
    return hashlib.sha256(json.dumps(texts).encode()).hexdigest()


def snowflake_refusal(sql: str) -> str | None:
    """The message of the run-time refusal, None when the statement is accepted."""
    try:
        check_read_only(sql, dialect="snowflake", params=PARAMS)
    except AdapterFailure as exc:
        assert exc.error_class is ErrorClass.CAPABILITY
        return exc.message
    return None


def snowflake_codes(sql: str) -> list[list[str]]:
    """The distinct issue codes of `statement_rules`, `raw_word_rules` and `table_rules`."""
    found = (
        statement_rules(sql, dialect="snowflake"),
        raw_word_rules(sql),
        table_rules(sql, TABLES, dialect="snowflake"),
    )
    return [sorted({issue.code for issue in issues}) for issues in found]


def databricks_outcome(sql: str) -> list[object]:
    try:
        check_read_only(sql, params=PARAMS)
        message = None
    except AdapterFailure as exc:
        message = exc.message
    issues = [statement_rules(sql), table_rules(sql, TABLES)]
    return [message, [[(i.code, i.message) for i in found] for found in issues]]


def record() -> dict[str, Any]:
    texts = statements()
    found = [snowflake_codes(s) for s in texts]
    names = sorted({code for rules in found for codes in rules for code in codes})
    outcomes = [
        json.dumps(
            [
                int(snowflake_refusal(s) is not None),
                *([names.index(code) for code in codes] for codes in rules),
            ]
        )
        for s, rules in zip(texts, found, strict=True)
    ]
    distinct = sorted(set(outcomes))
    databricks = [databricks_outcome(s) for s in texts]
    return {
        "commit": MAIN,
        "statements_digest": statements_digest(texts),
        "codes": names,
        # [refused, codes of statement_rules, of raw_word_rules, of table_rules]
        "outcomes": [json.loads(outcome) for outcome in distinct],
        "snowflake": [distinct.index(outcome) for outcome in outcomes],  # one per statement
        "databricks_digest": hashlib.sha256(json.dumps(databricks).encode()).hexdigest(),
    }


def _main() -> tuple[dict[str, Any], list[tuple[str, bool, list[list[str]]]]]:
    """The data file, and each statement with main's refusal and issue codes."""
    data = json.loads(DATA.read_text(encoding="utf-8"))
    texts = statements()
    assert data["statements_digest"] == statements_digest(texts)
    cases = []
    for sql, at in zip(texts, data["snowflake"], strict=True):
        refused, *rules = data["outcomes"][at]
        cases.append((sql, bool(refused), [[data["codes"][c] for c in codes] for codes in rules]))
    return data, cases


def test_the_data_is_mains_outcome_of_at_least_5000_statements() -> None:
    data, cases = _main()
    assert data["commit"] == MAIN
    assert len(cases) == COUNT >= 5000


def test_nothing_main_refuses_at_run_time_is_accepted() -> None:
    _, cases = _main()
    assert [s for s, refused, _ in cases if refused and snowflake_refusal(s) is None] == []


def test_no_issue_code_of_main_is_missing() -> None:
    _, cases = _main()
    missing = [
        (s, code)
        for s, _, codes in cases
        for main, now in zip(codes, snowflake_codes(s), strict=True)
        for code in main
        if code not in now
    ]
    assert missing == []


def test_databricks_is_identical_to_main() -> None:
    data, cases = _main()
    outcomes = [databricks_outcome(s) for s, _, _ in cases]
    assert hashlib.sha256(json.dumps(outcomes).encode()).hexdigest() == data["databricks_digest"]
    assert {outcome[0] is None for outcome in outcomes} == {True, False}


def test_the_comparison_is_not_vacuous() -> None:
    """Each of the four items refuses statements main accepts, and both accept others."""
    _, cases = _main()
    newly = [snowflake_refusal(s) for s, refused, _ in cases if not refused]
    assert set(newly) == {
        None,
        sqlapi._COLON_COMMENT,
        sqlrules.STAGE_PATH_MESSAGE,
        sqlapi._NOT_READ,  # only the three added functions give it to a text main accepts
        sqlapi._REFUSED_CHARACTER,
    }
    assert any(refused for _, refused, _ in cases)
    given = {code for _, _, codes in cases for found in codes for code in found}
    assert given >= {
        "sql_comment",
        "sql_flow_operator",
        "sql_refused_function",
        "sql_stage_path",
        "sql_system_function",
        "sql_dynamic_name",
        "sql_write_statement",
        "sql_syntax",
        "sql_table_not_allowed",
    }


if __name__ == "__main__":
    source = Path(sys.argv[1]).resolve()
    for module in (sqlrules, sqlapi):
        if not Path(str(module.__file__)).resolve().is_relative_to(source):
            sys.exit(f"shifde was not imported from {source}")
    lines = [
        f'"{key}": {json.dumps(value, separators=(",", ":"))}' for key, value in record().items()
    ]
    DATA.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8", newline="\n")
