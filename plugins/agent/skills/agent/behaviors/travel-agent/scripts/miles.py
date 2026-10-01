#!/usr/bin/env python3
"""Miles math, with no I/O: what the points are worth, transfer bonuses, break-even and the best cash/miles combination.

The points are valued by their cash alternative (cashback): using 1 point on an award means giving up `per_point` in cash.
A transfer bonus of b% turns 1 point into ratio x (1 + b) miles, so every mile gets cheaper by that factor. That is why a
bonus is a strategy: with 100%, the same balance buys twice the miles and the break-even award doubles.
"""
import datetime as dt


def per_point(points_cfg):
    b, v = points_cfg.get('balance') or 0, points_cfg.get('cash_value') or 0
    return v / b if b and v else 0


def active_bonus(bonuses, program, today=None):
    """% of the bonus in force for the program (0 when there is none or it expired)."""
    today = today or dt.date.today().isoformat()
    b = (bonuses or {}).get(program) or {}
    return b.get('pct', 0) if b.get('pct') and (not b.get('until') or b['until'] >= today) else 0


def miles_per_point(points_cfg, bonuses, program, today=None):
    ratio = ((points_cfg.get('programs') or {}).get(program) or {}).get('ratio', 1.0)
    return ratio * (1 + active_bonus(bonuses, program, today) / 100)


def break_even(cash_pp, taxes_pp, pp, mpp):
    """Miles per person at which the award costs the same as paying cash (above it, cash wins)."""
    return max(0, (cash_pp - taxes_pp) / pp * mpp) if pp else 0


def combos(n, cash_pp, awards, points_cfg, bonuses, today=None):
    """Every way of flying n people: all cash, or k people on one award option and n-k in cash, while the balance lasts.

    awards: [{program, miles, taxes, note}] per person, round trip. Returns options sorted by real cost (cash out + the cash
    value of the points used); each option: {k, program, miles, cost, cash_out, points, left, note}.
    """
    pp, balance = per_point(points_cfg), points_cfg.get('balance') or 0
    out = [{'k': 0, 'program': '', 'miles': 0, 'cost': n * cash_pp, 'cash_out': n * cash_pp, 'points': 0, 'left': balance, 'note': ''}]
    for aw in awards or []:
        mpp = miles_per_point(points_cfg, bonuses, aw['program'], today)
        pts = aw['miles'] / mpp
        for k in range(1, n + 1):
            need = k * pts
            if need > balance: break
            cash_out = k * aw['taxes'] + (n - k) * cash_pp
            out.append({'k': k, 'program': aw['program'], 'miles': aw['miles'], 'cost': cash_out + need * pp,
                        'cash_out': cash_out, 'points': need, 'left': balance - need, 'note': aw.get('note', '')})
    return sorted(out, key=lambda o: (o['cost'], o['cash_out']))
