#!/usr/bin/env python3
"""Hook tag_pull: a new release tag in a repository brings the copy in use up to date (PLN0259).

The CI publishes the plugins on its own (merge, version, tag <plugin>--vX.Y.Z), so nobody is left to update the copy
the agents run from. On every heavy tick this hook fetches the repository's tags; on a tag it has not seen it does
`git merge --ff-only origin/<branch>` in that copy (a `git pull --ff-only` after the fetch), writes the version in the
versions file (`versoes[<plugin>]`, the file publish-plugin keeps) and prints `== TAG NOVA (n)`, which wakes the session
to tell the person which runners need a relaunch (the hook never relaunches anything).

Config: {"type": "tag_pull", "path": "<copy in use>", "branch": "main", "pattern": "*--v*",
         "versions_file": "~/.config/publish-plugin.json", "remote": "origin"}
path is required (~ expanded). The first read only records the tags that exist (nothing old is announced). The copy is
never forced: on another branch, with tracked changes or without a fast-forward, nothing changes, the tick prints
`== TAG NOVA: copia em uso NAO atualizada` once per reason and tries again on the next tick. A tag whose commit is not
in the branch after the pull waits the same way. Dry ticks do nothing (fetch and pull leave the instance).
"""
import json, os, re, subprocess

import paths
from adapters import Gancho as Base
from watch_core import fileio

TAG_RE = re.compile(r'^(?P<plugin>.+)--v(?P<version>\d+\.\d+\.\d+)$')


def _ver(v):
    try: return tuple(int(x) for x in v.split('.'))
    except (AttributeError, ValueError): return None


def update_versions(path, tags):
    """versoes[plugin] = version for each release tag, never going back; other keys stay. Returns what changed."""
    try:
        with open(path, encoding='utf-8') as f: data = json.load(f)
        if not isinstance(data, dict): data = {}
    except (OSError, ValueError):
        data = {}
    vers = data.get('versoes') if isinstance(data.get('versoes'), dict) else {}
    changed = {}
    for t in tags:
        m = TAG_RE.match(t)
        if not m: continue
        p, v = m.group('plugin'), m.group('version')
        if _ver(vers.get(p)) is None or _ver(v) > _ver(vers.get(p)):
            vers[p] = changed[p] = v
    if not changed: return {}
    data['versoes'] = vers
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fileio.write_json(path, data, indent=1, ensure_ascii=False)
    return changed


class Gancho(Base):
    tipo = 'tag_pull'
    json_key = 'tag_pull'

    def configura(self):
        p = self.cfg.get('path')
        self.repo = paths.expand(p) if isinstance(p, str) and p.strip() else None
        self.branch = self.cfg.get('branch') or 'main'
        self.pattern = self.cfg.get('pattern') or '*--v*'
        self.remote = self.cfg.get('remote') or 'origin'
        vf = self.cfg.get('versions_file')
        self.versions = paths.expand(vf) if isinstance(vf, str) and vf.strip() else None
        self.timeout = int(self.cfg.get('timeout_s') or 120)

    def caminhos(self):
        return [p for p in (self.repo, self.versions and os.path.dirname(self.versions)) if p]

    def git(self, *args):
        r = subprocess.run(['git', '-C', self.repo, *args], text=True, capture_output=True, timeout=self.timeout,
                           env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        if r.returncode: raise RuntimeError(f'git {args[0]}: ' + ((r.stderr or r.stdout).strip().splitlines() or ['?'])[-1][:200])
        return r.stdout.strip()

    def pull(self):
        """(old head, new head) after a fast-forward of the copy; raises with the reason it was left alone."""
        cur = self.git('rev-parse', '--abbrev-ref', 'HEAD')
        if cur != self.branch: raise RuntimeError(f'copia na branch {cur}, nao em {self.branch}')
        if self.git('status', '--porcelain', '--untracked-files=no'): raise RuntimeError('copia com mudancas sem commit')
        old = self.git('rev-parse', '--short', 'HEAD')
        self.git('merge', '--ff-only', '-q', f'{self.remote}/{self.branch}')
        return old, self.git('rev-parse', '--short', 'HEAD')

    def run(self, ctx):
        if ctx.dry: return None
        st = ctx.s.setdefault('tag_pull', {}).setdefault(self.repo or '?', {})
        try:
            if not self.repo: raise RuntimeError('o gancho tag_pull precisa de "path" no config')
            self.git('fetch', '-q', '--tags', self.remote)
            tags = sorted(t for t in self.git('tag', '-l', self.pattern).split() if t)
        except Exception as e:
            return self.fail(st, [], f'{type(e).__name__}: {e}')
        if 'seen' not in st:                                     # first read: remember what exists, announce nothing
            st['seen'] = tags
            return None
        new = sorted((set(tags) - set(st['seen'])) | (set(st.get('pending') or []) & set(tags)))
        if not new: return None
        try:
            old, head = self.pull()
            waiting = [t for t in new if subprocess.run(['git', '-C', self.repo, 'merge-base', '--is-ancestor', t, 'HEAD'],
                                                         capture_output=True).returncode != 0]
        except Exception as e:
            return self.fail(st, new, str(e))
        done = [t for t in new if t not in waiting]
        st['seen'] = sorted(set(st['seen']) | set(done))
        st['pending'] = waiting
        st.pop('err', None)
        versions = {}
        if done and self.versions:
            try: versions = update_versions(self.versions, done)
            except OSError as e: versions = {'erro': str(e)[:200]}
        if not done: return self.fail(st, waiting, f'tag fora da {self.branch} depois do pull')
        return {'kind': 'new', 'tags': done, 'from': old, 'to': head, 'versions': versions, 'waiting': waiting,
                'repo': self.repo}

    def fail(self, st, tags, msg):
        st['pending'] = sorted(set(st.get('pending') or []) | set(tags))
        if st.get('err') == msg: return None                     # same reason as last tick: already told
        st['err'] = msg
        return {'kind': 'error', 'tags': tags, 'error': msg[:300], 'repo': self.repo}

    def texto(self, v):
        if not v: return None
        name = os.path.basename(os.path.normpath(v['repo'] or '?'))
        if v['kind'] == 'error':
            out = [f'== TAG NOVA: copia em uso NAO atualizada ({name})', f'-- {v["error"]}']
            if v['tags']: out.append('-- tags esperando: ' + ', '.join(v['tags']))
            out.append('-- nada foi forcado; tento de novo no proximo tick')
            return '\n'.join(out)
        out = [f'== TAG NOVA ({len(v["tags"])}) em {name}: copia em uso atualizada ({v["from"]} -> {v["to"]})']
        out += [f'-- {t}' for t in v['tags']]
        if v['versions'].get('erro'): out.append(f'-- arquivo de versoes NAO atualizado: {v["versions"]["erro"]}')
        elif v['versions']: out.append('-- versoes anotadas: ' + ', '.join(f'{p} {x}' for p, x in sorted(v['versions'].items())))
        if v['waiting']: out.append('-- ainda fora da copia: ' + ', '.join(v['waiting']))
        out.append('-- diga ao usuario quais runners precisam ser relancados para pegar a versao nova (nao relance runner de outro agent)')
        return '\n'.join(out)
