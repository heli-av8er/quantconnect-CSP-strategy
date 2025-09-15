from AlgorithmImports import *
from QuantConnect.Orders import *
from datetime import timedelta

class SymbolManager:
    """
    OVERHAULED: This version implements the dual search logic for finding either
    a Bull Call (Debit) or Bull Put (Credit) spread based on a 1:1 risk/reward.
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

    def attempt_trade_entry(self, allocation_per_trade, slice, margin_per_spread):
        """
        Searches for the best bullish spread (Call Debit or Put Credit)
        that meets the 1:1 risk/reward criteria.
        """
        if not self.is_trend_aligned(): return

        underlying_price = self.algorithm.securities[self.symbol].price
        option_chain = slice.option_chains.get(self.option_symbol)
        if not option_chain: return

        # --- Search for the best Bull Put Spread (Credit) ---
        best_put_spread = self.find_best_bull_put_spread(option_chain)

        # --- Search for the best Bull Call Spread (Debit) ---
        best_call_spread = self.find_best_bull_call_spread(option_chain, underlying_price)

        # --- Decision Logic ---
        # Prioritize the Put Credit Spread if both are found
        if best_put_spread:
            self.execute_spread_trade(best_put_spread['short'], best_put_spread['long'], allocation_per_trade, margin_per_spread)
        elif best_call_spread:
            self.execute_spread_trade(best_call_spread['short'], best_call_spread['long'], allocation_per_trade, margin_per_spread)
        else:
            self.algorithm.log("No suitable spread found meeting the 1:1 risk/reward criteria.")

    def find_best_bull_put_spread(self, option_chain):
        """Finds the furthest OTM Bull Put Spread with >= 50% credit."""
        candidate_spreads = []
        target_credit = self.spread_width * 0.5

        puts = [c for c in option_chain if c.right == OptionRight.PUT]
        
        for short_leg in puts:
            if short_leg.bid_price <= 0: continue
            
            long_strike = short_leg.strike - self.spread_width
            long_leg_candidates = [c for c in puts if c.strike == long_strike and c.expiry == short_leg.expiry]
            
            if long_leg_candidates:
                long_leg = long_leg_candidates[0]
                if long_leg.ask_price <= 0: continue

                net_credit = short_leg.bid_price - long_leg.ask_price
                if net_credit >= target_credit:
                    candidate_spreads.append({'short': short_leg, 'long': long_leg, 'strike': short_leg.strike})
        
        if not candidate_spreads: return None
        # Return the one with the lowest strike (furthest OTM)
        return sorted(candidate_spreads, key=lambda x: x['strike'])[0]

    def find_best_bull_call_spread(self, option_chain, underlying_price):
        """Finds the deepest ITM Bull Call Spread with <= 50% debit."""
        candidate_spreads = []
        target_debit = self.spread_width * 0.5
        
        calls = [c for c in option_chain if c.right == OptionRight.CALL and c.strike < underlying_price] # Must be ITM

        for short_leg in calls:
            if short_leg.bid_price <= 0: continue

            long_strike = short_leg.strike - self.spread_width
            long_leg_candidates = [c for c in calls if c.strike == long_strike and c.expiry == short_leg.expiry]

            if long_leg_candidates:
                long_leg = long_leg_candidates[0]
                if long_leg.ask_price <= 0: continue

                # For a Bull Call Spread, we BUY the lower strike and SELL the higher strike
                net_debit = long_leg.ask_price - short_leg.bid_price
                if 0 < net_debit <= target_debit:
                    candidate_spreads.append({'short': short_leg, 'long': long_leg, 'strike': short_leg.strike})

        if not candidate_spreads: return None
        # Return the one with the highest strike (deepest ITM)
        return sorted(candidate_spreads, key=lambda x: x['strike'], reverse=True)[0]

    def execute_spread_trade(self, short_leg, long_leg, allocation, margin):
        """Calculates size and submits orders for the chosen spread."""
        if margin <= 0: return
        
        max_contracts = (self.algorithm.portfolio.total_portfolio_value * allocation) / margin
        if max_contracts < 1: return
        quantity = int(max_contracts)

        tag = str(short_leg.symbol)
        if tag in self.algorithm.pending_entry_symbols: return

        # For Bull Call, we BUY long and SELL short
        if short_leg.right == OptionRight.CALL:
             self.algorithm.limit_order(long_leg.symbol, quantity, long_leg.ask_price, tag=tag)
             self.algorithm.limit_order(short_leg.symbol, -quantity, short_leg.bid_price, tag=tag)
             self.algorithm.log(f"Submitted Bull Call Spread entry for {short_leg.symbol.value}")
        # For Bull Put, we SELL short and BUY long
        else:
             self.algorithm.limit_order(short_leg.symbol, -quantity, short_leg.bid_price, tag=tag)
             self.algorithm.limit_order(long_leg.symbol, quantity, long_leg.ask_price, tag=tag)
             self.algorithm.log(f"Submitted Bull Put Spread entry for {short_leg.symbol.value}")
             
        self.algorithm.pending_entry_symbols.add(tag)

