#region imports
from AlgorithmImports import *
from SymbolManager import SymbolManager
from datetime import timedelta
#endregion

class VwmaCrossoverStrategy(QCAlgorithm):
    """
    OVERHAULED: This version uses the powerful V1 entry engine but executes
    trades as Bull Put Spreads for defined, controlled risk.
    FIXED: Uses a robust fill-handling system to correctly manage spread orders and fixes API errors.
    """

    def initialize(self):
        """Initial algorithm setup"""
        self.set_start_date(2023, 1, 1)
        self.set_end_date(2024, 1, 1)
        self.set_cash(100000)
        self.set_time_zone("America/New_York")

        # --- Tickers and Configuration (V1) ---
        self.regime_etf = "SOXX"
        self.bull_etf = "SOXL"
        self.bear_etf = "SOXS"
        
        # --- Indicator Parameters (V1) ---
        self.regime_slow_period = 21
        self.regime_fast_period = 8
        self.regime_adx_period = 14
        self.instrument_slow_period = 21
        self.instrument_fast_period = 8

        # --- Portfolio and Risk Parameters ---
        self.max_concurrent_trades = 10
        self.max_total_allocation = 0.8 
        self.allocation_per_trade = self.max_total_allocation / self.max_concurrent_trades
        self.profit_target_percentage = 0.95 # From your last successful test
        self.spread_width = 2.0 

        # --- General Strategy Parameters (V1) ---
        self.min_dte = 7
        self.max_dte = 14
        self.roll_days_trigger = 3
        self.adx_trend_threshold = 20
        
        # --- Data Setup ---
        self.soxx = self.add_equity(self.regime_etf, Resolution.HOUR).symbol
        
        self.bull_manager = SymbolManager(self, self.bull_etf, self.min_dte, self.max_dte, self.instrument_slow_period, self.instrument_fast_period, self.spread_width)
        self.bear_manager = SymbolManager(self, self.bear_etf, self.min_dte, self.max_dte, self.instrument_slow_period, self.instrument_fast_period, self.spread_width)

        # --- Regime Indicator Setup ---
        self.soxx_vwma_slow = self.vwma(self.soxx, self.regime_slow_period, Resolution.DAILY)
        self.soxx_vwma_fast = self.vwma(self.soxx, self.regime_fast_period, Resolution.DAILY)
        self.soxx_adx = self.adx(self.soxx, self.regime_adx_period, Resolution.DAILY)

        # --- State Tracking ---
        self.open_spreads = {}
        self.pending_entry_symbols = set()
        self.pending_fills = {} # Tracks partially filled spreads
        self.last_trade_date = None

        self.set_warm_up(timedelta(days=self.regime_slow_period + 5))
        self.last_execution_time = self.time

    def on_data(self, slice: Slice):
        """Main event handler, throttled to run once per hour."""
        if self.time < self.last_execution_time + timedelta(hours=1):
            return
        self.last_execution_time = self.time
        
        if self.is_warming_up: return
        
        self.execute_strategy(slice)

    def execute_strategy(self, slice: Slice):
        """Main logic for managing trades and finding new ones."""

        if self.open_spreads:
            for short_leg_symbol_str, trade_data in list(self.open_spreads.items()):
                self.check_roll_condition(SymbolCache.get_symbol(short_leg_symbol_str))

        if self.last_trade_date == self.time.date(): return

        if len(self.open_spreads) + len(self.pending_entry_symbols) < self.max_concurrent_trades:
            if not self.soxx_adx.is_ready or not self.soxx_vwma_fast.is_ready: return
            adx_value = self.soxx_adx.current.value
            if adx_value < self.adx_trend_threshold: return

            soxx_daily_bull = self.soxx_vwma_fast.current.value > self.soxx_vwma_slow.current.value
            
            manager = self.bull_manager if soxx_daily_bull else self.bear_manager
            margin_per_spread = self.spread_width * 100
            manager.attempt_trade_entry(self.allocation_per_trade, slice, margin_per_spread)

    def check_roll_condition(self, short_leg_symbol):
        option_security = self.securities.get(short_leg_symbol)
        if not option_security or not option_security.symbol.underlying: return

        underlying_price = self.securities[option_security.symbol.underlying].price
        
        is_itm = underlying_price <= option_security.symbol.id.strike_price
        dte = (option_security.expiry - self.time).days

        if is_itm and dte <= self.roll_days_trigger:
            self.log(f"ROLL TRIGGER: Spread at {short_leg_symbol.value} is ITM with {dte} DTE. Liquidating.")
            self.liquidate_spread(short_leg_symbol)

    def on_order_event(self, order_event):
        order = self.transactions.get_order_by_id(order_event.order_id)
        if order is None: return
        
        short_leg_symbol_str = order.tag
        if not short_leg_symbol_str: return

        if order_event.status in [OrderStatus.CANCELED, OrderStatus.INVALID]:
            if short_leg_symbol_str in self.pending_entry_symbols:
                self.pending_entry_symbols.remove(short_leg_symbol_str)
            if short_leg_symbol_str in self.pending_fills:
                del self.pending_fills[short_leg_symbol_str]
            return

        if order_event.status != OrderStatus.FILLED: return

        is_entry_order = short_leg_symbol_str in self.pending_entry_symbols
        is_exit_order = short_leg_symbol_str in self.open_spreads

        if is_entry_order:
            self.handle_spread_entry_fill(order)
        elif is_exit_order:
            self.handle_spread_exit_fill(short_leg_symbol_str)

    def handle_spread_entry_fill(self, order):
        """Robustly handles the filling of spread legs."""
        short_leg_symbol_str = order.tag
        
        if short_leg_symbol_str in self.pending_fills:
            partial_fill_data = self.pending_fills.pop(short_leg_symbol_str)
            first_leg_order = partial_fill_data['order']
            
            # Determine which leg is which
            is_call_spread = first_leg_order.symbol.id.option_right == OptionRight.CALL
            
            if is_call_spread:
                long_order = order if order.direction == OrderDirection.BUY else first_leg_order
                short_order = order if order.direction == OrderDirection.SELL else first_leg_order
                net_debit = long_order.price - short_order.price
                
                # Using short leg string as key
                self.open_spreads[str(short_order.symbol)] = {
                    'net_cost': net_debit,
                    'long_leg_symbol_str': str(long_order.symbol)
                }
                self.log(f"Bull Call Spread opened: {short_order.symbol}. Net Debit: ${net_debit:.2f}")

            else: # It's a Put Spread
                short_order = order if order.direction == OrderDirection.SELL else first_leg_order
                long_order = order if order.direction == OrderDirection.BUY else first_leg_order
                net_credit = short_order.price - long_order.price

                if net_credit <= 0:
                    self.liquidate_spread(short_order.symbol)
                    return
                
                self.open_spreads[str(short_order.symbol)] = {
                    'net_cost': -net_credit, # Store as negative for consistency
                    'long_leg_symbol_str': str(long_order.symbol)
                }
                self.log(f"Bull Put Spread opened: {short_order.symbol}. Net Credit: ${net_credit:.2f}")

            self.pending_entry_symbols.remove(short_leg_symbol_str)
            self.last_trade_date = self.time.date()
            self.set_spread_profit_taker(str(short_order.symbol))

        else:
            self.pending_fills[short_leg_symbol_str] = {'order': order}

    def set_spread_profit_taker(self, short_leg_symbol_str):
        """Creates GTC limit orders to close the spread at a profit."""
        if short_leg_symbol_str not in self.open_spreads: return
        
        trade_data = self.open_spreads[short_leg_symbol_str]
        long_leg_symbol = SymbolCache.get_symbol(trade_data['long_leg_symbol_str'])
        short_leg_symbol = SymbolCache.get_symbol(short_leg_symbol_str)
        net_cost = trade_data['net_cost']

        if net_cost < 0: # Credit Spread
            net_credit = -net_cost
            profit_target_debit = round(net_credit * (1 - self.profit_target_percentage), 2)
            if profit_target_debit < 0.01: profit_target_debit = 0.01
            
            # Buy back short, sell long
            self.limit_order(short_leg_symbol, -self.portfolio[short_leg_symbol].quantity, profit_target_debit, tag=short_leg_symbol_str)
            self.limit_order(long_leg_symbol, -self.portfolio[long_leg_symbol].quantity, 0.01, tag=short_leg_symbol_str)
        else: # Debit Spread
            max_profit = self.spread_width - net_cost
            target_credit = round(net_cost + (max_profit * self.profit_target_percentage), 2)
            
            # Sell short, sell long
            self.limit_order(short_leg_symbol, -self.portfolio[short_leg_symbol].quantity, target_credit, tag=short_leg_symbol_str)
            self.limit_order(long_leg_symbol, -self.portfolio[long_leg_symbol].quantity, 0.01, tag=short_leg_symbol_str)

        self.log(f"Submitted profit taker orders for spread {short_leg_symbol_str}.")

    def handle_spread_exit_fill(self, short_leg_symbol_str):
        """Checks if both legs of a closing spread order have filled."""
        if short_leg_symbol_str not in self.open_spreads: return

        trade_data = self.open_spreads[short_leg_symbol_str]
        long_leg_symbol = SymbolCache.get_symbol(trade_data['long_leg_symbol_str'])
        short_leg_symbol = SymbolCache.get_symbol(short_leg_symbol_str)
        
        if not self.portfolio[short_leg_symbol].invested and not self.portfolio[long_leg_symbol].invested:
            del self.open_spreads[short_leg_symbol_str]
            self.log(f"Spread at {short_leg_symbol_str} has been closed.")
            self.transactions.cancel_open_orders(short_leg_symbol)
            self.transactions.cancel_open_orders(long_leg_symbol)

    def liquidate_spread(self, short_leg_symbol):
        short_leg_symbol_str = str(short_leg_symbol)
        
        if short_leg_symbol_str in self.open_spreads:
            trade_data = self.open_spreads[short_leg_symbol_str]
            long_leg_symbol = SymbolCache.get_symbol(trade_data['long_leg_symbol_str'])
            if self.portfolio[short_leg_symbol].invested:
                self.market_order(short_leg_symbol, -self.portfolio[short_leg_symbol].quantity, tag=short_leg_symbol_str)
            if self.portfolio[long_leg_symbol].invested:
                self.market_order(long_leg_symbol, -self.portfolio[long_leg_symbol].quantity, tag=short_leg_symbol_str)
        
        if short_leg_symbol_str in self.pending_entry_symbols:
             self.transactions.cancel_open_orders(short_leg_symbol)

