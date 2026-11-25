#!/usr/bin/env python3
"""Read-only SQL guard for instance adapters that query a database (behavior construction-advisor, PLN0963).

The plugin ships no database driver: the instance's adapter keeps its own (psycopg2, psql -c...) and calls this module
before every statement. Three layers keep a query read-only:
  1. guard(sql): one statement only, starting with SELECT or WITH, with no write, session or admin keyword (INSERT,
     UPDATE, DELETE, INTO, SET, FOR UPDATE...) outside string literals, quoted identifiers and comments, and no call
     to a function with a side effect (set_config, dblink, pg_terminate_backend, pg_read_file, advisory locks...),
     also when its name comes between double quotes ("dblink_exec"(...)). Literals and comments are stripped in a
     single pass, so a quote inside a comment cannot hide a `;` and a comment inside a literal cannot hide a keyword.
     A `--` comment ends at a line feed or a carriage return, as in PostgreSQL. Refused because their quoting rules
     differ and a read does not need them: any `$` outside a literal or quoted identifier that is not a parameter
     ($1; a dollar-quote tag may be any byte above 0x7F, as in $é$, so the guard never tries to read one), escape
     strings (E'...'), Unicode identifiers (U&"...") and a backslash outside a literal (a psql meta-command such as
     \\! would run on the shell when the statement goes to psql through standard input). The function list is a
     best effort: it covers core PostgreSQL and the common extensions (tablefunc, xml2, pg_surgery, dblink,
     postgres_fdw, pg_prewarm, aws_s3...), but any other extension or SECURITY DEFINER function can open a new path.
  2. the session is read-only: libpq_options() gives the PGOPTIONS / psycopg2 `options` with
     default_transaction_read_only=on, standard_conforming_strings=on (a backslash never ends a literal, as guard()
     reads it) and a statement_timeout; run() also opens the transaction READ ONLY, sets standard_conforming_strings
     on with SET LOCAL and refuses a session that does not then read it as on.
  3. run() always rolls the transaction back, also after an error.
guard() and `sql_guard.py check` see only the text they are given. A driver that fills parameters on the client side
(psycopg2: %s, %(name)s) writes the values into the text after that, and a marker inside a literal, a quoted identifier
or a comment ('%s', "%s", -- %s) would turn a value into code. So run() with params guards the final text: it builds
it with cur.mogrify(sql, params) and executes exactly that text, without params. A cursor without mogrify (sqlite3,
psycopg 3 binding on the server) gets the params as they are, and run() refuses a %s or %( marker inside a literal,
identifier or comment. A server-side parameter ($1) is never written into the text, so it does not have the problem.
The real guarantee is a database role that can only read: the session does not stop a function that acts outside the
transaction (dblink opens its own connection and commits, pg_terminate_backend, a session advisory lock, pg_stat_reset,
reading a server file, heap_force_kill editing a page), and a superuser can call all of them. So run() refuses a
superuser session (current or session user) by default; allow_superuser=True lets it through with a RuntimeWarning,
and then guard() is the only barrier against those functions. The check reads the session as it is: an adapter that
logs in as a superuser and runs SET SESSION AUTHORIZATION <role> itself before run() passes as that role (the query
cannot undo it, since SET, RESET and set_config are refused).

  sql_guard.py check "<SQL>" | -          0 and "ok" when the statement passes; 4 and "RECUSADO: <motivo>" when not
  sql_guard.py options [--timeout-ms N]   the libpq options of a read-only session (default timeout 20000 ms)

`check` only holds for a session opened with the `options` PGOPTIONS (psql: `PGOPTIONS="$(sql_guard.py options)" psql
...`); without it, on a role or database with standard_conforming_strings off, `\\'` closes the literal and the rest of
the text runs as code, which `check` cannot see.
"""
import argparse, re, sys, warnings

ALLOWED_START = ('select', 'with')
# Keywords: free inside double quotes, so "set", "into" or "start" work as column names.
KEYWORDS = re.compile(
    r'\b(insert|update|delete|merge|upsert|create|alter|drop|truncate|grant|revoke|comment|vacuum|analyze|cluster|'
    r'reindex|refresh|lock|listen|notify|unlisten|prepare|execute|deallocate|discard|into|set|reset|begin|start|commit|'
    r'rollback|savepoint|release|abort|do|copy|call|import|load|security)\b', re.I)
# Functions with a side effect or that run a query given as text: refused also as a quoted identifier.
FUNCTIONS = re.compile(
    r'\b(set_config|nextval|setval|'
    r'pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile\w*|pg_switch_wal|pg_switch_xlog|'
    r'pg_promote|pg_wal_replay_\w+|pg_backup_\w+|pg_start_backup|pg_stop_backup|pg_create\w*|pg_drop\w*|pg_copy_\w+|'
    r'pg_logical\w*|pg_replication_(?!slots\b)\w+|pg_sync_replication_slots|pg_log_backend_memory_contexts|pg_log_standby_snapshot|'
    r'pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|pg_file_(?!settings\b)\w+|pg_logdir_ls|lo_\w+|loread|lowrite|'
    r'dblink\w*|pg_background\w*|pg_(try_)?advisory\w*|pg_stat_\w*reset\w*|pg_notify|pg_import_system_collations|'
    r'pg_truncate_visibility_map|pg_buffercache_evict\w*|binary_upgrade_\w+|heap_force_\w+|pg_prewarm|autoprewarm_\w+|'
    r'postgres_fdw_\w+|pglogical\w*|aws_(s3|lambda|commons)\w*|xslt_process|'
    r'query_to_xml\w*|cursor_to_xml\w*|table_to_xml\w*|schema_to_xml\w*|database_to_xml\w*|'
    r'crosstab\w*|connectby|xpath_table|ts_stat|ts_rewrite)\b', re.I)
ROW_LOCK = re.compile(r'\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b', re.I)
COMMENT_END = re.compile(r'[\r\n]')
IDENT_CHAR = re.compile(r'[A-Za-z0-9_$]')
DEFAULT_TIMEOUT_MS = 20000
BACKSLASH = 'barra invertida fora de string nao e aceita (no psql seria um meta-comando, como \\!)'
# guard() reads a backslash inside a literal as an ordinary character; with standard_conforming_strings off (role or
# database default, no libpq_options()) PostgreSQL reads \' as an escaped quote and the rest of the text as code.
SCS_SQL = 'SET LOCAL standard_conforming_strings = on'
# One round trip: (superuser?, standard_conforming_strings). Schema-qualified so a search_path with a writable schema
# before pg_catalog cannot shadow the function, view or =.
SUPERUSER_SQL = ("SELECT (pg_catalog.current_setting('is_superuser') OPERATOR(pg_catalog.=) 'on' OR EXISTS "
                 "(SELECT 1 FROM pg_catalog.pg_roles WHERE rolsuper AND (rolname OPERATOR(pg_catalog.=) current_user "
                 "OR rolname OPERATOR(pg_catalog.=) session_user))), "
                 "pg_catalog.current_setting('standard_conforming_strings')")
HIDDEN_MARKER = re.compile(r'%(%|s|\()')


class Refused(ValueError):
    """The statement is not a single read. str() is the reason, in pt-BR, for the person."""


def strip(sql, hidden=None):
    """The statement with string literals as '', quoted identifiers as "" and comments as a space, in one pass.
    Refuses (Refused) what it cannot strip safely: a `$` that is not a parameter ($1), E'' strings, U&"" identifiers,
    an unterminated literal, identifier or comment, a backslash outside a literal (also in a comment) and a quoted
    identifier that names a forbidden function. `hidden`, when a list, gets the raw text of each stripped part."""
    out, i, n = [], 0, len(sql)
    keep = hidden.append if hidden is not None else (lambda part: None)
    while i < n:
        c = sql[i]
        if c == "'":
            if i > 0 and sql[i - 1] in 'eE' and (i < 2 or not IDENT_CHAR.match(sql[i - 2])):
                raise Refused("string com escape (E'...') nao e aceita")
            j = i + 1
            while True:
                if j >= n: raise Refused('string sem fim (aspas simples)')
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'": j += 2; continue
                    break
                j += 1
            keep(sql[i:j + 1]); out.append("''"); i = j + 1
        elif c == '"':
            if i > 1 and sql[i - 1] == '&' and sql[i - 2] in 'uU' and (i < 3 or not IDENT_CHAR.match(sql[i - 3])):
                raise Refused('identificador com escape (U&"...") nao e aceito')
            j = i + 1
            while True:
                if j >= n: raise Refused('identificador sem fim (aspas duplas)')
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"': j += 2; continue
                    break
                j += 1
            m = FUNCTIONS.search(sql[i + 1:j].replace('""', '"'))
            if m: raise Refused(f'so leitura: funcao proibida {m.group(0).upper()} (tambem entre aspas duplas)')
            keep(sql[i:j + 1]); out.append('""'); i = j + 1
        elif sql.startswith('--', i):
            m = COMMENT_END.search(sql, i)             # PostgreSQL ends a -- comment at \n or \r
            j = m.start() if m else n
            if '\\' in sql[i:j]: raise Refused(BACKSLASH)
            keep(sql[i:j]); out.append(' '); i = j
        elif sql.startswith('/*', i):
            depth, j = 1, i + 2
            while depth:
                if j >= n: raise Refused('comentario sem fim (/* sem */)')
                if sql.startswith('/*', j): depth += 1; j += 2
                elif sql.startswith('*/', j): depth -= 1; j += 2
                else: j += 1
            if '\\' in sql[i:j]: raise Refused(BACKSLASH)
            keep(sql[i:j]); out.append(' '); i = j
        elif c == '$':
            # Only a parameter ($1, ASCII digits as PostgreSQL reads them); a digit before the $ does not take it in.
            if not (i + 1 < n and '0' <= sql[i + 1] <= '9'):
                raise Refused('$ fora de string so como parametro ($1): string com $$ ou $tag$ nao e aceita, '
                              'nome com $ vai entre aspas duplas')
            out.append(c); i += 1
        elif c == '\\':
            raise Refused(BACKSLASH)
        else:
            out.append(c); i += 1
    return ''.join(out)


def guard(sql):
    """Returns `sql` unchanged when it is a single read statement; raises Refused with the reason when not."""
    if not isinstance(sql, str) or not sql.strip(): raise Refused('consulta vazia')
    s = strip(sql).strip()
    while s.endswith(';'): s = s[:-1].rstrip()
    if ';' in s: raise Refused('so uma instrucao por vez')
    first = (s.split(None, 1) or [''])[0].lower()
    if first not in ALLOWED_START:
        raise Refused(f'so leitura: comeca com {first.upper() or "(vazio)"}, aceito {"/".join(x.upper() for x in ALLOWED_START)}')
    if ROW_LOCK.search(s): raise Refused('so leitura: SELECT ... FOR UPDATE/SHARE trava linha')
    m = FUNCTIONS.search(s)
    if m: raise Refused(f'so leitura: funcao proibida {m.group(0).upper()}')
    m = KEYWORDS.search(s)
    if m:
        raise Refused(f'so leitura: palavra proibida {m.group(0).upper()} (coluna com esse nome vai entre aspas duplas)')
    return sql


def hidden_marker(sql):
    """True when a client-side parameter marker (%s, %(name)s) sits inside a literal, quoted identifier or comment of
    `sql`, where a driver that fills it on the client would turn the value into code (%% is a literal %)."""
    parts = []
    strip(sql, parts)
    return any(m.group(1) != '%' for part in parts for m in HIDDEN_MARKER.finditer(part))


def _final_text(conn, cur, sql, params):
    """(text to guard, query to execute) for `params`: cur.mogrify's result (bytes from psycopg2, str from psycopg 3's
    ClientCursor), decoded with the connection's encoding only to be guarded; without mogrify, the template."""
    mogrify = getattr(cur, 'mogrify', None)
    if not callable(mogrify):
        if hidden_marker(sql):
            raise Refused('parametro (%s) dentro de string, identificador entre aspas ou comentario: o valor viraria '
                          'parte da consulta; tire as aspas do marcador')
        return None, None
    query = mogrify(sql, params)
    if isinstance(query, str): return query, query
    name = getattr(conn, 'encoding', None) or 'UTF8'
    try:
        from psycopg2.extensions import encodings
        name = encodings.get(name, name)
    except ImportError:
        pass
    try:
        return bytes(query).decode(name), query
    except (LookupError, UnicodeDecodeError, TypeError):
        raise Refused(f'nao deu para ler a consulta com os parametros (codificacao {name}) para conferir')


def libpq_options(timeout_ms=DEFAULT_TIMEOUT_MS):
    """The libpq `options` (PGOPTIONS) of a read-only session with standard strings and a statement timeout."""
    return (f'-c default_transaction_read_only=on -c standard_conforming_strings=on '
            f'-c statement_timeout={int(timeout_ms)}')


def run(conn, sql, params=None, begin='SET TRANSACTION READ ONLY', allow_superuser=False):
    """Guards `sql`, runs it on a DB-API connection and returns (columns, rows); the transaction is always rolled back.
    `begin` opens the transaction read-only (PostgreSQL); None skips it for a driver that is not PostgreSQL, and with
    it the session checks below. A connection in autocommit is refused: there SET TRANSACTION would not cover the
    statement and rollback undoes nothing. Right after `begin`, run() sets standard_conforming_strings on for the
    transaction (SET LOCAL) and refuses unless the session then reads it as on, also with allow_superuser, so a
    backslash in a literal means to PostgreSQL what it means to guard(); params are filled in only after that.
    psycopg2 picks its quoting when it connects: opened with the option off, it writes E'...', which guard() refuses,
    so open the connection with libpq_options().
    A superuser session (current_user or session_user) is refused unless allow_superuser is True, which runs the
    statement and warns (RuntimeWarning): use a role that can only read.
    With params and a cursor that has mogrify (psycopg2), the text with the values filled in is guarded and executed as
    is, without params; without mogrify, a %s marker inside a literal, quoted identifier or comment is refused. The
    superuser check is schema-qualified, but an adapter that ran SET SESSION AUTHORIZATION before passes as that role."""
    guard(sql)
    if getattr(conn, 'autocommit', False) is True:
        raise Refused('conexao em autocommit: abra com autocommit desligado para a transacao ser so leitura')
    try:
        cur = conn.cursor()
        try:
            if begin:
                cur.execute(begin)
                cur.execute(SCS_SQL)                             # before mogrify: the driver quotes by this setting
                cur.execute(SUPERUSER_SQL)
                row = cur.fetchone()
                if row is None or len(row) < 2 or row[1] != 'on':
                    raise Refused('standard_conforming_strings nao ficou on na sessao: o banco leria a barra invertida '
                                  'numa string diferente da guarda; abra a conexao com libpq_options()')
                if row[0] is not False:                          # unknown counts as superuser
                    if not allow_superuser:
                        raise Refused('sessao de superusuario: use um usuario do banco so leitura '
                                      '(GRANT SELECT so nas tabelas necessarias) ou passe allow_superuser=True')
                    warnings.warn('sql_guard: sessao de superusuario liberada por allow_superuser=True; a lista de '
                                  'funcoes da guarda nunca fica completa, use um usuario so leitura',
                                  RuntimeWarning, stacklevel=2)
            final = None
            if params is not None:                               # after the session checks: the text as it will run
                text, final = _final_text(conn, cur, sql, params)
                if text is not None: guard(text)
            if final is not None: cur.execute(final)             # the guarded text, no params: nothing fills it again
            elif params is None: cur.execute(sql)
            else: cur.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
        finally:
            try: cur.close()
            except Exception: pass
        return cols, rows
    finally:
        conn.rollback()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('check', help='confere uma consulta (- le da entrada padrao)')
    c.add_argument('sql')
    o = sub.add_parser('options', help='as opcoes libpq de uma sessao so leitura')
    o.add_argument('--timeout-ms', type=int, default=DEFAULT_TIMEOUT_MS)
    a = ap.parse_args(argv)
    if a.cmd == 'options':
        print(libpq_options(a.timeout_ms)); return 0
    sql = sys.stdin.read() if a.sql == '-' else a.sql
    try:
        guard(sql)
    except Refused as e:
        print(f'RECUSADO: {e}'); return 4
    print('ok'); return 0


if __name__ == '__main__':
    sys.exit(main())
