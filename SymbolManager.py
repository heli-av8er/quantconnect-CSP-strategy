from AlgorithmImports import *
from QuantConnect.Orders import *
from datetime import timedelta

class SymbolManager:
    """
    OVERHAULED: This version's entry logic is modified to find and
    execute Bull Put Spreads instead of single puts.
    """
    def __init__(self, algorithm, symbol_str, min_dte, max_dte, slow_period, fast_period, spread_width):
        self.algorithm = algorithm
        self.symbol = algorithm.add_equity(symbol_str, Resolution.HOUR).symbol
        self.spread_width = spread_width
        
        option = algorithm.add_option(symbol_str, Resolution.MINUTE)
        option.set_filter(lambda u: u.weeklys_only().expiration(timedelta(min_dte), timedelta(max_dte)))
        self.option_symbol = option.symbol

        self.vwma_slow = algorithm.vwma(self.symbol, slow_period, Resolution.HOUR)
        self.vwma_fast = algorithm.vwma(self.symbol, fast_period, Resolution.HOUR)

    def is_trend_aligned(self):
        """Checks if the instrument's own hourly trend is bullish (V1 Logic)."""
        if not self.vwma_fast.is_ready: return False
        return self.vwma_fast.current.value > self.vwma_slow.current.value

    def attempt_trade_entry(self, allocation_per_trade, adx_value, slice, margin_per_spread):
        """Finds two contracts and executes a Bull Put Spread."""
        if not self.is_trend_aligned(): return

        # --- Find Short Leg (V1 Logic) ---
        option_chain = slice.option_chains.get(self.option_symbol)
        if not option_chain: return

        puts = [c for c in option_chain if c.right == OptionRight.PUT]
        if not puts: return
        
        if adx_value > 40: target_delta = -0.5
        elif 25 <= adx_value <= 40: target_delta = -0.4
        else: target_delta = -0.3
        
        puts_with_greeks = [p for p in puts if hasattr(p.greeks, 'delta') and p.greeks.delta != 0]
        if not puts_with_greeks: return
        
        # Sort by closest delta first, then by expiry to break ties
        sorted_by_delta = sorted(puts_with_greeks, key=lambda c: abs(c.greeks.delta - target_delta))
        if not sorted_by_delta: return
        short_leg_contract = sorted_by_delta[0]

        # --- Find Long Leg ---
        long_strike = short_leg_contract.strike - self.spread_width
        # Find all contracts with the matching long strike and expiry
        long_leg_contracts = [c for c in puts if c.strike == long_strike and c.expiry == short_leg_contract.expiry]
        if not long_leg_contracts: 
            self.algorithm.log(f"Could not find matching long leg for strike {long_strike}")
            return
        long_leg_contract = long_leg_contracts[0]

        # --- Calculate Size and Submit Orders ---
        if margin_per_spread <= 0: return
        
        # Calculate size based on defined risk
        max_contracts = (self.algorithm.portfolio.total_portfolio_value * allocation_per_trade) / margin_per_spread
        if max_contracts < 1: return
        quantity = int(max_contracts)

        # Ensure we have valid prices to place limit orders
        if short_leg_contract.bid_price <= 0 or long_leg_contract.ask_price <= 0:
             self.algorithm.log(f"Invalid prices for spread legs: Short Bid ${short_leg_contract.bid_price}, Long Ask ${long_leg_contract.ask_price}")
             return

        # Use the short leg symbol as the unique identifier for the spread
        if str(short_leg_contract.symbol) in self.algorithm.pending_entry_symbols:
            return

        # Submit orders with a tag to link them
        tag = str(short_leg_contract.symbol)
        self.algorithm.limit_order(short_leg_contract.symbol, -quantity, short_leg_contract.bid_price, tag=tag)
        self.algorithm.limit_order(long_leg_contract.symbol, quantity, long_leg_contract.ask_price, tag=tag)
        
        self.algorithm.pending_entry_symbols.add(str(short_leg_contract.symbol))
        self.algorithm.log(f"Submitted Bull Put Spread entry for {short_leg_contract.symbol.value}")

