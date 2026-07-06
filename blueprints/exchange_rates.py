"""Exchange-rate management. Only the pricing person (edit right) can change a
rate; everyone else is read-only and blocked server-side. Rate changes are
versioned, logged, and shown with an impact preview before commit."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import ExchangeRate, Pricelist, Offer
from services.security import edit_required
from services.audit import log
from services import currency as cx

bp = Blueprint("exchange_rates", __name__, url_prefix="/exchange-rates")


@bp.route("/")
@login_required
def index():
    rates = db.session.scalars(
        db.select(ExchangeRate).order_by(ExchangeRate.quote_ccy,
                                         ExchangeRate.effective_date.desc())).all()
    # current rate per quote currency
    current_rates = {}
    for q in {r.quote_ccy for r in rates}:
        current_rates[q] = cx.get_rate(q)
    return render_template("exchange_rates/index.html", rates=rates,
                           current_rates=current_rates, today=date.today(),
                           can_edit=current_user.may_edit_prices)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@edit_required
def new():
    if request.method == "POST":
        quote = (request.form.get("quote_ccy") or "USD").strip().upper()
        try:
            rate = Decimal(request.form.get("rate", "0"))
        except InvalidOperation:
            flash("Rate must be a number.", "danger")
            return render_template("exchange_rates/new.html", form=request.form, preview=None)
        eff = _parse_date(request.form.get("effective_date")) or date.today()
        exp = _parse_date(request.form.get("expiry_date"))
        commit = request.form.get("commit") == "1"

        # Impact preview
        prev = cx.get_rate(quote, on=eff)
        impact = _build_impact(quote, prev.rate if prev else None, rate)

        if not commit:
            return render_template("exchange_rates/new.html", form=request.form,
                                   preview=impact, quote=quote, new_rate=rate,
                                   eff=eff, exp=exp,
                                   old_rate=(prev.rate if prev else None))

        # Expire any open current rate for this pair the day before the new one.
        if prev and (prev.expiry_date is None or prev.expiry_date >= eff):
            prev.expiry_date = eff
        row = ExchangeRate(base_ccy="UGX", quote_ccy=quote, rate=rate,
                           effective_date=eff, expiry_date=exp,
                           created_by=current_user.id)
        db.session.add(row)
        log("rate_change", "exchange_rate", None, field=f"UGX/{quote}",
            old_value=(prev.rate if prev else None), new_value=rate,
            detail=f"effective {eff}" + (f" to {exp}" if exp else ""))
        db.session.commit()
        flash("Exchange rate saved and logged. Issued offers keep their stamped rate.", "success")
        return redirect(url_for("exchange_rates.index"))

    return render_template("exchange_rates/new.html", form={}, preview=None)


def _build_impact(quote, old_rate, new_rate):
    usd_lists = db.session.scalars(
        db.select(Pricelist).filter_by(currency=quote)).all()
    ugx_lists = db.session.scalars(
        db.select(Pricelist).filter_by(currency="UGX")).all()
    draft_offers = db.session.scalars(
        db.select(Offer).filter_by(currency=quote, status="draft")).all()
    pct = None
    if old_rate and float(old_rate) != 0:
        pct = (float(new_rate) - float(old_rate)) / float(old_rate) * 100.0
    return {
        "old_rate": old_rate,
        "new_rate": new_rate,
        "pct": pct,
        "usd_lists": usd_lists,
        "n_ugx_lists": len(ugx_lists),
        "draft_offers": draft_offers,
        "quote": quote,
    }


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
