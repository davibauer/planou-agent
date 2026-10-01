"""Helper shared by the list sources (jira, gitlab, ado_workitems): an item only leaves the list when the origin
confirms it. Not a source (no Fonte): the sources import it as `from adapters import sumidos`.

Why: a list API sometimes answers empty (or short) with no error. Comparing that answer with the previous tick listed
every open item under "SAIRAM DA LISTA" and saved an empty list, so the next tick showed them all as new (the github
source in work-watch 0.33.0, fixed in 0.33.1; the same idea is applied here).

Rule:
  - an item missing from this read that the origin confirms is out (closed, done, merged, reassigned, removed) leaves
    now, with 'fim' = how it left;
  - otherwise it stays in the list with its previous record and a counter 'ausente' (reads in a row without it); on the
    AUSENTE_MAX-th read in a row without it, it leaves with 'fim' = FORA_DA_LISTA. An empty read counts too: three empty
    answers in a row are taken as the truth;
  - the counter is not copied forward: when the item comes back in a read, its fresh record has none;
  - a saved list that is empty outside the baseline in a state written before this helper (no MARCA): the items the
    read brings back that were created before the previous tick are recomposed quietly, not new.

A confirmation that fails (network, HTTP error) is "not confirmed": the item stays, and the read still counts.
"""
AUSENTE_MAX = 3                    # reads in a row without the item before it leaves unconfirmed
FORA_DA_LISTA = f'fora da lista em {AUSENTE_MAX} leituras seguidas'
MARCA = 'saida_confirmada'         # state key: this source's saved list is kept by this helper (an empty list is real)


def confirma(sumidos, agora, chave, verifica):
    """sumidos: previous records missing from this read. agora: {key: record} of this read; the items that stay are put
    back in it (previous record + 'ausente'). chave(record) -> key in agora. verifica(records) -> {key: how it left} for
    the ones the origin confirms are out (missing or None = not confirmed); an exception = none confirmed.
    Returns the records that leave, each with 'fim'."""
    if not sumidos: return []
    try: fim = verifica(list(sumidos)) or {}
    except (Exception, SystemExit): fim = {}
    saem = []
    for it in sumidos:
        k = chave(it)
        if fim.get(k):
            saem.append({**{c: v for c, v in it.items() if c != 'ausente'}, 'fim': fim[k]}); continue
        n = int(it.get('ausente') or 0) + 1
        if n >= AUSENTE_MAX:
            saem.append({**{c: v for c, v in it.items() if c != 'ausente'}, 'fim': FORA_DA_LISTA}); continue
        agora[k] = {**it, 'ausente': n}
    return saem


def recompoe(st, lista_antes, novos, criado, desde):
    """Splits the new items into (new, recomposed). Recomposed = created before `desde` (the previous tick) when the saved
    list is empty and the state has no MARCA (it may be the empty list a bad read saved before this helper existed).
    criado(record) -> datetime or None (None counts as new)."""
    if lista_antes or st.get(MARCA) or not novos or desde is None: return list(novos), []
    velhos = [it for it in novos if (criado(it) or desde) < desde]
    return [it for it in novos if it not in velhos], velhos


def marca(st):
    """Call when saving the list (not dry)."""
    st[MARCA] = 1
