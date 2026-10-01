#!/usr/bin/env python3
"""Hook alertas_azure: prints == ALERTAS (Azure Monitor alert e-mails turned into state by the e-mail source with
"alertas_azure": true) right after the source blocks. Read-only, runs on dry ticks too.

Config ("hooks": [{"type": "alertas_azure", "fonte": "email", ...}]):
  fonte               result key of the e-mail source (default "email")
  nao_acordar         [rule names] (PLN0160): rules that swing all day with healthy pods. Their ATIVOU, AINDA ATIVO and
                      desativou lines leave == ALERTAS and go to `== ALERTAS SEM ACORDAR (n)`, which the runner never
                      wakes the session for: the lines go to cache/runner/avisos.log and wait in cache/runner/adiado for
                      the next wake (`== SEM ACAO`). The name is compared whole, ignoring case and surrounding spaces
  nao_acordar_junto   [hook names] (default []): hooks whose block only exists because of these alerts (a Container
                      Insights query, say). When every alert of the tick is in nao_acordar, their block goes inside the
                      quiet block, indented, and does not wake either. A hook's name is its "nome", or its type
  ambiente            cluster environment of these rules (a key of the k8s source's "envs"); empty = any environment
  fonte_clusters      result key of the k8s source (default "clusters")
A pod in trouble in that environment in the same tick (a new pod problem or a restart, from the k8s source) lifts the
silence: the alerts go in == ALERTAS and every block stays as it is, so the session wakes as before.
"""
import io, contextlib

from adapters import Gancho as Base


def _nomes(v):
    return {str(x).strip().lower() for x in (v or []) if str(x).strip()}


class Gancho(Base):
    tipo = 'alertas_azure'
    posicao = 'fontes'
    roda_em_dry = True

    def configura(self):
        self.fonte = self.cfg.get('fonte', 'email')
        self.silenciar = _nomes(self.cfg.get('nao_acordar'))
        self.junto = [str(x).strip() for x in (self.cfg.get('nao_acordar_junto') or []) if str(x).strip()]
        self.ambiente = str(self.cfg.get('ambiente') or '').strip().lower()
        self.fonte_clusters = self.cfg.get('fonte_clusters', 'clusters')

    def pod_em_problema(self, ctx):
        """True when the k8s source brought a new pod problem or a restart in this hook's environment (any, when empty)."""
        envs = ((ctx.res.get(self.fonte_clusters) or {}).get('envs') or {}) if ctx else {}
        for env, v in envs.items():
            if self.ambiente and str(env).lower() != self.ambiente: continue
            if any(str(p.get('k', '')).startswith('pod:') for p in v.get('novos') or []) or v.get('restarts'): return True
        return False

    def run(self, ctx):
        evs = (ctx.res.get(self.fonte) or {}).get('alertas') or None
        if not evs: return None
        quietos = [] if not self.silenciar or self.pod_em_problema(ctx) else \
            [e for e in evs if str(e.get('regra', '')).strip().lower() in self.silenciar]
        return {'acorda': [e for e in evs if e not in quietos], 'quietos': quietos}

    def texto(self, v):
        from adapters.gmail import imprime_alertas
        if isinstance(v, list): v = {'acorda': v, 'quietos': []}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): imprime_alertas(v['acorda'])
        out = buf.getvalue().rstrip('\n')
        if v['quietos']:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf): imprime_alertas(v['quietos'])
            linhas = buf.getvalue().rstrip('\n').split('\n')[1:]            # without the == ALERTAS header
            q = f'== ALERTAS SEM ACORDAR ({len(v["quietos"])})\n' + '\n'.join('   ' + l.lstrip() for l in linhas)
            out = f'{out}\n\n{q}' if out else q
        return out or None

    def ajusta_textos(self, ctx, valores, textos):
        """After every hook ran: the blocks of nao_acordar_junto go inside the quiet block when every alert of the
        tick is silenced (a rule outside the list, or a pod in trouble, keeps them as they are)."""
        v = valores.get(self.nome)
        if not (self.junto and isinstance(v, dict) and v['quietos'] and not v['acorda']): return
        for nome in self.junto:
            t = textos.get(nome)
            if not t or nome == self.nome: continue
            corpo = '\n'.join('   ' + l for l in t.split('\n') if l.strip())
            textos[nome] = f'== ALERTAS SEM ACORDAR ({nome}, so por {", ".join(sorted({e["regra"] for e in v["quietos"]}))})\n{corpo}'
