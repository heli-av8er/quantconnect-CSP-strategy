#region imports
from AlgorithmImports import *
from SymbolManager import SymbolManager
#endregion


class VwmaCrossoverStrategy(QCAlgorithm):
    """
    This version includes critical fixes for state management to prevent
    trade stacking and handle potential runtime errors from invalid securities.
    """


    def initialize(self):
        """Initial algorithm setup"""
        self.set_start_date(2023, 1, 1)
        self.set_end_date(2024, 1, 1)
        self.set_cash(100000)
        self.set_time_zone("America/New_York")


        # --- Tickers and Configuration ---
        self.regime_etf = "SOXX"
        self.bull_etf = "SOXL"
        self.bear_etf = "SOXS"
        self.lookback_days = 21


        # --- Multi-Position Parameters ---
        self.max_concurrent_trades = 10
        self.max_total_allocation = 1.0
        self.allocation_per_trade = self.max_total_allocation / self.max_concurrent_trades


        # --- General Strategy Parameters ---
        self.min_dte = 7
        self.max_dte = 14
        self.roll_days_trigger = 3
        self.profit_target_price = 0.10
        self.stop_loss_multiplier = -0.50


        # --- Data Setup ---
        self.soxx = self.add_equity(self.regime_etf, Resolution.HOUR).symbol
       
        self.bull_manager = SymbolManager(self, self.bull_etf, self.min_dte, self.max_dte)
        self.bear_manager = SymbolManager(self, self.bear_etf, self.min_dte, self.max_dte)


        # --- Regime Indicator Setup ---
        self.soxx_vwma_slow = self.vwma(self.soxx, 21, Resolution.DAILY)
        self.soxx_vwma_fast = self.vwma(self.soxx, 8, Resolution.DAILY)
        self.soxx_adx = self.adx(self.soxx, 14, Resolution.DAILY)


        # --- State Tracking for Multiple Trades ---
        self.open_trades = {}
        # FIX: Add a set to track symbols with pending entry orders
        self.pending_entry_symbols = set()


        self.set_warm_up(timedelta(days=self.lookback_days))
        self.last_execution_time = self.time


    def on_data(self, slice: Slice):
        """Main event handler, throttled to run once per hour."""
        if self.time < self.last_execution_time + timedelta(hours=1):
            return
        self.last_execution_time = self.time
       
        self.execute_strategy(slice)


    def execute_strategy(self, slice: Slice):
        """Main logic for managing multiple trades and finding new ones."""
        if self.is_warming_up:
            return


        # --- Part 1: Manage Existing Open Positions ---
        if self.open_trades:
            soxx_daily_bull = self.soxx_vwma_fast.current.value > self.soxx_vwma_slow.current.value
           
            for contract_symbol, trade_data in list(self.open_trades.items()):
                # FIX: Add a safety check for the security's existence and validity
                option_security = self.securities.get(contract_symbol)
                if not option_security or not option_security.symbol.underlying:
                    self.log(f"Skipping management for invalid or delisted security: {contract_symbol}")
                    continue


                active_manager = self.bull_manager if contract_symbol.underlying == self.bull_manager.symbol else self.bear_manager
               
                regime_has_flipped = not soxx_daily_bull if active_manager == self.bull_manager else soxx_daily_bull
                instrument_trend_aligned = active_manager.is_trend_aligned()


                exit_reason = active_manager.check_risk_management(
                    option_security,
                    trade_data['entry_price'],
                    self.stop_loss_multiplier,
                    instrument_trend_aligned,
                    regime_has_flipped
                )
               
                if exit_reason:
                    self.liquidate(contract_symbol, exit_reason)
                   
                self.check_roll_condition(contract_symbol)


        # --- Part 2: Look for New Trades if Capacity Allows (IMPROVED GATEKEEPER) ---
        # FIX: Include pending entry symbols in the capacity check to prevent stacking
        if len(self.open_trades) + len(self.pending_entry_symbols) < self.max_concurrent_trades:
            adx_value = self.soxx_adx.current.value
            if adx_value < 20: return


            soxx_daily_bull = self.soxx_vwma_fast.current.value > self.soxx_vwma_slow.current.value
            if soxx_daily_bull:
                self.log(f"Bull Trade Signal")
                self.bull_manager.attempt_trade_entry(self.allocation_per_trade, adx_value, slice)
            else:
                self.log(f"Bear Trade Signal")
                self.bear_manager.attempt_trade_entry(self.allocation_per_trade, adx_value, slice)


    def check_roll_condition(self, contract_symbol):
        # FIX: More robust check for the security's existence
        option_security = self.securities.get(contract_symbol)
        if not option_security or not option_security.symbol.underlying:
            return


        underlying_price = self.securities[option_security.symbol.underlying].price
       
        is_itm = underlying_price <= option_security.symbol.id.strike_price
        dte = (option_security.expiry - self.time).days


        if is_itm and dte <= self.roll_days_trigger:
            self.log(f"ROLL TRIGGER: {contract_symbol.value} is ITM with {dte} DTE. Liquidating.")
            self.liquidate(contract_symbol)


    def on_order_event(self, order_event):
        order = self.transactions.get_order_by_id(order_event.order_id)
        if order is None: return
       
        # --- Manage Pending Entry Orders ---
        # If an entry order was canceled or filled, remove it from our pending set
        is_entry_order = order.direction == OrderDirection.SELL and "Profit Taker" not in (order.tag or "")
        if is_entry_order and order_event.status in [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.INVALID]:
            if order.symbol in self.pending_entry_symbols:
                self.pending_entry_symbols.remove(order.symbol)


        # --- Handle Filled Orders ---
        if order_event.status != OrderStatus.FILLED: return
       
        order_tag = order.tag if order.tag is not None else ""
       
        if is_entry_order:
            self.open_trades[order.symbol] = { 'entry_price': order_event.fill_price }
            self.debug(f"Entry order filled for {order.symbol}. Stored entry price ${order_event.fill_price}.")
           
            gtc_properties = OrderProperties()
            gtc_properties.time_in_force = TimeInForce.GOOD_TIL_CANCELED
            self.limit_order(order.symbol, -order.quantity, self.profit_target_price,
                            tag="Profit Taker", order_properties=gtc_properties)


        if order.direction == OrderDirection.BUY:
            if order.symbol in self.open_trades:
                del self.open_trades[order.symbol]
                self.log(f"Closing order filled for {order.symbol}. Position removed from tracking.")
