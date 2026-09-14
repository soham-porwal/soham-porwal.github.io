# Source citations — option unit conventions

This codebase converts option quantities differently per provider. That difference is not a
style choice; it comes from what each provider documents. This file records the evidence for
every such claim the adapters rely on, so a reader can check it instead of trusting prose.

**The vendor pages themselves are not redistributed.** Full documentation HTML is a licensing
question, so the packaged bundle deliberately excludes every `.html` file. What ships is this
file plus the hashes below. To verify a claim, re-fetch the URL yourself and compare.

**Page content drifts.** Every hash below describes the bytes as retrieved on the stated date.
A later fetch that hashes differently is expected over time and is not evidence of tampering;
it means the vendor edited the page. The quotes are what those specific bytes said.

## How to check a quote

The preserved files are raw downloaded HTML — retrieved with `curl`, bytes unchanged, no
JavaScript executed, no authentication. Quoted sentences are therefore surrounded by markup,
and three details will otherwise defeat a literal search:

- Field names are wrapped in HTML tags. Plaid's sentence contains `<code>quantity</code>`,
  not a bare `quantity`.
- Apostrophes are the typographic character `’` (U+2019), not `'`.
- These pages embed a second, escaped copy of their own body for client-side rendering, so a
  quote may match more than once. The counts below are the real match counts.

Search for the plain-prose fragments given under each claim; those are byte-exact.

## Claim 1 — Plaid option `quantity` is the total number of options, not contracts

- **Source:** https://plaid.com/docs/api/products/investments/
- **Preserved as:** `sources/plaid-investments-api-reference-20260913.html`
- **sha256:** `2be691dddee9610c9ed732c0c7a49b643dbbb70828f309eee526fc092b9d8154`
- **Bytes:** 851336
- **Retrieved:** 2026-09-13 (UTC)
- **Matches in file:** 1 (this sentence appears once)

> The total quantity of the asset held, as reported by the financial institution. If the
> security is an option, `quantity` will reflect the total number of options (typically the
> number of contracts multiplied by 100), not the number of contracts.

Byte-exact search fragment: `The total quantity of the asset held, as reported by the financial institution.`
In the preserved bytes the word `quantity` in that sentence appears as `<code>quantity</code>`.

The same page documents security type `derivative` and an `option_contract` object with
`contract_type`, `expiration_date`, `strike_price` and `underlying_security_ticker` — the
fields the Plaid adapter reads to identify an option at all.

Note the word **typically**. Plaid documents the usual 100-share contract but does not
guarantee it. This codebase does not infer a multiplier from the payload; see "What is still
assumed" below.

A second Plaid page is preserved, `sources/plaid-investments-docs.html`
(https://plaid.com/docs/investments/, sha256 `02b372ce2de6da13d804be059afe3fcb1435ee1f0e8e3c122c6a72900a949270`,
129260 bytes, retrieved 2026-09-11). That one is the **product overview** and does **not**
contain this sentence or any statement of the unit convention. Both are kept so the
distinction is visible rather than asserted.

## Claim 2 — Alpaca option `qty` is a contract count

- **Source:** https://docs.alpaca.markets/docs/options-trading-overview
- **Preserved as:** `sources/alpaca-options-trading-overview-20260913.html`
- **sha256:** `9e34c116f6ddfbe8fc27e7f988aee3b2fbc0edfa65c7fd9d7b074e432ca0a591`
- **Bytes:** 651014
- **Retrieved:** 2026-09-13 (UTC)
- **Matches in file:** 3 (rendered copy plus the page's embedded escaped copies)

This page states no unit rule in prose. It is cited for its **worked examples**, which use
OCC option symbols with small integer quantities. Under the heading `Positions`, introduced by
"Option positions will show up like any other position in the positions endpoint":

> "symbol": "PTON240126C00000500",
> "asset_class": "us_option",
> "qty": "2",
> "avg_entry_price": "6.05",
> "cost_basis": "1210",

A `qty` of 2 against an `avg_entry_price` of 6.05 producing a `cost_basis` of 1210 is
arithmetic a reader can check by hand: `2 × 6.05 × 100 = 1210`. That is **consistency with a
100-unit contract**, not the page stating a multiplier — this page never states one. The
multiplier citation is Claim 3. Order examples on the same page use the OCC symbol
`AAPL240126C00050000` with `"qty": "1"`, likewise a contract count.

Byte-exact search fragment: `Option positions will show up like any other position in the positions endpoint`.

## Claim 3 — an Alpaca option contract covers 100 shares

- **Source:** https://docs.alpaca.markets/docs/options-level-3-trading
- **Preserved as:** `sources/alpaca-options-level-3-20260913.html`
- **sha256:** `790054b1baa78d305fe429a0da0253cd306d8a2313375d99307eb3917343328f`
- **Bytes:** 544926
- **Retrieved:** 2026-09-13 (UTC)
- **Matches in file:** 3 (rendered copy plus the page's embedded escaped copies)

> Because each option contract covers 100 shares, multiply by 100

And, in a maintenance-margin worked example on the same page:

> the option’s multiplier is 100 so the `maintenance_margin = strike_price_diff * multiplier`

Byte-exact search fragment: `Because each option contract covers 100 shares, multiply by 100`.
The second quote contains U+2019 in `option’s`.

## What the code does with these

The canonical quantity for an option is **always contracts**, whichever provider it came from:

- The Alpaca adapter tags option rows `units="contracts"` and passes the quantity through.
- The Plaid adapter tags option rows `units="underlying"`; the engine divides by the
  instrument multiplier and rejects a non-integer result as `FRACTIONAL_OPTION_CONTRACT`.
- Both then value the position as `quantity × multiplier × price`.

`captures/05-synthetic-short-option.json` and `captures/07-plaid-resolved.json` pin this:
−2 contracts through Alpaca and −200 underlying units through Plaid both normalize to
−500 USD. That test asserts **this codebase's conversion**, backed by the convention cited
above. It is not a certification of either provider's live behaviour.

## What is still assumed

- **The multiplier is not read from the payload.** It comes from the reviewed instrument
  master in `reference.json`, and the engine accepts only 100 for options
  (`UNSUPPORTED_OPTION_MULTIPLIER`). Adjusted contracts and non-standard deliverables — which
  Plaid's "typically" leaves room for — are out of scope and fail closed rather than being
  guessed.
- **The capture payloads are authored.** `captures/07-plaid-resolved.json` is registered
  `synthetic_edge_case` in `captures/provenance.json`. The unit convention is cited; the
  payload carrying it is not a customer observation and is not labelled as one.
- **The Fidelity CSV dialect has no vendor documentation.** It is pinned to an MIT-licensed,
  author-published fixture recorded in `sources/external-provenance.json`, not to a published
  specification.
- **No authenticated broker request was ever made.** Every claim here is about published
  documentation, not about a live account.
