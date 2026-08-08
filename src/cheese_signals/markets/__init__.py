"""Real-market trading: CFDs and futures through a MetaTrader 5 broker.

Separate from the Pocket Option side of this app because the economics are
different in a way that changes every decision.

A binary option pays a fixed 85% and charges nothing visible, so the entire
question is directional accuracy above 54.05%. A CFD pays whatever the move
was and charges a spread on every trade, so the question becomes whether the
*edge per trade* exceeds the *cost per trade* -- and that is measurable in
advance, per instrument, before a single strategy is written.

``costs.py`` does that measurement first, deliberately. It is the gate the
rest of this package sits behind.
"""
