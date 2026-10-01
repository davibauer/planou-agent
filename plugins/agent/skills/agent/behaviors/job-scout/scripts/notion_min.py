"""Minimal Notion REST API client for notion_jobs.py (optional: without a token, the Notion sync just does not run).
Integration token in NOTION_TOKEN or in ~/.config/job-scout/secrets/notion.env (line NOTION_TOKEN=secret_...); without
one there, the token of watch_core (watch_core.config.NOTION_ENV, the one that writes the daily page; it falls back to the
old ~/.config/vigia/notion.env by itself).
Parent page where the jobs database will be created: NOTION_PARENT_PAGE (page id, shared with the integration)."""
import os, json, urllib.request, urllib.error

API = 'https://api.notion.com/v1'
HEAD = {'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}
import sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import paths
ENV = paths.NOTION_ENV

def _env(path=None):
    path = path or ENV; d = {}
    if os.path.exists(path):
        for l in open(path):
            k, _, v = l.strip().partition('=')
            if k and v: d[k] = v.strip().strip('"')
    return d

def shared_env():
    """notion.env of watch_core (new place, else the legacy ~/.config/vigia/notion.env: watch_core.config.file)."""
    from watch_core import config as wc
    return wc.file(wc.NOTION_ENV)

def token():
    t = os.environ.get('NOTION_TOKEN') or _env().get('NOTION_TOKEN') or _env(shared_env()).get('NOTION_TOKEN')
    if not t: raise RuntimeError(f'sem token do Notion: defina NOTION_TOKEN ou crie {ENV}')
    return t

PARENT = os.environ.get('NOTION_PARENT_PAGE') or _env().get('NOTION_PARENT_PAGE', '')

def _req(method, url, tok, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**HEAD, 'Authorization': f'Bearer {tok}'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Notion {method} {url.split("/v1/")[-1]}: HTTP {e.code} {e.read()[:300].decode(errors="replace")}')
