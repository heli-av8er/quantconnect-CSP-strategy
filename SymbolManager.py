from AlgorithmImports import *


class SymbolManager:
    """
    Manages the indicators and logic for a single ETF.
    This version is designed to work within a multi-position framework.
    """
    def __init__(self, algorithm, symbol, min_dte, max_dte):
        self.algorithm = algorithm
        self.symbol = algorithm.add_equity(symbol, Resolution.HOUR).symbol
       
        option = algorithm.add_option(symbol, Resolution.MINUTE)
        option.set_filter(lambda u: u.weeklys_only().expiration(timedelta(min_dte), timedelta(max_dte)))
        self.option_symbol = option.symbol


        self.vwma_slow = algorithm.vwma(self.symbol, 21, Resolution.HOUR)
        self.vwma_fast = algorithm.vwma(self.symbol, 8, Resolution.HOUR)


    def is_trend_aligned(self):
        """Checks if the instrument's own hourly trend is bullish."""
        return self.vwma_fast.current.value > self.vwma_slow.current.value


    def check_risk_management(self, contract, entry_price, stop_multiplier, instrument_trend_aligned, regime_has_flipped):
        """Implements the "stop and re-evaluate" logic for a single position."""
        current_price = contract.price
       
        stop_price_triggered = current_price >= entry_price * stop_multiplier
       
        if not stop_price_triggered:
            if regime_has_flipped:
                return "Exit: Primary SOXX daily regime has flipped."
            return None


        self.algorithm.debug(f"Stop-loss trigger for {self.symbol}. Re-evaluating trend conditions...")


        if instrument_trend_aligned and not regime_has_flipped:
            self.algorithm.log("Holding position: Stop triggered, but local and regime trends remain aligned.")
            return None


        if regime_has_flipped:
            return "Exit: Stop triggered AND primary SOXX daily regime has flipped."
       
        self.algorithm.log("Holding position: Stop triggered and local trend weak, but primary regime trend is still intact.")
        return None


    def attempt_trade_entry(self, allocation_per_trade, adx_value, slice):
        """Finds and executes a new put sale using a fixed allocation per trade."""
        if not self.is_trend_aligned():
            return


        self.algorithm.debug(f"Trend alignment confirmed for {self.symbol}. Assessing trade entry.")
       
        # Determine target delta from ADX
        if adx_value > 40: target_delta = -0.5
        elif 25 <= adx_value <= 40: target_delta = -0.4
        else: target_delta = -0.3


        option_chain = slice.option_chains.get(self.option_symbol)
        if not option_chain: return


        puts = [c for c in option_chain if c.right == OptionRight.PUT]
        if not puts: return
       
        puts_with_greeks = [p for p in puts if hasattr(p.greeks, 'delta') and p.greeks.delta != 0]
        if not puts_with_greeks: return


        sorted_puts = sorted(puts_with_greeks, key=lambda c: (c.expiry, abs(c.greeks.delta - target_delta)))
        if not sorted_puts: return
        chosen_contract = sorted_puts[0]


        # Calculate position size using the FIXED allocation per trade from main.py
        cash_required = chosen_contract.strike * 100
        if cash_required == 0: return
        # Use total_portfolio_value to size based on the entire account value
        max_contracts = (self.algorithm.portfolio.total_portfolio_value * allocation_per_trade) / cash_required
       
        if max_contracts < 1: return
        quantity = -int(max_contracts)
       
        limit_price = chosen_contract.bid_price
        if limit_price is None or limit_price <= 0: return


        props = OrderProperties()
        props.time_in_force = TimeInForce.DAY
        self.algorithm.limit_order(chosen_contract.symbol, quantity, limit_price, order_properties=props)
