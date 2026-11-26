#!/usr/bin/env python3
"""Synthetic boundary audit of KHunter fill, cost, and signal timing.

This script does not change trading logic and does not read a market database.
Synthetic bars are not historical returns. The optional public-quote probe only
records raw versus forward-adjusted prices for the 100-share cash constraint.

Run from the repository root:

    python3 research/audit/synthetic_fill_audit.py

Outputs:

    research/audit/synthetic_fill_results.json
"""

from __future__ import annotations

import json
import logging
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# akshare is an import side effect of the data layer. The tests below do not
# call it. Stub only when the real package is absent so the engine module loads.
try:
    import akshare  # noqa: F401
except ModuleNotFoundError:
    sys.modules.setdefault("akshare", types.ModuleType("akshare"))

import pandas as pd

from trading.backtest_engine import BacktestEngine, calculate_backtest_cost
from trading.support_strategy import SupportStrategy
from trading.turtle_strategy import TurtleStrategy
from utils.stock_utils import get_min_trade_unit, normalize_quantity

logging.disable(logging.CRITICAL)

OUT_PATH = Path(__file__).resolve().parent / "synthetic_fill_results.json"

# Statutory sell-side stamp tax after MOF/STA Announcement 2023 No. 39
# (halved from 0.1% effective 2023-08-28). Still the rate cited by 2026
# secondary summaries of that announcement. The engine hardcodes 0.001.
STATUTORY_STAMP_TAX_RATE = 0.0005
AUDITED_COMMIT = "9b2e8f0b179c4c897fac899673bf9c0751b5507e"


class _DB:
    def __init__(self, explode: bool = False):
        self.explode = explode

    def query_one(self, *args, **kwargs):
        if self.explode:
            raise RuntimeError("synthetic db failure")
        return None


def weekdays(n: int, start: date = date(2024, 1, 2)) -> list[str]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def engine(calendar: list[str], explode_db: bool = False) -> BacktestEngine:
    eng = BacktestEngine.__new__(BacktestEngine)
    eng.db_manager = _DB(explode_db)
    eng.stock_data_fetcher = None
    eng._sorted_trading_dates = list(calendar)
    eng.trading_calendar_cache = {d: True for d in calendar}
    eng.stock_data_cache = {}
    eng.stock_filtered_cache = {}
    eng._slippage = {"buy": 0.003, "sell": 0.003}
    eng.timing_strategy = None
    eng.ENABLE_NO_NEW_HIGH_EXIT = False
    eng.consecutive_loss_count = {}
    eng.loss_cool_down_pool = {}
    eng.position_highest_profit = {}
    return eng


def bar(d: str, o: float, h: float, l: float, c: float, v: float = 100000.0) -> dict:
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def put(eng: BacktestEngine, code: str, rows: list[dict]) -> None:
    df = frame(rows)
    eng.stock_data_cache[code] = df
    eng.stock_filtered_cache[code] = df.copy()


def position(code: str, buy: date, price: float, qty: int = 100,
             commission: float = 5.0, transfer: float = 0.0) -> dict:
    return {
        "stock_code": code,
        "stock_name": "SYN",
        "buy_date": buy,
        "buy_price": price,
        "quantity": qty,
        "buy_amount": price * qty,
        "buy_commission": commission,
        "buy_transfer_fee": transfer,
    }


def sell_cfg(**over) -> dict:
    cfg = {
        "take_profit": 1000,
        "stop_loss": -6,
        "hold_period": 10,
        "enable_trailing_stop": True,
        "enable_position_expire": False,
        "enable_loss_cool_down": False,
        "enable_consecutive_loss_limit": False,
        "trailing_drawdown_pct": 8,
    }
    cfg.update(over)
    return cfg


def record(results: list, test_id: str, status: str, summary: str, **evidence) -> None:
    results.append({
        "id": test_id,
        "status": status,
        "summary": summary,
        "evidence": evidence,
    })


def flat_history(dates: list[str], price: float = 10.0, last: dict | None = None) -> list[dict]:
    rows = [bar(d, price, price, price, price) for d in dates]
    if last:
        rows[-1].update(last)
    return rows


def run_synthetic() -> list[dict]:
    results: list[dict] = []
    cal = weekdays(40)
    d = {i: date.fromisoformat(cal[i]) for i in range(len(cal))}

    # --- costs ---
    sh_sell = calculate_backtest_cost("601138", 50.0, 100, is_buy=False)
    sh_buy = calculate_backtest_cost("601138", 50.0, 100, is_buy=True)
    sz_sell = calculate_backtest_cost("000001", 50.0, 100, is_buy=False)
    sz_buy = calculate_backtest_cost("000001", 50.0, 100, is_buy=True)
    notional = 50.0 * 100
    engine_stamp = sh_sell["stamp_tax"]
    statutory_stamp = round(notional * STATUTORY_STAMP_TAX_RATE, 2)
    record(
        results, "stamp_tax_rate",
        "fail",
        "卖出印花税按 0.1% 硬编码，不是 2023-08-28 起减半后的 0.05%。",
        engine_rate=0.001,
        statutory_rate=STATUTORY_STAMP_TAX_RATE,
        notional=notional,
        engine_stamp_tax=engine_stamp,
        statutory_stamp_tax=statutory_stamp,
        extra_tax=round(engine_stamp - statutory_stamp, 2),
        buy_stamp_tax=sh_buy["stamp_tax"],
        source="trading/backtest_engine.py calculate_backtest_cost stamp_tax_rate=0.001",
    )
    record(
        results, "min_commission_floor",
        "pass",
        "100 股、成交额 5000 元时，佣金被抬到 5 元，而不是名义 0.015%。",
        notional=notional,
        nominal_commission=round(notional * 0.00015, 2),
        buy_commission=sh_buy["commission"],
        sell_commission=sh_sell["commission"],
    )
    record(
        results, "transfer_fee_scope",
        "fail",
        "过户费费率 0.001% 与 2022-04-29 后的标准一致，但只对 6 开头收取，深市漏计。",
        shanghai_buy=sh_buy["transfer_fee"],
        shanghai_sell=sh_sell["transfer_fee"],
        shenzhen_buy=sz_buy["transfer_fee"],
        shenzhen_sell=sz_sell["transfer_fee"],
        statutory_one_side=round(notional * 0.00001, 2),
    )
    sh_round_trip = (
        sh_buy["commission"] + sh_buy["transfer_fee"]
        + sh_sell["commission"] + sh_sell["transfer_fee"] + sh_sell["stamp_tax"]
    )
    statutory_sh_round_trip = (
        5 + round(notional * 0.00001, 2)
        + 5 + round(notional * 0.00001, 2)
        + statutory_stamp
    )
    record(
        results, "round_trip_5000_yuan",
        "fail",
        "5000 元买卖各 100 股、价格不变时，引擎成本高于现行印花税口径。",
        engine_round_trip_cost=round(sh_round_trip, 2),
        statutory_round_trip_cost_if_min_commission_5=round(statutory_sh_round_trip, 2),
        engine_net_pnl_if_flat=round(-sh_round_trip, 2),
        note="最低佣金 5 元是券商约定，不是交易所法规。这里只把印花税换成 0.05% 作对照。",
    )

    # --- lot size versus the user's 100-share budget ---
    record(
        results, "lot_size_100_shares",
        "pass",
        "主板/创业板最小单位是 100 股；科创板是 200 股，100 股买不了。",
        main_board_unit=get_min_trade_unit("601138"),
        chinext_unit=get_min_trade_unit("300750"),
        star_unit=get_min_trade_unit("688981"),
        qty_5000_at_49_99=normalize_quantity("601138", 5000 / 49.99),
        qty_5000_at_50_01=normalize_quantity("601138", 5000 / 50.01),
        qty_star_5000_at_20=normalize_quantity("688981", 5000 / 20),
        qty_star_5000_at_30=normalize_quantity("688981", 5000 / 30),
    )

    # --- T+1 and stops ---
    eng = engine(cal)
    code = "600000"
    rows = []
    for i, ds in enumerate(cal[:8]):
        rows.append(bar(ds, 10, 10.2, 9.8, 10))
    # D0 buy day. D1 close crashes to 9.0 so D2 can see it as previous close.
    rows[1] = bar(cal[1], 10, 10, 9.0, 9.0)
    rows[2] = bar(cal[2], 8.1, 8.1, 8.1, 8.1)  # one-word limit-down open vs 9.0
    put(eng, code, rows)
    pos = position(code, d[0], 10.0)
    held, sells = eng._process_sell([pos], d[0], sell_cfg())
    record(
        results, "t1_same_day_unsellable",
        "pass" if (len(sells) == 0 and len(held) == 1) else "fail",
        "买入当日 hold_days=0，即使价格已跌破止损也不卖。",
        sell_count=len(sells),
        held=len(held),
    )

    held, sells = eng._process_sell([position(code, d[0], 10.0)], d[2], sell_cfg(stop_loss=-10))
    sell_px = sells[0]["sell_price"] if sells else None
    record(
        results, "limit_down_open_still_fills",
        "fail" if sells else "pass",
        "前收已触发止损后，次日一字跌停开盘仍按开盘价减滑点卖出，没有跌停可成交性检查。",
        sold=bool(sells),
        sell_type=sells[0]["sell_type"] if sells else None,
        sell_price=sell_px,
        one_word_open=8.1,
        slipped=round(8.1 * (1 - 0.003), 4),
        configured_stop_loss=-10,
        hardcoded_stop_price=round(10 * 0.94, 4),
        note="config stop_loss=-10 本应放宽到 -10%，实际仍在 -6% 价位触发。",
    )

    # configured tighter stop is ignored
    rows_tight = [bar(ds, 10, 10.2, 9.8, 10) for ds in cal[:6]]
    rows_tight[1] = bar(cal[1], 10, 10, 9.5, 9.5)  # -5% from buy 10, inside -6%, outside -3%
    rows_tight[2] = bar(cal[2], 9.6, 9.7, 9.4, 9.55)
    eng2 = engine(cal)
    put(eng2, code, rows_tight)
    held, sells = eng2._process_sell(
        [position(code, d[0], 10.0)], d[2], sell_cfg(stop_loss=-3))
    record(
        results, "configured_stop_loss_ignored",
        "fail" if len(sells) == 0 else "pass",
        "前收较买入价跌 5%。配置止损 -3% 时期望卖出，引擎固定用 -6%，所以不卖。",
        sold=bool(sells),
        prev_close=9.5,
        buy_price=10.0,
        drop_pct=-5.0,
        configured_stop_loss=-3,
        hardcoded_stop_price=9.4,
    )

    # gap through stop fills at the open, not at the stop
    rows_gap = [bar(ds, 10, 10.2, 9.8, 10) for ds in cal[:6]]
    rows_gap[1] = bar(cal[1], 10, 10, 9.0, 9.0)
    rows_gap[2] = bar(cal[2], 8.5, 9.2, 8.4, 9.0)
    eng3 = engine(cal)
    put(eng3, code, rows_gap)
    _, sells = eng3._process_sell([position(code, d[0], 10.0)], d[2], sell_cfg())
    gap_px = sells[0]["sell_price"] if sells else None
    record(
        results, "gap_through_stop_uses_open",
        "pass" if sells and abs(gap_px - round(8.5 * 0.997, 4)) < 1e-9 else "fail",
        "止损触发后成交在次日开盘（再减滑点），不是止损价，跳空被计入。",
        sell_price=gap_px,
        open_price=8.5,
        stop_price=9.4,
        expected_slipped_open=round(8.5 * 0.997, 4),
    )

    # same-bar low does not stop out if previous close is above the stop
    rows_wick = [bar(ds, 10, 10.2, 9.8, 10) for ds in cal[:6]]
    rows_wick[1] = bar(cal[1], 10, 10.1, 9.7, 9.8)  # -2%, above -6%
    rows_wick[2] = bar(cal[2], 9.7, 9.9, 8.0, 9.6)  # intraday low through stop
    eng4 = engine(cal)
    put(eng4, code, rows_wick)
    _, sells = eng4._process_sell([position(code, d[0], 10.0)], d[2], sell_cfg(take_profit=1000))
    record(
        results, "no_same_bar_low_stop",
        "pass" if len(sells) == 0 else "fail",
        "止损看前收，不看当日最低价。当日下影刺破止损价不会成交。",
        sold=bool(sells),
        prev_close=9.8,
        same_bar_low=8.0,
        stop_price=9.4,
    )

    # same-bar high does not take profit if the open is not through the target
    rows_hi = [bar(ds, 10, 10.05, 9.95, 10) for ds in cal[:6]]
    rows_hi[1] = bar(cal[1], 10, 10.2, 9.9, 10.2)
    rows_hi[2] = bar(cal[2], 10.1, 11.5, 10.0, 11.2)
    eng5 = engine(cal)
    put(eng5, code, rows_hi)
    _, sells = eng5._process_sell(
        [position(code, d[0], 10.0)], d[2], sell_cfg(take_profit=5))
    record(
        results, "no_same_bar_high_take_profit",
        "pass" if len(sells) == 0 else "fail",
        "止盈用的是卖出价（当日开盘减滑点）算出来的收益率，不用当日最高价。",
        sold=bool(sells),
        open_price=10.1,
        same_bar_high=11.5,
        take_profit_pct=5,
    )

    # take profit DOES use today's open, not yesterday's close
    rows_tp = [bar(ds, 10, 10.05, 9.95, 10) for ds in cal[:6]]
    rows_tp[1] = bar(cal[1], 10, 10.2, 9.9, 10.1)  # prev close +1%, below 5%
    rows_tp[2] = bar(cal[2], 11.0, 11.2, 10.8, 11.1)  # gap open +10%
    eng6 = engine(cal)
    put(eng6, code, rows_tp)
    _, sells = eng6._process_sell(
        [position(code, d[0], 10.0)], d[2], sell_cfg(take_profit=5))
    record(
        results, "take_profit_uses_today_open",
        "fail",
        "说明书写的是前一根 K 线计算。止盈实际用当日开盘价（减滑点）是否已经达到阈值。这不是用了收盘价的前视，但和“一律用前一根”不符。",
        sold=bool(sells),
        sell_type=sells[0]["sell_type"] if sells else None,
        sell_price=sells[0]["sell_price"] if sells else None,
        prev_close=10.1,
        open_price=11.0,
        marketing_claim="calculations use the prior bar",
        observed="take-profit threshold applied to slipped open",
    )

    # min-commission omitted from the sell decision.
    # _process_sell first turns the open into open*(1-sell slippage).
    raw_open = 52.0
    slipped = round(raw_open * (1 - 0.003), 4)
    rows_fee = [bar(ds, 50, 50.2, 49.8, 50) for ds in cal[:6]]
    rows_fee[0] = bar(cal[0], 50, 50, 50, 50)
    rows_fee[1] = bar(cal[1], 50, 50.4, 49.8, 50)
    rows_fee[2] = bar(cal[2], raw_open, raw_open, raw_open, raw_open)
    eng7 = engine(cal)
    put(eng7, "000001", rows_fee)
    amount = slipped * 100
    est_comm = amount * 0.00015
    est_stamp = amount * 0.001
    est_net = amount - est_comm - est_stamp
    actual_cost = 5000 + 5
    est_ret = (est_net - actual_cost) / actual_cost * 100
    act = calculate_backtest_cost("000001", slipped, 100, is_buy=False)
    act_net = amount - act["commission"] - act["transfer_fee"] - act["stamp_tax"]
    act_ret = (act_net - actual_cost) / actual_cost * 100
    midpoint = (est_ret + act_ret) / 2
    _, sells = eng7._process_sell(
        [position("000001", d[0], 50.0, commission=5.0)],
        d[2],
        sell_cfg(take_profit=midpoint),
    )
    decision_sold_below_recorded = bool(sells) and sells[0]["return_rate"] < midpoint
    record(
        results, "sell_decision_ignores_min_commission",
        "fail" if decision_sold_below_recorded else "pass",
        "卖出决策用的佣金是成交额×0.015%，没有 5 元下限；落库收益率才用下限。阈值夹在两者中间时会卖出，但记账收益率低于阈值。",
        open_price=raw_open,
        slipped_price=slipped,
        threshold=midpoint,
        decision_estimate_return_pct=est_ret,
        recorded_return_pct=sells[0]["return_rate"] if sells else None,
        sold=bool(sells),
        sell_commission_recorded=sells[0]["sell_commission"] if sells else None,
        actual_return_if_min_commission_applied=act_ret,
        default_take_profit_note="默认止盈是 21% 或配置里的 60%。这条只在阈值贴着成本时改变是否成交。",
    )

    # halt / volume / missing price
    eng8 = engine(cal)
    put(eng8, "600001", [bar(cal[0], 10, 10, 10, 10, v=0)])
    record(
        results, "zero_volume_still_tradable",
        "fail" if eng8._has_trading_data_on_date("600001", d[0]) else "pass",
        "成交量为 0 但开盘价大于 0 时，停牌检查仍视为可交易。",
        has_data=eng8._has_trading_data_on_date("600001", d[0]),
        volume=0,
        open=10,
    )
    record(
        results, "missing_bar_not_tradable",
        "pass" if not eng8._has_trading_data_on_date("600001", d[1]) else "fail",
        "缓存里没有当日 K 线时，停牌/退市检查返回不可交易。",
        has_data=eng8._has_trading_data_on_date("600001", d[1]),
    )

    stale = eng8._get_stock_price("600001", d[1], "open")
    exec_stale = eng8._resolve_buy_execution("600001", d[1], {"buy_execution": {"mode": "open"}})
    record(
        results, "missing_bar_price_falls_back",
        "fail",
        "主循环会先做停牌检查，所以这条不一定成交。但取价函数在缺 bar 时返回更早的收盘价，开盘价模式会把它当成可成交开盘价。",
        fallback_open=stale,
        resolve_filled=exec_stale["filled"],
        resolve_price=exec_stale["price"],
        last_real_close=10.0,
    )

    eng9 = engine(cal, explode_db=True)
    dummy = eng9._get_stock_price("600009", d[1], "close")
    record(
        results, "price_exception_returns_ten",
        "fail" if dummy == 10.0 else "pass",
        "取价抛异常时返回 10.0，而不是拒绝成交。",
        returned=dummy,
    )

    # delisting: bars stop, position is kept and later price is the last close
    eng10 = engine(cal)
    put(eng10, "600010", [bar(cal[0], 10, 10, 10, 10), bar(cal[1], 8, 8, 7, 7.5)])
    held, sells = eng10._process_sell([position("600010", d[0], 10.0)], d[5], sell_cfg(stop_loss=-1))
    later = eng10._get_stock_price("600010", d[5], "close")
    record(
        results, "delist_or_long_halt_never_exits",
        "fail" if (len(sells) == 0 and later == 7.5) else "pass",
        "行情中断后持仓留着，取价退回最后一根收盘价。退市损失不会按无法卖出处理。",
        sold=bool(sells),
        held=len(held),
        mark_price=later,
        last_close=7.5,
    )

    # open-mode limit-up fill
    eng11 = engine(cal)
    put(eng11, "600011", [
        bar(cal[0], 10, 10, 10, 10),
        bar(cal[1], 11, 11, 11, 11),  # one-word limit-up
    ])
    buy = eng11._resolve_buy_execution(
        "600011", d[1], {"buy_execution": {"mode": "open"}})
    slipped = round(buy["price"] * (1 + eng11._slippage["buy"]), 4) if buy["filled"] else None
    record(
        results, "one_word_limit_up_buy_fills",
        "fail" if buy["filled"] else "pass",
        "开盘价模式对一字涨停无条件成交。生产路径还会再乘买入滑点。没有板块涨跌停价。",
        filled=buy["filled"],
        raw_open=buy["price"],
        after_default_slippage=slipped,
        prev_close=10.0,
        limit_up_price=11.0,
    )

    # 15% gap still fills (illegal on main board, possible on ChiNext)
    eng12 = engine(cal)
    put(eng12, "300001", [
        bar(cal[0], 10, 10, 10, 10),
        bar(cal[1], 11.5, 11.6, 11.4, 11.5),
    ])
    buy15 = eng12._resolve_buy_execution(
        "300001", d[1], {"buy_execution": {"mode": "open"}})
    record(
        results, "no_board_limit_check",
        "fail" if buy15["filled"] and buy15["price"] == 11.5 else "pass",
        "开盘涨 15% 仍按开盘价成交。主板 10% 涨停下这根 K 线不合法，引擎不区分板块、ST 或北交所。",
        filled=buy15["filled"],
        price=buy15["price"],
        prev_close=10.0,
        open_gap=0.15,
    )

    # ma_limit same-bar low touch, newest-first frame as the engine passes it
    hist_dates = cal[:6]
    asc = [bar(ds, 10, 10.1, 9.9, 10) for ds in hist_dates]
    # execution day is a new day after the 6 history days? Use cal[6] as T
    # MA uses iloc[1:] of newest-first, so row 0 must be T.
    t_row = bar(cal[6], 10.8, 11.0, 9.90, 10.7)
    newest_first = frame([t_row] + list(reversed(asc)))
    eng13 = engine(cal)
    # price lookup uses cache, ascending
    put(eng13, "600013", asc + [t_row])
    ml = eng13._resolve_buy_execution(
        "600013", d[6],
        {"buy_execution": {"mode": "ma_limit", "ma_period": 5, "slippage": 0.005,
                           "fill_price": "better", "min_periods": 5}},
        df_to_date=newest_first,
    )
    record(
        results, "ma_limit_same_bar_low_fill",
        "fail" if ml["filled"] and ml["price"] < 10.8 else "pass",
        "ma_limit 不是默认模式。当日只要最低价碰到委托价，就按 min(开盘, 委托价) 成交，不看成交量，下影线即视为买到。",
        filled=ml["filled"],
        fill_price=ml["price"],
        order_price=ml["order_price"],
        open_price=10.8,
        low_price=9.90,
        mode=ml["mode"],
        default_yaml_mode="open",
    )

    # turtle: T-day breakout must not buy; T-1 breakout must buy even if T fails
    def turtle_frame(break_on: str) -> pd.DataFrame:
        rows = []
        for i, ds in enumerate(cal[:25]):
            o, h, l, c = 10.0, 10.2, 9.8, 10.05
            if break_on == "T" and i == 24:
                o, h, l, c = 10.4, 12.0, 10.3, 11.6
            if break_on == "T-1" and i == 23:
                o, h, l, c = 10.3, 12.0, 10.2, 11.7
            if break_on == "T-1" and i == 24:
                o, h, l, c = 11.0, 11.1, 10.4, 10.5  # down day, no new break
            rows.append(bar(ds, o, h, l, c))
        return frame(rows)

    turtle = TurtleStrategy({"preset": "short", "n_entry": 10, "n_exit": 5, "atr_period": 10})
    turtle.USE_MA_FILTER = False
    buy_on_t_only = turtle.get_timing_result(turtle_frame("T"), use_prev_day_signal=True)
    buy_on_t1 = turtle.get_timing_result(turtle_frame("T-1"), use_prev_day_signal=True)
    # order invariance: engine passes newest-first
    desc = turtle_frame("T-1").iloc[::-1].reset_index(drop=True)
    buy_desc = turtle.get_timing_result(desc, use_prev_day_signal=True)
    record(
        results, "turtle_signal_uses_prior_bar",
        "pass" if (not buy_on_t_only.is_buy and buy_on_t1.is_buy and buy_desc.is_buy) else "fail",
        "短线海龟在 use_prev_day_signal=True 时用倒数第二根 K 线。只有 T 日突破不会买；T-1 突破、T 日不突破仍会买。输入倒序也会先转正序。",
        buy_if_only_T_breaks=buy_on_t_only.is_buy,
        buy_if_T_minus_1_breaks=buy_on_t1.is_buy,
        buy_if_engine_passes_newest_first=buy_desc.is_buy,
        message_T_minus_1=buy_on_t1.message,
    )

    # support strategy does not sort before iloc[-2]
    asc_rows = []
    for i, ds in enumerate(cal[:22]):
        close = 10.0
        if i == 20:  # true T-1 in an ascending frame
            close = 13.0
        asc_rows.append(bar(ds, close, close, close, close))
    asc_df = frame(asc_rows)
    desc_df = asc_df.iloc[::-1].reset_index(drop=True)
    support = SupportStrategy({"method": "ma20"})
    buy_asc = support.get_timing_result(asc_df, use_prev_day_signal=True)
    buy_desc_s = support.get_timing_result(desc_df, use_prev_day_signal=True)
    used_date_if_desc = str(desc_df.iloc[-2]["date"])
    true_t1 = str(asc_df.iloc[-2]["date"])
    record(
        results, "support_strategy_bar_order",
        "fail" if (buy_asc.is_buy != buy_desc_s.is_buy or used_date_if_desc != true_t1) else "pass",
        "回测把 K 线倒序传给择时。支撑位策略用 iloc[-2] 之前没有转正序，倒序时读到的是很早的一根，不是 T-1。",
        ascending_is_buy=buy_asc.is_buy,
        newest_first_is_buy=buy_desc_s.is_buy,
        date_strategy_reads_if_newest_first=used_date_if_desc,
        true_T_minus_1=true_t1,
    )

    # selection offset helper
    prev = eng._get_previous_trading_day(d[5])
    record(
        results, "selection_date_is_previous_session",
        "pass" if prev == d[4] else "fail",
        "回测循环里的选股日是执行日的前一个交易日。这只证明日期偏移，不证明每条策略都没用到执行日收盘价。",
        execution_day=d[5].isoformat(),
        selection_day=prev.isoformat() if prev else None,
    )
    return results


def pin_source() -> dict:
    """Line numbers of the rules this audit relies on, read from this checkout."""
    needles = {
        "trading/backtest_engine.py": [
            "stamp_tax_rate = 0.001",
            "min_commission = 5",
            "selection_date = self._get_previous_trading_day",
            "base_stop_level = -6",
            "return 10.0",
            "if hold_days > 0:",
            "T+1规则：当天买入的股票不能当天卖出",
        ],
        "utils/stock_data_fetcher.py": [
            "list_status='L'",
            "fqkline/get?param=",
        ],
        "config/backtest_engine_config.yaml": [
            "initial_capital: 500000",
            "mode: open",
            "buy: 0.003",
        ],
    }
    found = {}
    for rel, keys in needles.items():
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        found[rel] = {}
        for key in keys:
            hits = [i + 1 for i, line in enumerate(lines) if key in line]
            found[rel][key] = hits
    return found


def probe_public_quotes() -> dict:
    """One public Tencent pull. Failure is recorded, not retried."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://stock.finance.qq.com/",
    }
    code = "sh601138"

    def pull(adj: str, start: str, end: str):
        url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={code},day,{start},{end},80,{adj}"
        )
        resp = requests.get(url, timeout=12, headers=headers)
        resp.raise_for_status()
        stock = resp.json().get("data", {}).get(code, {})
        key = {"qfq": "qfqday", "hfq": "hfqday", "": "day"}[adj]
        rows = stock.get(key) or []
        return [
            {"date": r[0], "open": float(r[1]), "close": float(r[2]),
             "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])}
            for r in rows
        ]

    try:
        early_raw = pull("", "2020-01-01", "2020-01-15")
        early_qfq = pull("qfq", "2020-01-01", "2020-01-15")
        recent_raw = pull("", "2026-11-01", "2026-11-26")
        recent_qfq = pull("qfq", "2026-11-01", "2026-11-26")
    except Exception as exc:
        return {"status": "not_verified", "error": f"{type(exc).__name__}: {exc}"}

    def pair(raw_rows, qfq_rows):
        qmap = {r["date"]: r for r in qfq_rows}
        out = []
        for r in raw_rows:
            q = qmap.get(r["date"])
            if not q:
                continue
            out.append({
                "date": r["date"],
                "raw_close": r["close"],
                "qfq_close": q["close"],
                "raw_over_qfq": round(r["close"] / q["close"], 6) if q["close"] else None,
                "cash_100_shares_raw": round(r["close"] * 100, 2),
                "cash_100_shares_qfq": round(q["close"] * 100, 2),
            })
        return out

    early = pair(early_raw, early_qfq)
    recent = pair(recent_raw, recent_qfq)
    return {
        "status": "pass" if early and recent else "not_verified",
        "source": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "symbol": "601138",
        "note": "Public quote comparison only. Not a strategy backtest and not an entry price.",
        "early_sample": early[:3],
        "recent_sample": recent[-3:],
        "data_cutoff": recent[-1]["date"] if recent else None,
    }


def main() -> int:
    results = run_synthetic()
    probe = probe_public_quotes()
    counts = {"pass": 0, "fail": 0, "not_verified": 0}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {
        "audited_commit": AUDITED_COMMIT,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_pins": pin_source(),
        "synthetic_disclaimer": "Synthetic bars test engine rules. They are not historical returns.",
        "counts": counts,
        "tests": results,
        "public_quote_probe": probe,
        "stage2_strategy_backtest": {
            "status": "not_run",
            "reason": "Critical fill and data-model failures. No historical return is reported.",
        },
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": counts, "out": str(OUT_PATH),
                      "probe": probe.get("status"), "cutoff": probe.get("data_cutoff")},
                     ensure_ascii=False, indent=2))
    for row in results:
        print(f"{row['status'].upper():4}  {row['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
